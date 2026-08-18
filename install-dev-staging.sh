#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# RohTembak (XL) - dev/staging installer
# -----------------------------------------------------------------------------
# Installs the app into /opt/rohtembak, creates a venv, installs deps, and
# starts uvicorn on port 8000.
#
# This script is for DEVELOPMENT ONLY.
# Production containers use entrypoint.sh which handles install + restart.
#
# Usage:
#   wget -O install-dev-staging.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install-dev-staging.sh
#   chmod +x install-dev-staging.sh
#   ./install-dev-staging.sh
#
# Secrets: pass them as environment variables to run non-interactively, e.g.
#   API_KEY=... AES_KEY_ASCII=... ./install-dev-staging.sh
# or provide a ready-made env file:
#   ROHTEMBAK_ENV_FILE=/path/to/env ./install-dev-staging.sh
# =============================================================================

REPO_URL="https://github.com/rohcuan/rohtembak-xl"
REPO_BRANCH="main"
INSTALL_DIR="/opt/rohtembak"
APP_PORT="${APP_PORT:-8000}"

log() { echo -e "\033[1;32m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

# --- Parse args ---------------------------------------------------------------
START_APP=1
for arg in "$@"; do
    case "${arg}" in
        --no-start) START_APP=0 ;;
        --help|-h)
            echo "Usage: $0 [--no-start]"
            echo "  --no-start  Install code + venv only, do not start uvicorn."
            exit 0
            ;;
        *) warn "Unknown argument: ${arg}" ;;
    esac
done

# --- Rollback helper: clean up broken state so a re-run starts fresh. --------
_cleanup_on_fail() {
    local exit_code=$?
    if [[ "${exit_code}" -ne 0 ]]; then
        warn "Install failed (exit ${exit_code}). Cleaning up partial state..."
        rm -rf "${INSTALL_DIR}/venv" 2>/dev/null || true
        if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
            rm -rf "${INSTALL_DIR}" 2>/dev/null || true
        fi
        warn "Partial state cleaned. You can safely re-run install-dev-staging.sh."
    fi
}
trap _cleanup_on_fail EXIT

# --- 0. Checks -----------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        warn "Not root — re-running with sudo..."
        exec sudo "$0" "$@"
    fi
    die "Run as root (sudo)."
fi

command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This installer targets Debian/Ubuntu."

# --- 1. System dependencies ----------------------------------------------------
log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    python3-dev \
    git \
    curl \
    wget \
    ca-certificates \
    build-essential \
    pkg-config

# --- 2. Get the source ---------------------------------------------------------
if [[ -f "${INSTALL_DIR}/main.py" ]]; then
    log "Source already present at ${INSTALL_DIR}, pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only || warn "git pull failed; continuing with existing code."
else
    mkdir -p "$(dirname "${INSTALL_DIR}")"
    log "Cloning ${REPO_URL} ..."
    git clone -b "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

# --- 3. Python virtualenv + dependencies ---------------------------------------
log "Creating virtualenv and installing Python dependencies..."
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# --- 4. Configuration (.env) ---------------------------------------------------
if [[ -f "${INSTALL_DIR}/.env" ]]; then
    log "Keeping existing .env"
else
    cp .env.example .env

    SECRET_KEYS=(
        BASE_API_URL
        BASE_CIAM_URL
        BASIC_AUTH
        AX_DEVICE_ID
        AX_FP_KEY
        UA
        API_KEY
        AES_KEY_ASCII
        ENCRYPTED_FIELD_KEY
        XDATA_KEY
        AX_API_SIG_KEY
        X_API_BASE_SECRET
        CIRCLE_MSISDN_KEY
        JWT_SECRET
    )

    if [[ -n "${ROHTEMBAK_ENV_FILE:-}" ]]; then
        log "Using env file: ${ROHTEMBAK_ENV_FILE}"
        cp "${ROHTEMBAK_ENV_FILE}" "${INSTALL_DIR}/.env"
    elif [[ -n "${API_KEY:-}" ]]; then
        log "Filling secrets from environment variables..."
        for key in "${SECRET_KEYS[@]}"; do
            val="${!key:-}"
            if [[ -n "${val}" ]]; then
                sed -i "s|^${key}=.*|${key}=${val}|" "${INSTALL_DIR}/.env"
            fi
        done
    else
        if [[ -t 0 ]]; then
            log "Interactive secret setup. Press Enter to leave a value blank."
            for key in "${SECRET_KEYS[@]}"; do
                current="$(grep -E "^${key}=" "${INSTALL_DIR}/.env" | cut -d= -f2-)"
                read -r -p "  ${key} [${current}]: " value
                if [[ -n "${value}" ]]; then
                    sed -i "s|^${key}=.*|${key}=${value}|" "${INSTALL_DIR}/.env"
                fi
            done
        else
            warn "No TTY - skipping interactive secret setup. Fill ${INSTALL_DIR}/.env manually."
        fi
    fi
    log "Wrote ${INSTALL_DIR}/.env"
fi

# --- 5. Start uvicorn ----------------------------------------------------------
if [[ "${START_APP}" -eq 1 ]]; then
    log "Starting uvicorn..."
    cd "${INSTALL_DIR}"

    # Kill any existing uvicorn on this port
    if command -v fuser >/dev/null 2>&1; then
        old_pids=$(fuser "${APP_PORT}"/tcp 2>/dev/null | tr -s ' ' || true)
        for pid in $old_pids; do
            kill "$pid" 2>/dev/null || true
        done
    elif command -v lsof >/dev/null 2>&1; then
        kill $(lsof -ti :"${APP_PORT}" 2>/dev/null) 2>/dev/null || true
    fi
    sleep 1

    setsid venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port "${APP_PORT}" > /tmp/rohtembak.log 2>&1 &
    UVICORN_PID=$!
    sleep 3
    if kill -0 "${UVICORN_PID}" 2>/dev/null; then
        log "Uvicorn running (PID ${UVICORN_PID}) on port ${APP_PORT}."
    else
        warn "Uvicorn may have failed to start. Check: tail /tmp/rohtembak.log"
    fi
else
    log "Skipping uvicorn start (--no-start)."
fi

# --- 6. Fix ownership ----------------------------------------------------------
REAL_USER="${SUDO_USER:-$(whoami)}"
if [[ "${REAL_USER}" != "root" ]]; then
    log "Fixing ownership → ${REAL_USER} ..."
    chown -R "${REAL_USER}:" "${INSTALL_DIR}"
fi

# --- 7. Summary ----------------------------------------------------------------
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
log "Done!"
echo
echo "  URL        : http://${IP:-localhost}:${APP_PORT}/"
echo "  Admin login: admin / admin"
echo "  Logs       : tail -f /tmp/rohtembak.log"
echo "  Stop       : kill \$(lsof -ti :${APP_PORT})"
echo "  Restart    : cd ${INSTALL_DIR} && setsid venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${APP_PORT} > /tmp/rohtembak.log 2>&1 &"
