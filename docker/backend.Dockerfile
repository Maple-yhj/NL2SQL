FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip wheel . --wheel-dir /wheels


FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Data Agent 后端" \
      org.opencontainers.image.description="可治理数据分析代理的 FastAPI 服务" \
      org.opencontainers.image.source="https://github.com/Maple-yhj/NL2SQL" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_AGENT_STATE_DIR=/var/lib/data-agent

RUN groupadd --system --gid 10001 data-agent \
    && useradd --system --uid 10001 --gid data-agent \
        --home-dir /home/data-agent --create-home data-agent

COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index \
        --find-links=/wheels data-agent \
    && rm -rf /wheels

WORKDIR /app

COPY db/auth.sql ./db/auth.sql
COPY docker/entrypoint.py ./docker/entrypoint.py

RUN mkdir -p /var/lib/data-agent \
    && chown -R data-agent:data-agent /var/lib/data-agent

USER 10001:10001

EXPOSE 8000

ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
