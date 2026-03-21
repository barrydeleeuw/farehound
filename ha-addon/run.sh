#!/usr/bin/with-contenv bashio

# Read add-on options
export SERPAPI_API_KEY=$(bashio::config 'serpapi_api_key')
export ANTHROPIC_API_KEY=$(bashio::config 'anthropic_api_key')
export HA_NOTIFY_SERVICE=$(bashio::config 'ha_notify_service')
export POLL_INTERVAL_HOURS=$(bashio::config 'poll_interval_hours')
export ALERT_THRESHOLD=$(bashio::config 'alert_threshold')

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
bashio::log.info "Poll interval: ${POLL_INTERVAL_HOURS}h | Alert threshold: ${ALERT_THRESHOLD}"

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
