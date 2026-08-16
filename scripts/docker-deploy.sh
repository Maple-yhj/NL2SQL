#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
environment_file="${1:-$project_root/.env.docker}"
case "$environment_file" in
    /*) ;;
    *) environment_file="$project_root/$environment_file" ;;
esac

cd "$project_root"

if [ ! -f "$environment_file" ]; then
    echo "未找到 $environment_file。" >&2
    echo "请先复制 .env.docker.example，并填写其中的必需配置。" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker，请先安装 Docker Engine 或 Docker Desktop。" >&2
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "未找到 Docker Compose v2，请先安装或升级 Docker。" >&2
    exit 1
fi

DATA_AGENT_ENV_FILE="$environment_file" \
    docker compose --env-file "$environment_file" \
    up --build --detach --wait --wait-timeout 300

echo "Data Agent 已启动。"
echo "可使用 docker compose --env-file $environment_file ps 查看状态。"
