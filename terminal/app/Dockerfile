FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY frontend ./frontend
COPY scripts/build.mjs ./scripts/build.mjs
COPY dist ./dist
RUN npm run build

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TERMINAL_DATA=/data TERMINAL_WORKSPACE=/workspace TERMINAL_RUNTIME=docker TERMINAL_UID=1000 TERMINAL_GID=1000 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends bash git curl nano procps nodejs npm ca-certificates && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 operators && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash operator \
    && mkdir -p /data /workspace && chmod 700 /data && chown 1000:1000 /workspace
WORKDIR /app
COPY requirements.txt ./
COPY vendor ./vendor
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps --only-shell chromium && chmod -R a+rX /ms-playwright
COPY terminal_app ./terminal_app
COPY scripts ./scripts
RUN chmod +x /app/scripts/terminal-browser && ln -s /app/scripts/terminal-browser /usr/local/bin/terminal-browser && printf "/app\n" > /usr/local/lib/python3.12/site-packages/terminal-workspace.pth
COPY --from=frontend /src/dist ./dist
EXPOSE 8765 8081
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=2)"
CMD ["uvicorn", "terminal_app.app:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "1"]
