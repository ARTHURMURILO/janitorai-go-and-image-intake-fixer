#!/bin/bash
# Live test of the self-hosted image store + intake local-map.
set -e
BASE="${BASE:-${BASE:-http://127.0.0.1:8081}}"
TOKEN=$(cat ${ZEN_DIR:-$HOME/Documents}/zen-images/.upload-token 2>/dev/null || true)
if [ -z "$TOKEN" ]; then echo "no token file yet — triggering generation"; curl -s "$BASE/healthz" >/dev/null; TOKEN=$(cat ${ZEN_DIR:-$HOME/Documents}/zen-images/.upload-token); fi
echo "token: ${TOKEN:0:8}..."

# Copy a real image to work with (fetch the test webp once)
curl -s "https://ella.janitorai.com/media-approved/RK6nu6wEIoWiNcaKilNdk.webp" -o /tmp/livetest.webp

echo "--- 1) upload (should be 401 without token):"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$BASE/img/upload" -F "file=@/tmp/livetest.webp"

echo "--- 2) upload (with token):"
RESP=$(curl -s -X POST "$BASE/img/upload" -H "X-Upload-Token: $TOKEN" -F "file=@/tmp/livetest.webp")
echo "$RESP"
URL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
echo "returned url: $URL"

echo "--- 3) fetch it back publicly:"
curl -s -o /tmp/fetched.webp -w "%{http_code} %{content_type} %{size_download}\n" "$URL"

echo "--- 4) intake through the API path (bogus key, expect 401 upstream but imgintake attach):"
PUBPATH="${URL#*127.0.0.1:8081}"
curl -s -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer test-invalid-key" \
  -d "{\"model\":\"test\",\"messages\":[{\"role\":\"user\",\"content\":\"what is in ![the map]($URL)?\"}]}" \
  -o /dev/null -w "upstream_status:%{http_code}\n"
sleep 1
echo "--- 5) bridge log (imghost + imgintake):"
grep -a "imghost\|imgintake" ${ZEN_DIR:-$HOME/Documents}/zen-proxy.log | tail -5
