#!/usr/bin/env bash

# API keys (read from HA add-on options via /data/options.json)
if [ -f /data/options.json ]; then
    export SERPAPI_API_KEY=$(jq -r '.serpapi_api_key' /data/options.json)
    export ANTHROPIC_API_KEY=$(jq -r '.anthropic_api_key' /data/options.json)

    TG_API_ID=$(jq -r '.telegram_api_id // empty' /data/options.json)
    TG_API_HASH=$(jq -r '.telegram_api_hash // empty' /data/options.json)
    if [ -n "$TG_API_ID" ]; then
        export TELEGRAM_API_ID="$TG_API_ID"
        export TELEGRAM_API_HASH="$TG_API_HASH"
    fi

    TG_BOT=$(jq -r '.telegram_bot_token // empty' /data/options.json)
    TG_CHAT=$(jq -r '.telegram_chat_id // empty' /data/options.json)
    if [ -n "$TG_BOT" ]; then
        export TELEGRAM_BOT_TOKEN="$TG_BOT"
        export TELEGRAM_CHAT_ID="$TG_CHAT"
    fi
fi

# HA Supervisor token for API access (injected by Supervisor)
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

# DuckDB data directory
export FAREHOUND_DATA_DIR="/data"

echo "Starting FareHound..."

cd /app
exec python3 -m src.orchestrator
