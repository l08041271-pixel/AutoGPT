"""Adoption of dropped graph executions.

A run is *dropped* when the pod that claimed it disappears without finishing
it. The run message was already consumed, so the broker will not redeliver it,
and the row sits at RUNNING forever while the cluster lock quietly expires with
its owner. The lock is the authority on who is responsible for a run, so an
unfinished run with no lock has nobody working on it and is safe to re-queue.

Adoption goes through the same requeue path the admin recovery endpoint uses,
so the run continues from the node executions already persisted for it.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from prometheus_client import Counter

from backend.data import redis_client as redis
from backend.data.execution import ExecutionStatus, GraphExecutionMeta
from backend.executor.cluster_lock import execution_lock_key
from backend.executor.utils import add_graph_execution
from backend.util.clients import get_database_manager_async_client
from backend.util.settings import Settings

if TYPE_CHECKING:
    from backend.data.db_manager import DatabaseManagerAsyncClient
    from backend.data.redis_client import AsyncRedisClient

logger = logging.getLogger(__name__)
settings = Settings()

# Candidates are read a page at a time: a busy cluster keeps plenty of *owned*
# runs in the window, and one page can be nothing but those.
PAGE_SIZE = 100
MAX_SCAN = 500

# A fleet-wide restart runs every pod's sweep at once, and each pod sees the
# same dropped runs. The claim makes one pod responsible for a run per sweep.
RECOVERY_CLAIM_PREFIX = "exec_recovery:"

adopted_runs_counter = Counter(
    "execution_manager_adopted_dropped_runs",
    "Dropped graph executions re-queued for a live executor to resume",
)


async def recover_dropped_runs() -> list[str]:
    """Re-queue unfinished runs whose executor is gone, returning their ids."""
    if not settings.config.enable_dropped_run_recovery:
        return []

    db = get_database_manager_async_client()
    redis_client = await redis.get_redis_async()
    adopted = await _adopt_unowned(db, redis_client)
    if adopted:
        logger.info(f"Adopted {len(adopted)} dropped executions: {adopted}")
    return adopted


async def _adopt_unowned(
    db: "DatabaseManagerAsyncClient", redis_client: "AsyncRedisClient"
) -> list[str]:
    """Walk dropped-run candidates oldest first, adopting the unowned ones."""
    want = settings.config.dropped_run_recovery_batch_size
    adopted: list[str] = []
    scanned = 0
    while len(adopted) < want and scanned < MAX_SCAN:
        page = await _running_executions(db, _candidate_window(), scanned)
        if not page:
            break
        scanned += len(page)
        for meta in page:
            if len(adopted) >= want:
                break
            try:
                if await _adopt_if_unowned(redis_client, meta):
                    adopted.append(meta.id)
            except Exception as e:
                logger.warning(
                    f"Could not adopt dropped execution #{meta.id}: "
                    f"{type(e).__name__}: {e}"
                )
        if len(page) < PAGE_SIZE:
            break
    return adopted


def _candidate_window() -> tuple[datetime, datetime]:
    """Start-time bounds of a run that may have been dropped.

    Old enough that a healthy executor has had time to make progress, young
    enough that resuming it is still the right call rather than resurrecting
    an abandoned run.
    """
    now = datetime.now(timezone.utc)
    return (
        now - timedelta(seconds=settings.config.dropped_run_recovery_max_age_seconds),
        now - timedelta(seconds=settings.config.dropped_run_recovery_grace_seconds),
    )


async def _running_executions(
    db: "DatabaseManagerAsyncClient",
    window: tuple[datetime, datetime],
    offset: int,
) -> list[GraphExecutionMeta]:
    """Runs started inside *window* that are still marked RUNNING."""
    started_gte, started_lte = window
    return await db.get_graph_executions(
        statuses=[ExecutionStatus.RUNNING],
        started_time_gte=started_gte,
        started_time_lte=started_lte,
        order_by="startedAt",
        order_direction="asc",
        limit=PAGE_SIZE,
        offset=offset,
    )


def recovery_claim_key(graph_exec_id: str) -> str:
    """Key that gives a single pod the job of adopting a dropped run."""
    return f"{RECOVERY_CLAIM_PREFIX}{graph_exec_id}"


async def _adopt_if_unowned(
    redis_client: "AsyncRedisClient", meta: GraphExecutionMeta
) -> bool:
    """Re-queue *meta* unless an executor still holds its cluster lock.

    A sweeper that loses the claim, or the QUEUED transition, is not an error:
    it means another pod already adopted the run. The transition is what makes
    that safe, since only one conditional update can flip RUNNING to QUEUED.
    """
    if await _has_owner(redis_client, meta.id):
        return False
    if not await _claim(redis_client, meta.id):
        return False

    requeued = await add_graph_execution(
        graph_id=meta.graph_id,
        user_id=meta.user_id,
        graph_version=meta.graph_version,
        graph_exec_id=meta.id,
        dry_run=meta.is_dry_run,
        bypass_paywall=True,
    )
    if requeued.status != ExecutionStatus.QUEUED:
        return False

    adopted_runs_counter.inc()
    return True


async def _has_owner(redis_client: "AsyncRedisClient", graph_exec_id: str) -> bool:
    """True while some executor still holds the run's cluster lock."""
    return bool(await redis_client.get(execution_lock_key(graph_exec_id)))


async def _claim(redis_client: "AsyncRedisClient", graph_exec_id: str) -> bool:
    """True if this pod is the one adopting *graph_exec_id* this sweep."""
    return bool(
        await redis_client.set(
            recovery_claim_key(graph_exec_id),
            "1",
            nx=True,
            ex=settings.config.dropped_run_recovery_interval_seconds,
        )
    )
