FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.13-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
RUN uv sync --frozen --no-dev --no-editable && useradd --uid 10001 --create-home memory
USER memory
EXPOSE 8765
CMD ["memory", "serve"]
