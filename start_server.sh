#!/bin/bash
cd /Users/rey/.openclaw/workspace/futures-panel
python3 -B app.py --port 8318 >> /tmp/fp_v4.log 2>&1 &
echo "Started PID $!"
sleep 3
lsof -i:8318 | grep LISTEN
