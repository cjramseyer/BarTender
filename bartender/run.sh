#!/usr/bin/with-contenv bashio

# Read ingress path provided by Home Assistant
INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PATH
export DATA_DIR="/data"
export PORT="8099"
export DISPLAY_PORT="8100"
export EXTERNAL_API_PORT="8110"

bashio::log.info "Starting BarTender management server on port ${PORT} (ingress: ${INGRESS_PATH})"
bashio::log.info "Starting BarTender display server on port ${DISPLAY_PORT}"
bashio::log.info "Starting BarTender external API server on port ${EXTERNAL_API_PORT}"

cd /app || exit 1

# Start the read-only display server in the background
python3 -m bartender.display &

# Start external API-only listener in the background
EXTERNAL_API_MODE="true" PORT="${EXTERNAL_API_PORT}" python3 -m bartender.app &

# Start the management server in the foreground
python3 -m bartender.app
