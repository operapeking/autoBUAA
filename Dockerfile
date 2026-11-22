FROM node:22-alpine AS frontend-builder

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN python -c "import tomllib, subprocess, sys; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--no-cache-dir', *deps])"

COPY backend ./backend
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

RUN groupadd --gid 1000 autobuaa \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin autobuaa \
    && mkdir -p /data \
    && chown -R autobuaa:autobuaa /app /data

ENV AUTOBUAA_DATA_DIR=/data \
    AUTOBUAA_PORT=8080 \
    AUTOBUAA_FRONTEND_DIST_DIR=/app/frontend/dist \
    AUTOBUAA_TIMEZONE=Asia/Shanghai \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

USER autobuaa

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=3)"

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8080"]
