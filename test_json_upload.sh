#!/bin/bash
# JSON-shape upload test (what the userscript sends) + round-trip + intake.
set -e
BASE="${BASE:-${BASE:-http://127.0.0.1:8081}}"
TOKEN=$(cat ${ZEN_DIR:-$HOME/Documents}/zen-images/.upload-token)
curl -s "https://ella.janitorai.com/media-approved/RK6nu6wEIoWiNcaKilNdk.webp" -o /tmp/lt.webp
B64=$(base64 -w0 /tmp/lt.webp 2>/dev/null || base64 /tmp/lt.webp | tr -d "\n")
printf '{"name":"cat-test.webp","data":"%s"}' "$B64" > /tmp/upload.json
echo "--- 1) JSON upload (userscript shape):"
RESP=$(curl -s -X POST "$BASE/img/upload" -H "Content-Type: application/json" -H "X-Upload-Token: $TOKEN" \
  -d @/tmp/upload.json)
echo "$RESP" | head -c 200; echo
URL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['url'])")
echo "--- 2) fetch back: "
curl -s -o /tmp/lt2.webp -w "%{http_code} %{content_type} %{size_download}\n" "$URL"
echo "--- 3) intake of the JSON-uploaded image:"
curl -s -X POST "$BASE/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer test-invalid-key" \
  -d "{\"model\":\"test\",\"messages\":[{\"role\":\"user\",\"content\":\"look ![cat]($URL) pls\"}]}" \
  -o /dev/null -w "upstream:%{http_code}\n"
sleep 1
grep -a "imghost\|imgintake" ${ZEN_DIR:-$HOME/Documents}/zen-proxy.log | tail -3
