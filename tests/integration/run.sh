#!/usr/bin/env bash
# Integration checks against a real nginx + the built image + a fake upstream.
set -uo pipefail
cd "$(dirname "$0")"

COMPOSE="docker compose -f docker-compose.it.yml -p ppcit"
BASE="http://localhost:18080"
pass=0; fail=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '        %s\n' "$2"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$3] got [$2]"; fi; }
has()  { case "$2" in *"$3"*) ok "$1";; *) bad "$1" "missing [$3] in: $(echo "$2"|head -c 200)";; esac; }
hasnt(){ case "$2" in *"$3"*) bad "$1" "unexpected [$3]";; *) ok "$1";; esac; }

cleanup() { $COMPOSE down -v --remove-orphans >/dev/null 2>&1; }
trap cleanup EXIT

echo "==> building + starting stack"
if ! $COMPOSE up -d --build >/tmp/ppcit-up.log 2>&1; then
  echo "STACK FAILED TO START"; tail -40 /tmp/ppcit-up.log; exit 1
fi

echo "==> waiting for readiness"
for i in $(seq 1 60); do
  curl -fsS "$BASE/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "$BASE/health" >/dev/null 2>&1; then
  echo "NEVER BECAME READY"; $COMPOSE logs --tail=40; exit 1
fi

echo
echo "=== nginx config actually loads (resolver fix) ==="
if $COMPOSE exec -T nginx nginx -t >/dev/null 2>&1; then ok "nginx -t"; else bad "nginx -t"; fi
check "nginx is serving" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health")" "200"

echo
echo "=== health / backend ==="
H=$(curl -s "$BASE/health")
check "redis connected"  "$(echo "$H" | jq -r .redis)"         "true"
check "backend reported" "$(echo "$H" | jq -r .cache_backend)" "redis"
check "cache state"      "$(echo "$H" | jq -r .cache_state)"   "connected"

echo
echo "=== JSON passthrough is lossless (PEP 700/708/740) ==="
J=$(curl -s -H 'Accept: application/vnd.pypi.simple.v1+json' "$BASE/simple/demo/")
has "PEP 700 versions"          "$J" '"versions"'
has "PEP 708 meta.tracks"       "$J" '"tracks"'
has "PEP 708 alternate-locs"    "$J" '"alternate-locations"'
has "PEP 740 provenance"        "$J" '"provenance"'
has "core-metadata hash kept"   "$J" 'deadbeef'
has "url rewritten"             "$J" '/artifacts/fake-files:9100/packages/'
SYN=$(curl -s -D- -o /dev/null -H 'Accept: application/vnd.pypi.simple.v1+json' "$BASE/simple/demo/" | tr -d '\r' | awk -F': ' '/[Xx]-[Ss]ynthesis/{print $2}')
check "X-Synthesis 0 on passthrough" "$SYN" "0"

echo
echo "=== HTML passthrough preserves unmodelled markup ==="
HT=$(curl -s -H 'Accept: text/html' "$BASE/simple/legacy/")
has "custom attr preserved" "$HT" 'data-custom-attr="preserve-me"'
has "requires-python kept"  "$HT" 'data-requires-python'
has "url rewritten"         "$HT" '/artifacts/fake-files:9100/packages/'

echo
echo "=== generated wheel metadata routes to Python ==="
META_URL="$BASE/artifacts/fake-files:9100/packages/legacy-1.0-py3-none-any.whl.metadata"
for i in $(seq 1 20); do
  META_STATUS=$(curl -s -o /tmp/ppcit-metadata -w '%{http_code}' "$META_URL")
  [ "$META_STATUS" = "200" ] && break
  # Extraction is deliberately asynchronous and bounded.
  curl -s -H 'Accept: text/html' "$BASE/simple/legacy/" >/dev/null
  sleep 1
done
check "generated metadata status" "$META_STATUS" "200"
has "generated metadata body" "$(cat /tmp/ppcit-metadata)" "Name: demo"
META_HEADERS=$(curl -s -D- -o /dev/null "$META_URL" | tr -d '\r')
has "metadata served by Python" "$META_HEADERS" "x-content-type-options: nosniff"

