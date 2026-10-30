#!/bin/sh
set -eu
cd "$(dirname "$0")"
project="steam-ats-test-$$"
compose() { docker compose -p "$project" -f compose.yaml "$@"; }
cleanup() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        compose logs --tail=100
        compose exec -T ats sh -c 'tail -n 100 /opt/ats/var/log/trafficserver/diags.log' || true
    fi
    compose down --volumes --remove-orphans >/dev/null
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
compose build
compose up -d --wait --wait-timeout 120 origin ats
compose exec -T ats traffic_ctl config get proxy.config.cache.enable_read_while_writer proxy.config.http.cache.open_write_fail_action proxy.config.http.cache.max_open_read_retries proxy.config.http.cache.open_read_retry_time proxy.config.http.cache.ignore_query
compose run --rm --no-deps tests
compose exec -T ats traffic_ctl metric get proxy.process.cache.read_busy.success
compose run --rm --no-deps tests integration.py seed
compose restart ats
compose up -d --wait --wait-timeout 120 ats
compose run --rm --no-deps tests integration.py restart
