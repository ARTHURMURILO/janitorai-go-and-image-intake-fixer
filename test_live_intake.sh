#!/bin/bash
# End-to-end check: does the live bridge attach images before forwarding?
curl -s -X POST ${BASE:-http://127.0.0.1:8081}/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer test-invalid-key" \
  -d '{"model":"test","messages":[{"role":"user","content":"hi ![A image](https://ella.janitorai.com/media-approved/RK6nu6wEIoWiNcaKilNdk.webp) what is this?"}]}' \
  -o /tmp/last_upstream_resp.txt -w "upstream_status:%{http_code}\n"
echo "--- upstream said:"
head -c 300 /tmp/last_upstream_resp.txt; echo
sleep 1
echo "--- bridge log (imgintake):"
grep -a "imgintake" ${ZEN_DIR:-$HOME/Documents}/zen-proxy.log | tail -5
