# Vercel Deployment Configuration for AutoGPT Platform

## Architecture Overview

This `vercel.json` configures the AutoGPT Platform for deployment on Vercel as a multi-service project:

```
Vercel Project
├── frontend (Next.js on Node.js Runtime)
│   └── Listens on port 3000
│   └── Serves UI and auth endpoints (/api/auth/*)
│   └── Calls backend via local rewrites at /api
│
└── backend (FastAPI on Python Runtime)
    └── Listens on port 8000
    └── Serves REST API at /api
    └── Handles all business logic and agent execution
```

## Services Explained

### `frontend` (Public)
- **Framework**: Next.js 15.x (App Router)
- **Runtime**: Node.js 24.x
- **Entry Point**: `node server.js` (standalone output)
- **Public Routes**:
  - `/` — UI application
  - `/api/auth/*` — Better Auth endpoints (login, signup, password reset)
  - All other routes fall through to the catch-all rewrite

**Important**: The frontend embeds the auth service (Better Auth) which directly connects to PostgreSQL. It does NOT call the backend for auth—auth is self-contained.

### `backend` (Internal)
- **Framework**: FastAPI (Python 3.13)
- **Runtime**: Python 3.13
- **Entry Point**: `poetry run rest` (from `[tool.poetry.scripts]` in `pyproject.toml`)
- **Port**: 8000 (default for Vercel Functions)
- **Public Routes**: Only reachable via `/api` rewrite
  - `/api/*` → FastAPI REST endpoints
  - `/api/health`, `/api/agents`, `/api/runs`, etc.

**Max Duration**: Set to 60 seconds (increase to 900 for Pro tier if needed)

## Routing Flow

```
Client Request
    ↓
Vercel Edge (Load Balancer)
    ↓
    ├─ /api/* → backend service (FastAPI on Python runtime)
    └─ /* → frontend service (Next.js on Node.js runtime)
    
Frontend (Next.js)
    ├─ Static pages, UI components
    ├─ Server-side rendering (SSR)
    └─ API routes at /api/proxy/* (rewrite to /api/*)
    
Frontend → Backend calls:
    fetch('/api/agents') → FastAPI handler
    fetch('/api/runs') → FastAPI handler
    etc.
```

## Environment Variables

### Frontend Environment Variables
Required by the frontend service (set in Vercel project settings):

```bash
# Better Auth Configuration
BETTER_AUTH_SECRET=<generated-secret>                    # Secure random string
BETTER_AUTH_URL=<deployment-url>                         # e.g., https://autogpt.vercel.app
DATABASE_URL=postgresql://...@...                        # Managed PostgreSQL connection
AUTH_DB_SCHEMA=platform                                  # Schema name for auth tables

# Google OAuth (optional)
AUTH_GOOGLE_CLIENT_ID=<google-oauth-client-id>
AUTH_GOOGLE_CLIENT_SECRET=<google-oauth-secret>

# Other optional configs
NEXT_PUBLIC_LAUNCHDARKLY_CLIENT_ID=<flag-client-id>
NEXT_PUBLIC_POSTHOG_KEY=<posthog-key>
OPENAI_API_KEY=<for-voice-transcription>
```

### Backend Environment Variables
Required by the backend service (set in Vercel project settings):

```bash
# Database
DATABASE_URL=postgresql://...@...                        # Same as frontend
DIRECT_URL=postgresql://...@...                          # For Prisma migrations
DB_HOST=<managed-db-host>
DB_PORT=5432
DB_USER=postgres
DB_PASS=<secure-password>
DB_NAME=postgres

# Redis (use managed service like Upstash)
REDIS_HOST=<redis-host>
REDIS_PORT=6379
REDIS_PASSWORD=<redis-password>

# RabbitMQ (use managed service like CloudAMQP)
RABBITMQ_HOST=<rabbitmq-host>
RABBITMQ_DEFAULT_USER=<user>
RABBITMQ_DEFAULT_PASS=<password>

# Authentication
JWT_JWKS_URL=<deployment-url>/api/auth/jwks              # e.g., https://autogpt.vercel.app/api/auth/jwks

# Security Keys (MUST generate these)
ENCRYPTION_KEY=<generated-fernet-key>
UNSUBSCRIBE_SECRET_KEY=<generated-secret>
VAPID_PRIVATE_KEY=<generated-vapid-private>
VAPID_PUBLIC_KEY=<generated-vapid-public>

# Platform URLs
PLATFORM_BASE_URL=<deployment-url>/api                   # e.g., https://autogpt.vercel.app/api
FRONTEND_BASE_URL=<deployment-url>                       # e.g., https://autogpt.vercel.app

# LLM & AI Services
OPENAI_API_KEY=<openai-key>
ANTHROPIC_API_KEY=<anthropic-key>
GROQ_API_KEY=<groq-key>

# Email Service (for notifications)
POSTMARK_SERVER_API_TOKEN=<postmark-token>
POSTMARK_SENDER_EMAIL=<sender@example.com>

# Optional: Feature Flags
LAUNCH_DARKLY_SDK_KEY=<launchdarkly-key>
SENTRY_DSN=<sentry-dsn>                                  # Error tracking

# Optional: Analytics
POSTHOG_API_KEY=<posthog-key>
```

