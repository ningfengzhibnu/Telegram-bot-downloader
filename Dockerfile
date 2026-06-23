FROM python:3.11-slim-bookworm

# 安装 netcat（用于等待代理端口就绪）
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
RUN pip install --no-cache-dir telethon qrcode[pil] python-socks

WORKDIR /app

# 复制启动脚本和 bot 代码
COPY entrypoint.sh /entrypoint.sh
COPY bot.py /app/bot.py
# session.session 由用户通过 volume 挂载，不打包进镜像

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
