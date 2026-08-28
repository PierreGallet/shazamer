# ── Stage 1: build the frontend ──────────────────────────────────────────
FROM node:22-alpine AS web

WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── Stage 1b: build the documentation ────────────────────────────────────
# Its own stage rather than a step in the frontend one: separate dependency
# tree, and a docs-only change then reuses the frontend layer untouched.
FROM node:22-alpine AS docs

WORKDIR /docs
COPY docs-site/package.json docs-site/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY docs-site/ ./
RUN npm run build

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1 \
    PATH="/root/.local/bin:$PATH"

# ffmpeg does every decode, seek and probe in the pipeline — it is the one
# hard system dependency. deno backs yt-dlp's remote JS components.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates unzip \
 && curl -LsSf https://astral.sh/uv/install.sh | sh \
 && curl -fsSL https://deno.land/install.sh | sh \
 && mv /root/.deno/bin/deno /usr/local/bin/ \
 && apt-get purge -y curl unzip \
 && apt-get autoremove -y && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* /root/.deno

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

COPY src/ ./src/
# Read at runtime by /api/health, so the deployed version is knowable.
COPY .release-please-manifest.json ./
# The whole directory, not just the health check: admin scripts are only
# useful if they are present when they are needed, and copying one in by
# hand mid-incident is how a step gets skipped.
COPY scripts/ ./scripts/
COPY scripts/healthcheck.py ./
COPY --from=web /web/dist ./web/dist
# Mounted at /docs by the app, so the docs ship with the version they
# describe instead of drifting on a separate Pages deploy.
COPY --from=docs /docs/build ./docs-site/build

# uploads/media hold audio; data holds the SQLite library; tmp holds task state.
RUN mkdir -p uploads media data tmp

EXPOSE 8000

# Cheap probe: a raw socket under `python -S` skips urllib and site imports,
# which cost more than the request itself and are paid from the same CPU quota
# the analysis is using. Tolerances match docker-stack.yml.
HEALTHCHECK --interval=30s --timeout=20s --start-period=40s --retries=5 \
  CMD ["python", "-S", "/app/healthcheck.py"]

CMD ["python", "-m", "uvicorn", "src.web:app", "--host", "0.0.0.0", "--port", "8000"]
