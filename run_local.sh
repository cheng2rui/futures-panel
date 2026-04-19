#!/bin/bash
# Futures Panel 本地启动脚本（macOS / Linux 直接运行）
# 用法: ./run_local.sh

set -e

PORT=8318
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/futures-panel.pid"
LOGFILE="$DIR/futures-panel.log"

start() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "服务已在运行 (PID: $(cat $PIDFILE))"
        exit 1
    fi
    echo "启动期货面板 on http://localhost:$PORT ..."
    cd "$DIR"
    python3 app.py --port $PORT > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "PID: $(cat $PIDFILE)"
    echo "日志: $LOGFILE"
}

stop() {
    if [ ! -f "$PIDFILE" ]; then
        echo "服务未运行"
        exit 1
    fi
    kill "$(cat "$PIDFILE")" && rm "$PIDFILE"
    echo "已停止"
}

status() {
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        echo "运行中 (PID: $(cat $PIDFILE))"
    else
        echo "未运行"
    fi
}

case "$1" in
    start) start ;;
    stop)  stop  ;;
    restart) stop; sleep 1; start ;;
    status) status ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
