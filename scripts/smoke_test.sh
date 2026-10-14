#!/usr/bin/env bash
# Smoke-test a running PrintBridge container: upload a 3MF, wait for PrusaSlicer, fetch the G-code.
set -euo pipefail
BASE=${1:-http://localhost:8000}
MODEL=${2:-tests/fixtures/two-blocks.3mf}

for _ in $(seq 1 30); do
  curl -fsS "$BASE/api/profiles" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$BASE/api/profiles" | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p["slicer_found"], "PrusaSlicer not found"; print("slicer found")'

id=$(curl -fsS -F "file=@$MODEL" -F 'options={"quality":"draft","infill":15}' "$BASE/api/jobs" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "job $id"

for _ in $(seq 1 120); do
  job=$(curl -fsS "$BASE/api/jobs/$id")
  state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["state"])' "$job")
  case $state in
    ready) break ;;
    failed) echo "$job"; exit 1 ;;
  esac
  sleep 1
done
[ "$state" = ready ] || { echo "timed out in state $state"; exit 1; }
echo "$job" | python3 -c 'import json,sys; print("stats:", json.load(sys.stdin)["stats"])'

curl -fsS "$BASE/api/jobs/$id/gcode" -o /tmp/smoke.gcode
grep -q '^G28' /tmp/smoke.gcode
grep -q '^M420 S1' /tmp/smoke.gcode
curl -fsS "$BASE/healthz" | python3 -c 'import json,sys; print("version:", json.load(sys.stdin)["version"])'
echo "smoke test passed ($(wc -c </tmp/smoke.gcode) bytes of G-code)"
