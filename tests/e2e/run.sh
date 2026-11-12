#!/usr/bin/env bash
# End-to-end test of the built image with its Pushgateway sidecar and Prometheus.
# Uses only disposable Docker volumes. Usage: tests/e2e/run.sh [--keep]
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
export IMAGE="${IMAGE:-rustic-scheduler:e2e}"
COMPOSE=(docker compose -f compose.e2e.yaml)
KEEP="${1:-}"

step() { printf '\n== %s\n' "$*"; }
fail() { printf 'FAILED: %s\n' "$*" >&2; "${COMPOSE[@]}" logs --tail 60 >&2 || true; exit 1; }
cleanup() { [ "$KEEP" = "--keep" ] || "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

step "build the image"
docker build -q -f "$ROOT/docker/Dockerfile" -t "$IMAGE" "$ROOT" >/dev/null

# The Prometheus example configuration, unchanged except for a faster interval.
sed 's/scrape_interval: 30s/scrape_interval: 2s/' "$ROOT/examples/prometheus/prometheus.yml" > prometheus.yml
chmod 644 prometheus.yml rustic_password

step "prepare volumes: a source with a file only root can read, and a writable state dir"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" create >/dev/null 2>&1
project="rustic-scheduler-e2e"
docker run --rm -v "${project}_source:/data" -v "${project}_state:/state" --entrypoint sh "$IMAGE" -c true 2>/dev/null || true
docker run --rm --user 0 -v "${project}_source:/data" -v "${project}_state:/state" --entrypoint sh "$IMAGE" -c '
  echo "hello" > /data/readable.txt
  echo "only root may read this" > /data/root-only.txt
  chown 0:0 /data/root-only.txt && chmod 600 /data/root-only.txt
  mkdir -p /data/private && echo "nested" > /data/private/nested.txt && chmod 700 /data/private
  chown 1000:1000 /state'

step "initialise a throwaway repository with plain Rustic inside the image"
docker run --rm -v "${project}_state:/var/lib/rustic-scheduler" -v "$PWD/profiles:/etc/rustic-scheduler/profiles:ro" \
  -v "$PWD/rustic_password:/run/secrets/rustic_password:ro" --entrypoint rustic "$IMAGE" \
  -P /etc/rustic-scheduler/profiles/repo.toml init >/dev/null 2>&1 || fail "rustic init"

step "start the stack and wait for the container health check"
"${COMPOSE[@]}" up -d >/dev/null 2>&1
for _ in $(seq 60); do
  health="$(docker inspect --format '{{.State.Health.Status}}' "${project}-rustic-scheduler-1" 2>/dev/null || true)"
  [ "$health" = "healthy" ] && break
  "${COMPOSE[@]}" exec -T rustic-scheduler rustic-scheduler healthcheck >/dev/null 2>&1 && break
  sleep 1
done
"${COMPOSE[@]}" exec -T rustic-scheduler rustic-scheduler healthcheck || fail "scheduler is not ready"
sched() { "${COMPOSE[@]}" exec -T rustic-scheduler "$@"; }

step "runs unprivileged, on a read-only root filesystem, with one capability in its bounding set"
[ "$(sched id -u)" = "1000" ] || fail "not running as uid 1000"
sched sh -c 'touch /probe 2>/dev/null' && fail "root filesystem is writable"
sched sh -c 'cat /data/source/root-only.txt >/dev/null 2>&1' && fail "the scheduler process itself can read root-only files"
sched sh -c 'grep -q "^CapEff:.0000000000000000" /proc/1/status' || fail "the scheduler has effective capabilities"
sched rustic-scheduler validate | grep -q "valid" || fail "validate"

step "manual backup through the control socket"
sched rustic-scheduler trigger demo --wait | tee /dev/stderr | grep -q "backup succeeded" || fail "backup did not succeed"

step "the snapshot contains the root-only file (read through CAP_DAC_READ_SEARCH)"
listing="$(sched rustic-scheduler rustic demo -- ls latest 2>/dev/null)"
for f in readable.txt root-only.txt private/nested.txt; do
  echo "$listing" | grep -q "$f" || fail "$f is missing from the snapshot"
done
sched rustic-scheduler rustic demo -- snapshots --json 2>/dev/null | grep -q '"hostname": "e2e-host"' || fail "snapshot host"

step "scheduler metrics"
metrics="$(sched python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9300/metrics").read().decode())')"
echo "$metrics" | grep -q 'rustic_scheduler_operation_last_outcome{backup_job="demo",operation="backup",outcome="succeeded"} 1.0' || fail "backup outcome metric"
echo "$metrics" | grep -q 'rustic_scheduler_operation_last_outcome{backup_job="demo",operation="check",outcome="succeeded"} 1.0' || fail "check outcome metric"
echo "$metrics" | grep -q 'rustic_scheduler_snapshots{backup_job="demo"} 1.0' || fail "snapshot telemetry metric"
echo "$metrics" | grep -q 'rustic_scheduler_pushgateway_push_verified{backup_job="demo"} 1.0' || fail "push verification metric"

step "Prometheus scrapes both endpoints with the example configuration"
port="$("${COMPOSE[@]}" port prometheus 9090 | sed 's/.*://')"
query() { curl -fsS --get "http://127.0.0.1:${port}/api/v1/query" --data-urlencode "query=$1"; }
ok=""
for _ in $(seq 40); do
  a="$(query 'rustic_scheduler_operation_last_outcome{backup_job="demo",operation="backup",outcome="succeeded"}' || true)"
  b="$(query 'rustic_backup_files_new{snapshot_label="demo",job="rustic_backup"}' || true)"
  if echo "$a" | grep -q '"job":"rustic-scheduler"' && echo "$b" | grep -q '"hostname":"e2e-host"'; then ok=1; break; fi
  sleep 1
done
[ -n "$ok" ] || fail "Prometheus does not have both metric families"
curl -fsS "http://127.0.0.1:${port}/api/v1/targets" | grep -o '"health":"[a-z]*"' | sort | uniq -c

step "pushed metrics survive a Pushgateway restart (persistence file)"
sleep 2
"${COMPOSE[@]}" restart pushgateway >/dev/null 2>&1
sleep 2
"${COMPOSE[@]}" exec -T rustic-scheduler python -c 'import urllib.request;print(urllib.request.urlopen("http://pushgateway:9091/metrics").read().decode())' \
  | grep -q 'rustic_backup_files_new{.*snapshot_label="demo"' || fail "metrics were lost on Pushgateway restart"

step "graceful stop, then restart: last results are still reported"
"${COMPOSE[@]}" stop -t 60 rustic-scheduler >/dev/null 2>&1
code="$(docker inspect --format '{{.State.ExitCode}}' "${project}-rustic-scheduler-1")"
[ "$code" = "0" ] || fail "scheduler exited with status $code"
"${COMPOSE[@]}" start rustic-scheduler >/dev/null 2>&1
for _ in $(seq 30); do sched rustic-scheduler healthcheck >/dev/null 2>&1 && break; sleep 1; done
sched python -c 'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9300/metrics").read().decode())' \
  | grep -q 'rustic_scheduler_operation_last_outcome{backup_job="demo",operation="backup",outcome="succeeded"} 1.0' || fail "state was lost on restart"

step "no secret in the logs"
"${COMPOSE[@]}" logs 2>&1 | grep -q "e2e-test-password" && fail "password found in logs"

printf '\nEND-TO-END TEST PASSED\n'
