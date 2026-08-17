#!/usr/bin/with-contenv bashio

# Read ingress path provided by Home Assistant
INGRESS_PATH=$(bashio::addon.ingress_entry)
export INGRESS_PATH
export DATA_DIR="/data"
export PORT="8099"

bashio::log.info "Starting BarTender on port ${PORT} with ingress path: ${INGRESS_PATH}"

cd /app
python3 -m bartender.app
