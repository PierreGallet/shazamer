# ── Stage 1: build the frontend ──────────────────────────────────────────
FROM node:22-alpine AS web

WORKDIR /web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY web/ ./
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
COPY --from=web /web/dist ./web/dist

# uploads/media hold audio; data holds the SQLite library; tmp holds task state.
RUN mkdir -p uploads media data tmp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health').read()"

CMD ["python", "-m", "uvicorn", "src.web:app", "--host", "0.0.0.0", "--port", "8000"]
