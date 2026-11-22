# One container: the website, the API and the Stockfish analysis worker.
# Runs on Render's free plan (512 MB, so one engine) or anywhere Docker does.

FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CHESS_REVIEW_DATA_DIR=/data STOCKFISH_PATH=/opt/stockfish/stockfish \
    FRONTEND_DIST=/app/frontend/dist WORKER_THREADS=1 PORT=7860
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates tar \
    && rm -rf /var/lib/apt/lists/*

# Analysis version v1 is defined for Stockfish 18 (official release build). The last line
# fails the build if the binary does not run or reports a different version.
RUN mkdir -p /opt/stockfish /tmp/sf && cd /tmp/sf \
    && curl -fsSL -o sf.tar https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar \
    && tar -xf sf.tar \
    && BIN="$(find /tmp/sf -type f -name 'stockfish-ubuntu-x86-64-avx2*' | head -n 1)" \
    && test -n "$BIN" && mv "$BIN" /opt/stockfish/stockfish && chmod +x /opt/stockfish/stockfish \
    && rm -rf /tmp/sf \
    && echo quit | /opt/stockfish/stockfish | grep -q 'Stockfish 18'

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend/app backend/app
COPY --from=web /web/dist frontend/dist
COPY start.sh /app/start.sh

# Hugging Face runs containers as user 1000.
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app /data /app && chmod +x /app/start.sh
USER app
EXPOSE 7860
CMD ["/app/start.sh"]
