#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

# Read ingress path provided by Home Assistant
INGRESS_PATH="$(bashio::app.ingress_entry)"
export INGRESS_PATH

# Read addon version provided by Home Assistant
ADDON_VERSION="$(bashio::addon.version)"
export ADDON_VERSION
APP_VERSION="${ADDON_VERSION}"
export APP_VERSION

export DATA_DIR="/data"
export PORT="8099"
export DISPLAY_PORT="8100"
export EXTERNAL_API_PORT="8110"
STORAGE_BACKEND="$(bashio::config 'storage_backend')"
DATABASE_URL="$(bashio::config 'database_url')"
SESSION_TIMEOUT_MINUTES="$(bashio::config 'session_timeout_minutes')"
export STORAGE_BACKEND DATABASE_URL SESSION_TIMEOUT_MINUTES

if [ -z "${STORAGE_BACKEND}" ]; then
	export STORAGE_BACKEND="internal"
fi

if [ "${STORAGE_BACKEND}" != "internal" ] && [ -z "${DATABASE_URL}" ]; then
	bashio::log.error "DATABASE_URL is required when STORAGE_BACKEND=${STORAGE_BACKEND}"
	exit 1
fi

bashio::log.info "Starting BarTender management server on port ${PORT} (ingress: ${INGRESS_PATH})"
bashio::log.info "Starting BarTender display server on port ${DISPLAY_PORT}"
bashio::log.info "Starting BarTender external API server on port ${EXTERNAL_API_PORT}"

cd /app || exit 1

# Start the read-only display server with a production WSGI server in the background
waitress-serve --threads=1 --listen="0.0.0.0:${DISPLAY_PORT}" bartender.display_wsgi:display_app &

# Start external API-only listener with a production WSGI server in the background
EXTERNAL_API_MODE="true" waitress-serve --threads=1 --listen="0.0.0.0:${EXTERNAL_API_PORT}" bartender.wsgi:app &

# Start the management server with a production WSGI server in the foreground
waitress-serve --threads=1 --listen="0.0.0.0:${PORT}" bartender.wsgi:app
