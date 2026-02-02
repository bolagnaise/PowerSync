#!/usr/bin/with-contenv bashio
# shellcheck shell=bash

# Get configuration
LOG_LEVEL=$(bashio::config 'log_level')
OPTIMIZATION_INTERVAL=$(bashio::config 'optimization_interval')
HORIZON_HOURS=$(bashio::config 'horizon_hours')
DEFAULT_COST_FUNCTION=$(bashio::config 'default_cost_function')

# Export configuration as environment variables
export LOG_LEVEL="${LOG_LEVEL}"
export OPTIMIZATION_INTERVAL="${OPTIMIZATION_INTERVAL}"
export HORIZON_HOURS="${HORIZON_HOURS}"
export DEFAULT_COST_FUNCTION="${DEFAULT_COST_FUNCTION}"

# Get Home Assistant API details
export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export HA_URL="http://supervisor/core"

bashio::log.info "Starting PowerSync Optimizer..."
bashio::log.info "Log level: ${LOG_LEVEL}"
bashio::log.info "Optimization interval: ${OPTIMIZATION_INTERVAL} minutes"
bashio::log.info "Horizon: ${HORIZON_HOURS} hours"
bashio::log.info "Default cost function: ${DEFAULT_COST_FUNCTION}"

# Start the Flask server
cd /app
exec python3 -m optimizer.server
