#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# RohTembak (XL) - one-command installer for Debian 12
# -----------------------------------------------------------------------------
# Deploys the app into /opt/rohtembak, installs dependencies, and runs it as a
# systemd service (rohtembak.service) on port 8000.
#
# Usage:
#   wget -O install.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install.sh
#   chmod +x install.sh
#   ./install.sh
#
# Secrets: pass them as environment variables to run non-interactively, e.g.
#   API_KEY=... AES_KEY_ASCII=... ./install.sh
# or provide a ready-made env file:
#   ROHTEMBAK_ENV_FILE=/path/to/env ./install.sh
# =============================================================================

REPO_URL="https://github.com/rohcuan/rohtembak-xl"
REPO_BRANCH="main"
INSTALL_DIR="/opt/rohtembak"
SERVICE_NAME="rohtembak"
APP_PORT="${APP_PORT:-8000}"

log() { echo -e "\033[1;32m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

# --no-systemd: install code + venv only, skip the systemd unit. Used by the
# container entrypoint (entrypoint.sh) where the runtime handles restart/logs.
INSTALL_SYSTEMD=1
for arg in "$@"; do
    case "${arg}" in
        --no-systemd) INSTALL_SYSTEMD=0 ;;
        --help|-h)
            echo "Usage: $0 [--no-systemd]"
            echo "  --no-systemd  Skip systemd service creation (container entrypoint mode)."
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
        # Remove broken venv so re-run recreates it from scratch
        rm -rf "${INSTALL_DIR}/venv" 2>/dev/null || true
        # Remove incomplete clone if main.py never appeared
        if [[ ! -f "${INSTALL_DIR}/main.py" ]]; then
            rm -rf "${INSTALL_DIR}" 2>/dev/null || true
        fi
        warn "Partial state cleaned. You can safely re-run install.sh."
    fi
}
trap _cleanup_on_fail EXIT

# --- 0. Checks -----------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    die "Run as root (sudo)."
fi

command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This installer targets Debian/Ubuntu."

# --- 1. System dependencies ----------------------------------------------------
log "Installing system dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# Redundancy is intentional: every package the app/runtime may need is listed,
# even those that usually come pre-installed on a Debian 12 image.
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

    # Secret keys that must be filled in. Non-secret keys already have sane
    # defaults in .env.example.
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

# --- 5. systemd service (skipped with --no-systemd) ---------------------------
if [[ "${INSTALL_SYSTEMD}" -eq 1 ]]; then
    log "Installing systemd service..."
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=RohTembak (XL) WebUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${APP_PORT}
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" --now

    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log "Service ${SERVICE_NAME} is running."
    else
        warn "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
    fi
else
    log "Skipping systemd service (--no-systemd). The container entrypoint will run the app."
fi

# --- 6. Fix ownership ----------------------------------------------------------
# When run via sudo, files are owned by root. Fix so the real user can write
# data/, ax.fp, .env etc. without needing sudo every time.
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
if [[ "${INSTALL_SYSTEMD}" -eq 1 ]]; then
    echo "  Admin login: admin / admin"
    echo "  Logs       : journalctl -u ${SERVICE_NAME} -f"
    echo "  Restart    : systemctl restart ${SERVICE_NAME}"
else
    echo "  Logs       : podman logs <container>  (or docker logs)"
    echo "  Restart    : podman restart <container>"
fi
