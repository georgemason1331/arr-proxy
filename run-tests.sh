#!/usr/bin/env bash
# Build and test arr-proxy.
#
#   ./run-tests.sh            all suites
#   ./run-tests.sh mock       only the scripted end-to-end stack
#   ./run-tests.sh real       only the genuine Sonarr/Radarr stack
#   KEEP=1 ./run-tests.sh     leave the containers running afterwards
set -uo pipefail

SUITE="${1:-all}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E="$ROOT/tests/e2e"
FAILURES=()

phase() { printf '\n%s\n  %s\n%s\n' "$(printf '=%.0s' {1..70})" "$1" "$(printf '=%.0s' {1..70})"; }
step()  { local name="$1"; shift; "$@" || FAILURES+=("$name"); }

phase "Building images"
docker build -t arrproxy:e2e "$ROOT" || exit 1
docker build -t arrproxy-tester:e2e -f "$E2E/Dockerfile.tester" "$E2E" || exit 1

if [[ "$SUITE" == "unit" || "$SUITE" == "all" ]]; then
  phase "Unit tests"
  step unit env MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$ROOT/tests/unit:/tests/unit:ro" -e PYTHONPATH=/app -w /app \
    --entrypoint python arrproxy-tester:e2e -m pytest /tests/unit -q -p no:cacheprovider
fi

if [[ "$SUITE" == "mock" || "$SUITE" == "all" ]]; then
  phase "End-to-end against scripted instances"
  cd "$E2E"
  python make_seeds.py >/dev/null
  docker compose -f docker-compose.e2e.yml down -v --remove-orphans >/dev/null 2>&1
  docker compose -f docker-compose.e2e.yml up -d --wait || exit 1
  step e2e-mock docker compose -f docker-compose.e2e.yml --profile test run --rm tester
  phase "Failure modes (instances stopped and restarted)"
  step e2e-degraded python check_degraded.py
  [[ -z "${KEEP:-}" ]] && docker compose -f docker-compose.e2e.yml down -v --remove-orphans >/dev/null 2>&1
fi

if [[ "$SUITE" == "real" || "$SUITE" == "all" ]]; then
  phase "End-to-end against real Sonarr and Radarr"
  cd "$E2E"
  docker compose -f docker-compose.real.yml up -d --wait \
    real-sonarr-main real-sonarr-anime real-radarr-main real-radarr-anime || exit 1
  # Root folders must exist and be writable before the *arrs will accept them.
  docker exec -u 0 real-sonarr-main sh -c \
    'mkdir -p /data/tv /data/anime /data/movies /data/anime-movies && chown -R 1000:1000 /data'
  python setup_real.py || exit 1
  docker compose -f docker-compose.real.yml --profile proxy up -d proxy --wait || exit 1
  step e2e-real docker compose -f docker-compose.real.yml --profile test run --rm tester
  if [[ -z "${KEEP:-}" ]]; then
    docker compose -f docker-compose.real.yml --profile proxy --profile test \
      down -v --remove-orphans >/dev/null 2>&1
  fi
fi

phase "Summary"
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "All suites passed."
  exit 0
fi
echo "Failed: ${FAILURES[*]}"
exit 1
