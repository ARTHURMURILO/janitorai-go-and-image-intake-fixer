#!/bin/bash
umask 077   # bridge log/session files are owner-only
# Zen proxy production starter — kills old instances, starts the bridge on 8081.
#
# IMAGE_PUBLIC_HOST is the publicly reachable URL of this bridge (used to build
# image links at upload time). Set your own tunnel/domain here, or drop it into
# start-zen-proxy.local.sh (gitignored) so your real hostname never lands in git.
if [ -f "$(dirname "$0")/start-zen-proxy.local.sh" ]; then
  . "$(dirname "$0")/start-zen-proxy.local.sh"
fi
export IMAGE_PUBLIC_HOST="${IMAGE_PUBLIC_HOST:-https://YOUR-TUNNEL.ngrok-free.dev}"

ZEN_DIR="${ZEN_DIR:-$HOME/Documents}"
pkill -f 'zen-cors-proxy' 2>/dev/null
sleep 2
nohup python3 "$ZEN_DIR/zen-cors-proxy.py" 8081 > "$ZEN_DIR/zen-proxy.log" 2>&1 &
echo "started pid $!"
sleep 3
curl -s http://127.0.0.1:8081/healthz; echo
echo "--- log tail ---"
tail -n 8 "$ZEN_DIR/zen-proxy.log"
