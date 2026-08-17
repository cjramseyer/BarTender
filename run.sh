#!/usr/bin/with-contenv bashio

# Read ingress path provided by Home Assistant
INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PATH
export DATA_DIR="/data"
export PORT="8099"
export DISPLAY_PORT="8100"

bashio::log.info "Starting BarTender management server on port ${PORT} (ingress: ${INGRESS_PATH})"
bashio::log.info "Starting BarTender display server on port ${DISPLAY_PORT}"

cd /app

# Start the read-only display server in the background
python3 -m bartender.display &

# Start the management server in the foreground
python3 -m bartender.app
