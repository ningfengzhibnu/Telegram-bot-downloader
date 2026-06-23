#!/bin/bash
# 构建并启动 TG 下载机器人
# 使用方法：
#   1. 确保 docker-compose.yml 和 .env 已就绪
#   2. 运行：bash build.sh
#
# 前置条件：
#   - 已安装 Docker
#   - .env 文件中已配置 TG_API_ID 和 TG_API_HASH

set -e

IMAGE_NAME="tg-bot-downloader"
IMAGE_TAG="latest"

echo "=== 构建 TG Bot 镜像 ==="
echo ""

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装或不在 PATH 中"
    exit 1
fi

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 文件，请先复制 .env.example 并填入配置："
    echo "   cp .env.example .env"
    echo "   然后编辑 .env 填入你的 TG_API_ID 和 TG_API_HASH"
    exit 1
fi

# 检查 session.session 是否存在（首次运行会自动生成）
if [ ! -f "session.session" ]; then
    echo "ℹ️  未找到 session.session，首次运行将通过 QR 码登录"
    echo "   容器启动后查看下载目录的 tg-bot-login-qr.png 扫码登录"
fi

echo "[1/2] 构建 Docker 镜像..."
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .

echo ""
echo "✅ 构建完成！"
echo ""
echo "[2/2] 启动容器..."
# 兼容 Docker Compose V1 (docker-compose) 和 V2 (docker compose)
if command -v docker-compose &> /dev/null; then
    docker-compose up -d
elif docker compose version &> /dev/null; then
    docker compose up -d
else
    echo "❌ 未找到 docker-compose 或 docker compose，请手动启动容器"
    echo "   docker run -d --name tg-bot --network host \\"
    echo "     -v \$(pwd)/session.session:/app/session.session \\"
    echo "     -v \$(pwd)/downloads:/downloads \\"
    echo "     --env-file .env \\"
    echo "     ${IMAGE_NAME}:${IMAGE_TAG}"
    exit 1
fi

echo ""
echo "✅ 容器已启动！"
echo "   查看日志：docker logs -f tg-bot"
echo "   QR 码位置（首次登录时）：./downloads/tg-bot-login-qr.png"
