"""FareHound Mini Web App backend.

FastAPI service that serves the Telegram WebApp UI at HTML routes (`/deal/{id}`,
`/routes`, `/settings`) and JSON action endpoints under `/api/`. Boots in the
same process as the bot — see `src.orchestrator.main` for the asyncio.gather
that runs both event loops in parallel.

Telegram identity flows in via `initData` (signed HMAC against the bot token);
no separate auth.
"""
