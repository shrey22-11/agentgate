# --- Stage 1: build the React SPA -------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: Python runtime -------------------------------------------------
# Debian slim, never Alpine — decided in Section L (Alpine's musl libc has
# repeatedly caused subtle wheel-compatibility issues for Python packages
# with C extensions; not worth the smaller image size here).
FROM python:3.12-slim AS runtime
WORKDIR /app/backend

# NOTE: backend/ and frontend/ are kept as siblings under /app, exactly as
# they are in the repo root locally. app/main.py resolves the SPA path as
# parent.parent.parent/frontend/dist relative to itself — that relationship
# only holds if this layout matches the local one. Don't "flatten" this.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY backend/ .
COPY --from=frontend-build /frontend/dist /app/frontend/dist

# Normalise line endings (a CRLF checkout on Windows would break `/bin/sh`),
# then make the entrypoint executable.
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1
# Informational: platforms that inject $PORT (Render/Railway) override this.
ENV PORT=8000
EXPOSE 8000

# Liveness for `docker run` / compose. Managed platforms use their own HTTP
# probe against healthCheckPath instead (see render.yaml / fly.toml).
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health').read()" || exit 1

# entrypoint.sh runs `alembic upgrade head` then execs uvicorn on $PORT.
CMD ["./entrypoint.sh"]
