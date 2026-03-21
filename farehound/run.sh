#!/usr/bin/with-contenv bashio

# API keys (read from HA add-on options, exported for config.py env resolution)
export SERPAPI_API_KEY=$(bashio::config 'serpapi_api_key')
export ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')

# Optional Telegram credentials
TELEGRAM_API_ID=$(bashio::config 'telegram_api_id')
TELEGRAM_API_HASH=$(bashio::config 'telegram_api_hash')
if [[ -n "$TELEGRAM_API_ID" ]]; then
    export TELEGRAM_API_ID
    export TELEGRAM_API_HASH
fi

# HA Supervisor token for API access (injected by Supervisor)
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"

# DuckDB data directory
export FAREHOUND_DATA_DIR="/data"

bashio::log.info "Starting FareHound..."

# Graceful shutdown
cleanup() {
    bashio::log.info "Shutting down FareHound..."
    kill -TERM "$PID" 2>/dev/null
    wait "$PID"
}
trap cleanup SIGTERM SIGINT

# Start orchestrator
python -m src.orchestrator &
PID=$!
wait "$PID"
