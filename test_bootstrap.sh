#!/bin/bash
# Bootstrap gate test for /img/token.
NGROK="${NGROK:-https://YOUR-TUNNEL.ngrok-free.dev}"   # export NGROK=... to test your own tunnel
SKIP="-H ngrok-skip-browser-warning:1"

echo "1) localhost direct (no XFF, private remote) — expect 200:"
curl -s ${BASE:-http://127.0.0.1:8081}/img/token | head -c 90; echo

echo "2) via ngrok BEFORE any chat traffic from this IP — expect 403:"
curl -s $SKIP "$NGROK/img/token" | head -c 90; echo

echo "3) simulate chat traffic: authenticated POST /v1/chat/completions via ngrok:"
curl -s -o /dev/null -w "upstream:%{http_code}\n" -X POST "$NGROK/v1/chat/completions" \
  -H "Content-Type: application/json" -H "Authorization: Bearer fake-owner-key" \
  -d '{"model":"test","messages":[{"role":"user","content":"hi"}]}'

echo "4) via ngrok AFTER chat traffic from this IP — expect 200:"
curl -s $SKIP "$NGROK/img/token" | head -c 90; echo

echo "5) forged XFF claiming to be the bound IP (server logs would show real client as last hop) — expect 403:"
curl -s $SKIP -H "X-Forwarded-For: 8.8.8.8" "$NGROK/img/token" | head -c 90; echo

echo "--- log tail:"
grep -a "imghost" ${ZEN_DIR:-$HOME/Documents}/zen-proxy.log | tail -4
