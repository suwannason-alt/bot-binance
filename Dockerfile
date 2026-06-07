# =============================================================================
#  BTCUSDT Futures Trading Bot — Dockerfile
#  Multi-stage build: compiler toolchain stays in the builder layer only,
#  keeping the final runtime image as small as possible.
#
#  Persistent data (volumes — do NOT bake into the image):
#    /app/state   ← StateManager SQLite DB  (named volume: bot_state)
#    /app/data    ← OHLCV CSV cache         (named volume: market_data)
# =============================================================================


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 1 — builder
#  Installs Python packages inside an isolated venv.
#  gcc / libffi-dev are needed to compile C-extension wheels (numpy, aiohttp).
#  They do NOT end up in the runtime image.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12.7-slim-bullseye AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gcc \
      libffi-dev \
 && rm -rf /var/lib/apt/lists/*

# Isolated virtual environment — copied intact into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
 && pip install --no-cache-dir -r /tmp/requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
#  Stage 2 — runtime
#  Clean base image + pre-built venv from stage 1 + application source only.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12.7-slim-bullseye AS runtime

# Runtime-only system packages:
#   libffi8      — required by aiohttp / cffi at runtime
#   ca-certificates — TLS trust store for Binance HTTPS / WSS endpoints
#   tzdata       — IANA timezone database (we pin TZ=UTC below)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libffi8 \
      ca-certificates \
      tzdata \
 && ln -sf /usr/share/zoneinfo/UTC /etc/localtime \
 && echo "UTC" > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

# ── Copy compiled packages from builder ──────────────────────────────────────
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# ── Python runtime flags ──────────────────────────────────────────────────────
# PYTHONUNBUFFERED — flush stdout/stderr immediately so live trading logs and
#                    WFO hydration output appear in `docker compose logs` in
#                    real time instead of being buffered until process exit.
# PYTHONDONTWRITEBYTECODE — skip .pyc generation; saves a tiny bit of space.
# TZ — hard-pin the container clock to UTC so Binance timestamp handshakes
#      and funding-rate windows are always correctly aligned.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

# ── Non-root user (security best practice) ───────────────────────────────────
RUN useradd --system --create-home --uid 1001 --shell /bin/bash botuser

WORKDIR /app

# ── Application source ────────────────────────────────────────────────────────
# Modular layout: copy the entry point + the two runtime package dirs.
# (`tests/` and `scripts/` are NOT needed at runtime → excluded to keep the image
#  lean; main.py's sys.path shim tolerates the absent scripts/ dir.)
# .dockerignore still excludes .env, data/, __pycache__, etc.
COPY main.py ./
COPY src ./src
COPY backtesting ./backtesting

# ── Persistent mount points ───────────────────────────────────────────────────
# Create the directories now (owned by botuser) so Docker can overlay named
# volumes on top of them without permission errors on first run.
RUN mkdir -p /app/state /app/data \
 && chown -R botuser:botuser /app

# Declare the two persistent paths as volume mount points.
# docker-compose.yml maps named volumes here so data survives container
# rebuilds, image upgrades, and unexpected crashes.
VOLUME ["/app/state", "/app/data"]

USER botuser

# ── Entrypoint ────────────────────────────────────────────────────────────────
# WFO is ON by default (see config.py / main.py _parse_args).
# Override via docker-compose `command:` or `docker run` trailing args:
#   docker run binance-bot --no-wfo       → classic fixed BREAKOUT_PERIOD=14
#   docker run binance-bot --forecast     → WFO + Markov regime forecast
ENTRYPOINT ["python", "main.py"]
CMD []
