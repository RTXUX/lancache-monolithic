#!/bin/sh
set -eu
cd "$(dirname "$0")"
project="steam-cache-test-$$"
compose() { docker compose -p "$project" -f compose.yaml "$@"; }
cleanup() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then compose logs --tail=150; fi
    compose down --volumes --remove-orphans >/dev/null
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
compose build
compose up -d --wait --wait-timeout 120 origin nginx varnish
compose run --rm --no-deps tests
compose run --rm --no-deps tests integration.py seed
compose restart varnish
compose up -d --wait --wait-timeout 120 varnish
compose run --rm --no-deps tests integration.py restart
