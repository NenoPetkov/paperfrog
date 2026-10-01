FROM python:3.12.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy TZ=UTC
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 10001 paperfrog && useradd --uid 10001 --gid 10001 --create-home paperfrog
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev && mkdir -p state data artifacts reports logs && chown -R 10001:10001 state data artifacts reports logs
COPY config ./config
USER 10001:10001
ENV PATH="/app/.venv/bin:$PATH"
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)"]
ENTRYPOINT ["paperfrog"]
CMD ["run", "paper"]
