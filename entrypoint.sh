#!/bin/bash
set -e

echo "=== TG Bot 容器启动 ==="

# 等待 Clash 代理端口就绪
PROXY_HOST="${TG_PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${TG_PROXY_PORT:-7890}"
echo "[*] 等待代理端口 ${PROXY_HOST}:${PROXY_PORT} 就绪..."
for i in $(seq 1 60); do
    if nc -z ${PROXY_HOST} ${PROXY_PORT} 2>/dev/null; then
        echo "[ok] 代理已就绪"
        break
    fi
    if [ $i -eq 60 ]; then
        echo "[warn] 代理未就绪，仍尝试启动 bot"
        break
    fi
    sleep 1
done

echo "[+] 启动 Telegram 下载机器人..."
cd /app
exec python3 bot.py
