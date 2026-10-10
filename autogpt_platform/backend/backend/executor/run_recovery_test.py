"""Adoption of dropped graph executions."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.data.execution import ExecutionStatus, GraphExecutionMeta
from backend.executor import run_recovery
from backend.executor.manager import ExecutionManager
from backend.executor.cluster_lock import execution_lock_key
from backend.executor.run_recovery import recover_dropped_runs


class _Harness(SimpleNamespace):
    """The fakes a sweep runs against, plus the calls it made."""

    db: AsyncMock
    redis: AsyncMock
    requeue: AsyncMock


def _config(**overrides) -> SimpleNamespace:
    values = {
        "enable_dropped_run_recovery": True,
        "dropped_run_recovery_interval_seconds": 60,
        "dropped_run_recovery_grace_seconds": 600,
        "dropped_run_recovery_max_age_seconds": 86400,
        "dropped_run_recovery_batch_size": 25,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _meta(exec_id: str, *, dry_run: bool = False) -> GraphExecutionMeta:
    return GraphExecutionMeta(
        id=exec_id,
        user_id="user-1",
        graph_id="graph-1",
        graph_version=3,
        inputs=None,
        credential_inputs=None,
        nodes_input_masks=None,
        preset_id=None,
        status=ExecutionStatus.RUNNING,
        stats=None,
        is_dry_run=dry_run,
    )


def _requeued(status: ExecutionStatus = ExecutionStatus.QUEUED) -> SimpleNamespace:
    return SimpleNamespace(status=status)


def _close(actual: datetime, expected: datetime) -> bool:
    return abs((actual - expected).total_seconds()) < 60


@pytest.fixture
def sweep(mocker) -> _Harness:
    db = AsyncMock()
    db.get_graph_executions = AsyncMock(return_value=[])
    redis_client = AsyncMock()
    redis_client.get = AsyncMock(return_value=None)
    redis_client.set = AsyncMock(return_value=True)
    mocker.patch.object(run_recovery, "settings", SimpleNamespace(config=_config()))
    mocker.patch.object(
        run_recovery, "get_database_manager_async_client", return_value=db
    )
    mocker.patch.object(
        run_recovery.redis, "get_redis_async", new=AsyncMock(return_value=redis_client)
    )
    requeue = mocker.patch.object(
        run_recovery, "add_graph_execution", new=AsyncMock(return_value=_requeued())
    )
    return _Harness(db=db, redis=redis_client, requeue=requeue)


def test_the_lock_key_is_the_one_the_executor_holds():
    """The sweep reads the key the executor writes to claim a run; if the two
    ever drift apart, every unfinished run looks dropped."""
    assert execution_lock_key("exec-1") == "exec_lock:exec-1"


async def test_disabled_sweep_touches_nothing(mocker):
    db_factory = mocker.patch.object(
        run_recovery, "get_database_manager_async_client"
    )
    mocker.patch.object(
        run_recovery,
        "settings",
        SimpleNamespace(config=_config(enable_dropped_run_recovery=False)),
    )

    assert await recover_dropped_runs() == []
    db_factory.assert_not_called()


async def test_unowned_run_is_requeued(sweep: _Harness):
    sweep.db.get_graph_executions.return_value = [_meta("exec-1", dry_run=True)]

    assert await recover_dropped_runs() == ["exec-1"]

    kwargs = sweep.requeue.await_args.kwargs
    assert kwargs["graph_exec_id"] == "exec-1"
    assert kwargs["graph_id"] == "graph-1"
    assert kwargs["user_id"] == "user-1"
    assert kwargs["graph_version"] == 3
    assert kwargs["dry_run"] is True
    assert kwargs["bypass_paywall"] is True


async def test_owned_run_is_left_alone(sweep: _Harness):
    sweep.db.get_graph_executions.return_value = [_meta("exec-1")]
    sweep.redis.get.return_value = "other-pod"

    assert await recover_dropped_runs() == []

    sweep.redis.get.assert_awaited_once_with(execution_lock_key("exec-1"))
    sweep.requeue.assert_not_called()


async def test_a_run_another_pod_claimed_is_skipped(sweep: _Harness):
    """Every pod sweeps at once after a deploy; one of them owns each run."""
    sweep.db.get_graph_executions.return_value = [_meta("exec-1")]
    sweep.redis.set.return_value = None

    assert await recover_dropped_runs() == []

    sweep.requeue.assert_not_called()


async def test_claim_covers_one_sweep(sweep: _Harness):
    sweep.db.get_graph_executions.return_value = [_meta("exec-1")]

    assert await recover_dropped_runs() == ["exec-1"]

    kwargs = sweep.redis.set.await_args.kwargs
    assert kwargs["nx"] is True
    assert kwargs["ex"] == 60
    assert sweep.redis.set.await_args.args[0] == run_recovery.recovery_claim_key(
        "exec-1"
    )


async def test_run_that_did_not_become_queued_is_not_adopted(sweep: _Harness):
    """Losing the QUEUED transition to a concurrent sweeper means it published."""
    sweep.db.get_graph_executions.return_value = [_meta("exec-1")]
    sweep.requeue.return_value = _requeued(ExecutionStatus.RUNNING)

    assert await recover_dropped_runs() == []


async def test_candidates_are_bounded_to_the_dropped_run_window(sweep: _Harness):
    await recover_dropped_runs()

    kwargs = sweep.db.get_graph_executions.await_args.kwargs
    now = datetime.now(timezone.utc)
    assert kwargs["statuses"] == [ExecutionStatus.RUNNING]
    assert kwargs["order_by"] == "startedAt"
    assert kwargs["order_direction"] == "asc"
    assert kwargs["limit"] == run_recovery.PAGE_SIZE
    assert kwargs["offset"] == 0
    assert _close(kwargs["started_time_lte"], now - timedelta(seconds=600))
    assert _close(kwargs["started_time_gte"], now - timedelta(seconds=86400))


async def test_one_failed_adoption_does_not_stop_the_sweep(sweep: _Harness):
    sweep.db.get_graph_executions.return_value = [_meta("exec-1"), _meta("exec-2")]
    sweep.requeue.side_effect = [RuntimeError("rabbit is down"), _requeued()]

    assert await recover_dropped_runs() == ["exec-2"]


async def test_sweep_pages_past_runs_that_are_still_owned(sweep: _Harness):
    """A full page of healthy long-running runs must not hide a dropped one."""
    owned = [_meta(f"owned-{i}") for i in range(run_recovery.PAGE_SIZE)]
    dropped = _meta("dropped")
    owned_keys = {execution_lock_key(meta.id) for meta in owned}
    sweep.redis.get.side_effect = (
        lambda key: "other-pod" if key in owned_keys else None
    )
    sweep.db.get_graph_executions.side_effect = [owned, [dropped]]

    assert await recover_dropped_runs() == ["dropped"]

    offsets = [
        call.kwargs["offset"] for call in sweep.db.get_graph_executions.await_args_list
    ]
    assert offsets == [0, run_recovery.PAGE_SIZE]


async def test_the_candidate_window_is_fixed_across_pages(sweep: _Harness):
    """A moving upper bound would shift rows out from under the offset."""
    owned = [_meta(f"owned-{i}") for i in range(run_recovery.PAGE_SIZE)]
    sweep.db.get_graph_executions.side_effect = [owned, [_meta("dropped")]]
    sweep.redis.get.side_effect = (
        lambda key: "other-pod" if key.startswith("exec_lock:owned-") else None
    )

    assert await recover_dropped_runs() == ["dropped"]

    windows = [
        call.kwargs["started_time_lte"]
        for call in sweep.db.get_graph_executions.await_args_list
    ]
    assert len(windows) == 2
    assert windows[0] == windows[1]


async def test_adoptions_are_capped_by_the_batch_size(mocker, sweep: _Harness):
    mocker.patch.object(
        run_recovery,
        "settings",
        SimpleNamespace(config=_config(dropped_run_recovery_batch_size=1)),
    )
    sweep.db.get_graph_executions.return_value = [_meta("exec-1"), _meta("exec-2")]

    assert await recover_dropped_runs() == ["exec-1"]


async def test_scan_stops_at_the_maximum_page_count(sweep: _Harness):
    """A cluster with only healthy runs must not page forever."""
    page = [_meta(f"owned-{i}") for i in range(run_recovery.PAGE_SIZE)]
    sweep.db.get_graph_executions.return_value = page
    sweep.redis.get.return_value = "other-pod"

    assert await recover_dropped_runs() == []

    assert (
        sweep.db.get_graph_executions.await_count
        == run_recovery.MAX_SCAN // run_recovery.PAGE_SIZE
    )


def test_recovery_loop_sweeps_then_stops_on_shutdown(mocker):
    """The sweep must run on startup, not only after the first interval."""
    manager = ExecutionManager()
    sweeps = []

    async def _sweep() -> list[str]:
        sweeps.append(manager.stop_consuming.is_set())
        manager.stop_consuming.set()
        return []

    mocker.patch("backend.executor.manager.recover_dropped_runs", new=_sweep)

    manager._consume_dropped_runs()

    assert sweeps == [False]


def test_recovery_loop_survives_a_failed_sweep(mocker):
    manager = ExecutionManager()
    mocker.patch(
        "backend.executor.manager.settings",
        SimpleNamespace(config=_config(dropped_run_recovery_interval_seconds=0.01)),
    )
    sweeps = []

    async def _sweep() -> list[str]:
        sweeps.append(1)
        if len(sweeps) == 1:
            raise RuntimeError("redis is down")
        manager.stop_consuming.set()
        return []

    mocker.patch("backend.executor.manager.recover_dropped_runs", new=_sweep)

    manager._consume_dropped_runs()

    assert len(sweeps) == 2
