# Workflow Controller 後端
# 注意：容器內沒有 openclaw 指令。要用 OpenClawCliDispatcher 派工時，請直接在主機上執行
# （見 README「部署」），或設定 FLOW_CONTROLLER_DISPATCHER=fake 只跑 API。
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    FLOW_CONTROLLER_DB_URL=sqlite:////data/controller.db

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 1000 controller && mkdir -p /data && chown controller /data
USER controller
VOLUME ["/data"]
EXPOSE 18090

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18090/healthz', timeout=3)" || exit 1

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "18090", "--workers", "1"]
