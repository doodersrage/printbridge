# PrintBridge on Debian 13 (trixie) with PrusaSlicer from the Debian archive.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PRINTBRIDGE_DATA=/data \
    PRINTBRIDGE_SLICER=/usr/bin/prusa-slicer

RUN apt-get update \
 && apt-get install -y --no-install-recommends prusa-slicer python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/bin/python3
COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/bin/python3

RUN useradd --system --create-home --uid 1000 printbridge \
 && mkdir -p /data && chown printbridge /data
USER printbridge
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["/app/.venv/bin/printbridge"]
