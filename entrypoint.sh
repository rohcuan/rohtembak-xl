#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# RohTembak (XL) - container entrypoint (systemd-less)
# -----------------------------------------------------------------------------
# Runs the app as PID 1. If nothing is installed yet (fresh container), it
# bootstraps a full install via install.sh --no-systemd, then starts uvicorn.
# On subsequent starts it just pulls the latest code and runs.
#
# Persist /opt/rohtembak (code, venv, .env, data/, ax.fp) in a named volume so
# the install only happens once and data survives container recreation.
# =============================================================================

INSTALL_DIR="${INSTALL_DIR:-/opt/rohtembak}"
APP_PORT="${APP_PORT:-8000}"

# --- 1. Minimal bootstrap: bare Debian 12 images have neither curl nor git. ---
if ! command -v curl >/dev/null 2>&1; then
    echo "[entrypoint] installing curl..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null
    apt-get install -y curl >/dev/null
fi

# --- 2. Fresh install? Bootstrap via install.sh --no-systemd. ------------------
if [ ! -x "${INSTALL_DIR}/venv/bin/python" ] || [ ! -f "${INSTALL_DIR}/main.py" ]; then
    echo "[entrypoint] fresh install detected - running install.sh --no-systemd"
    curl -fsSL https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install.sh -o /tmp/install.sh
    chmod +x /tmp/install.sh
    INSTALL_DIR="${INSTALL_DIR}" APP_PORT="${APP_PORT}" bash /tmp/install.sh --no-systemd
    rm -f /tmp/install.sh
else
    echo "[entrypoint] app already installed - pulling latest code..."
    git -C "${INSTALL_DIR}" pull --ff-only 2>/dev/null || echo "[entrypoint] git pull failed; starting with existing code."
fi

# --- 3. Wait for real secrets before starting (no crash-loop). -----------------
if [ ! -f "${INSTALL_DIR}/.env" ] || ! grep -qE "^BASE_CIAM_URL=.+" "${INSTALL_DIR}/.env"; then
    echo "[entrypoint] .env belum berisi secret (BASE_CIAM_URL kosong)."
    echo "[entrypoint] Letakkan .env asli, mis.: docker cp .env rohtembak:/opt/rohtembak/.env"
    echo "[entrypoint] lalu: docker restart rohtembak"
    echo "[entrypoint] Menunggu .env... (container tetap hidup, bukan crash-loop)"
    sleep infinity
fi

# --- 4. Run the app as PID 1. ---------------------------------------------------
cd "${INSTALL_DIR}"
exec "${INSTALL_DIR}/venv/bin/python" -m uvicorn main:app --host 0.0.0.0 --port "${APP_PORT}"