META_BAD=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/artifacts/evil.example/demo.whl.metadata")
check "metadata allowlist enforced" "$META_BAD" "403"

UPSTREAM_META_URL="$BASE/artifacts/fake-files:9100/packages/demo-1.0-py3-none-any.whl.metadata"
UPSTREAM_META=$(curl -s "$UPSTREAM_META_URL")
has "existing upstream metadata fallback" "$UPSTREAM_META" "Name: demo"
curl -s -o /dev/null "$UPSTREAM_META_URL"
UPSTREAM_META_CACHE=$(curl -s -D- -o /dev/null "$UPSTREAM_META_URL" | tr -d '\r' | awk -F': ' '/[Xx]-[Nn]ginx-[Cc]ache/{print $2}')
check "upstream metadata cache HIT" "$UPSTREAM_META_CACHE" "HIT"

echo
echo "=== artifact fetch through nginx (THE resolver fix) ==="
A=$(curl -s -o /tmp/ppcit-whl -w '%{http_code}' "$BASE/artifacts/fake-files:9100/packages/demo-1.0-py3-none-any.whl")
check "artifact 200" "$A" "200"
SIZE=$(wc -c </tmp/ppcit-whl | tr -d ' ')
WANT=$(python3 -c 'import upstream; print(len(upstream.WHEEL_BYTES))')
check "artifact bytes intact" "$SIZE" "$WANT"
PAYLOAD=$(python3 -c 'import zipfile; print(zipfile.ZipFile("/tmp/ppcit-whl").read("demo/__init__.py").decode())')
has "payload correct" "$PAYLOAD" "fake wheel payload"

echo "=== artifact is cached by nginx ==="
curl -s -o /dev/null "$BASE/artifacts/fake-files:9100/packages/demo-1.0-py3-none-any.whl"
CS=$(curl -s -D- -o /dev/null "$BASE/artifacts/fake-files:9100/packages/demo-1.0-py3-none-any.whl" | tr -d '\r' | awk -F': ' '/[Xx]-[Nn]ginx-[Cc]ache/{print $2}')
if [ "$CS" = "HIT" ]; then ok "nginx cache HIT"; else bad "nginx cache HIT" "got [$CS]"; fi

echo
echo "=== allowlist is enforced at nginx ==="
E=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/artifacts/evil.example/x.whl")
check "off-allowlist host 403" "$E" "403"

echo
echo "=== negative caching ==="
N1=$(curl -s -D- -o /dev/null "$BASE/simple/missing/" | tr -d '\r')
check "404 status" "$(echo "$N1"|head -1|awk '{print $2}')" "404"
sleep 1
N2=$(curl -s -D- -o /dev/null "$BASE/simple/missing/" | tr -d '\r')
has "404 served from cache" "$N2" "404"

echo
echo "=== background metadata enrichment (allowlist-gated) ==="
for i in $(seq 1 20); do
  M=$(curl -s "$BASE/metrics" | awk '/proxy_metadata_heads_total/{print $2}')
  [ "${M:-0}" -gt 0 ] && break
  sleep 1
done
if [ "${M:-0}" -gt 0 ]; then ok "metadata HEAD probes fired ($M)"; else bad "metadata HEAD probes" "none fired"; fi
UP_LOG=$($COMPOSE logs python-proxy 2>&1)
hasnt "no probe to off-allowlist host" "$UP_LOG" "evil.example"

echo
echo "=== metrics surface ==="
MT=$(curl -s "$BASE/metrics")
has "probe error counter" "$MT" "proxy_cache_redis_probe_errors_total"
has "backend gauge"       "$MT" "proxy_cache_backend"

echo
echo "================================"
printf "passed: %d   failed: %d\n" "$pass" "$fail"
[ "$fail" -eq 0 ] || { echo; echo "--- proxy logs ---"; $COMPOSE logs --tail=30 python-proxy; echo "--- nginx logs ---"; $COMPOSE logs --tail=30 nginx; }
exit $([ "$fail" -eq 0 ] && echo 0 || echo 1)
