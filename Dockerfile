FROM python:3.11-slim-bookworm

# 安装 netcat（entrypoint.sh 用 nc -z 检测代理端口）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
RUN pip install --no-cache-dir \
    telethon \
    qrcode[pil] \
    python-socks \
    aiohttp

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

COPY bot.py /app/bot.py

ENTRYPOINT ["/entrypoint.sh"]
