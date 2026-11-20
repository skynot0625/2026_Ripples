#!/bin/bash
cd "$(dirname "$0")"
for f in run/browser.pid run/server.pid run/sender.pid; do
  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null
  rm -f "$f"
done
echo "종료함"