## What's NOT Deployed on Vercel

The following services require persistent infrastructure and are not deployed here:

- **Executor** �� Runs agent workflows (stateful, long-running)
- **WebSocket Server** — Real-time agent communication
- **Scheduler** — Cron jobs and scheduled agent runs
- **Notification Server** — Background job processing
- **Database Manager** — Database connection pooling and queries
- **ClamAV** — Malware scanning service
- **FalkorDB** — Graph database for knowledge memory

**Deploy these separately on:**
- Kubernetes cluster (EKS, GKE, AKS)
- Docker Compose on a VM or dedicated server
- Managed container service (AWS ECS, Google Cloud Run)

These services communicate via:
- **PostgreSQL** (managed database)
- **Redis Cluster** (managed cache/message queue)
- **RabbitMQ** (managed message broker)

## Local Development → Vercel Migration

### Vercel Limitations to Be Aware Of

1. **Function Timeout**: 60 seconds by default (900 seconds on Pro tier)
   - Long-running agent tasks must be offloaded to background jobs (use Celery or Bull with external worker)
   - Current `maxDuration: 60` is set; upgrade if using Pro tier

2. **No WebSocket Support in Functions**
   - WebSocket server must run on external infrastructure
   - Frontend WebSocket connections won't work on Vercel (remove or implement polling)

3. **No Background Jobs**
   - Scheduler and notification services must run externally
   - Use services like: Bull Queue, Temporal, AWS Step Functions

4. **Stateless Only**
   - No files can be persisted to disk
   - All data must go to database or external storage (GCS, S3)

### Frontend Changes Required

Currently the frontend reads these env vars:
```typescript
NEXT_PUBLIC_AGPT_SERVER_URL=http://localhost:8006/api
NEXT_PUBLIC_AGPT_WS_SERVER_URL=ws://localhost:8001/ws
```

**For Vercel, update to:**
```bash
# .env.production
NEXT_PUBLIC_AGPT_SERVER_URL=/api                         # Local rewrite
NEXT_PUBLIC_AGPT_WS_SERVER_URL=                          # Remove (no WebSocket support)
```

The frontend Next.js config already has rewrites set up in `next.config.mjs`, so API calls to `/api/*` are routed correctly to the backend service.

### Backend Changes Required

Update environment variable reads to use Vercel-provided URLs:

```python
# backend/app.py or config
import os

# On Vercel, these are injected by the platform
PLATFORM_BASE_URL = os.getenv("PLATFORM_BASE_URL")      # e.g., https://autogpt.vercel.app/api
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL")      # e.g., https://autogpt.vercel.app
```

## Deployment Steps

1. **Create Vercel Project**
   ```bash
   vercel link
   ```

2. **Set Environment Variables**
   - Go to Vercel Dashboard → Project Settings → Environment Variables
   - Add all required env vars from sections above
   - Set for Production, Preview, and Development environments

3. **Configure Database**
   - Create managed PostgreSQL (e.g., Supabase, AWS RDS, PlanetScale)
   - Run migrations: `poetry run prisma migrate deploy`

4. **Configure External Services**
   - Redis (Upstash, Redis Cloud, etc.)
   - RabbitMQ (CloudAMQP, Heroku CloudAMQP add-on, etc.)

5. **Deploy**
   ```bash
   vercel deploy --prod
   ```

6. **Deploy Background Services Separately**
   - Executor, scheduler, notifications on Kubernetes or Docker
   - Connect to same PostgreSQL, Redis, RabbitMQ

## Monitoring & Debugging

- **Vercel Logs**: `vercel logs` or Vercel Dashboard
- **Backend Logs**: Check Vercel Function logs for FastAPI output
- **Database**: Use `psql` or managed console
- **Sentry**: Real-time error tracking (if configured)
- **PostHog**: Analytics and feature usage

## Limitations & Future Considerations

1. **WebSocket Support**: Implement polling or use external WebSocket server (Socket.io on Node)
2. **Agent Execution**: Current design assumes executor runs continuously; refactor for serverless
3. **File Storage**: Use GCS or S3; don't rely on local `/tmp`
4. **Caching**: Use Redis or Vercel KV instead of in-memory caches

## References

- [Vercel Services Documentation](https://vercel.com/docs/services)
- [Vercel Python Support](https://vercel.com/docs/functions/serverless-functions/runtimes/python)
- [Next.js on Vercel](https://vercel.com/docs/frameworks/nextjs)
- [AutoGPT Platform Self-Hosting Guide](https://docs.agpt.co/platform/getting-started)
