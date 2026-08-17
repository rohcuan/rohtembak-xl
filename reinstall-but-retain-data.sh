#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# RohTembak (XL) - reinstall that RETAINS runtime data
# -----------------------------------------------------------------------------
# For big repo changes: wipes /opt/rohtembak completely (code + venv + old
# systemd unit), reinstalls from the repo from scratch, then restores your
# runtime data so nothing is lost:
#   - .env              (configuration)
#   - data/             (SQLite DB: users, balances, XL accounts, refresh
#                       tokens, transactions, fees, per-user fingerprints)
#
# This is NOT a git pull - it is a clean reinstall.
#
# Can be run from any working directory (e.g. your home dir) - no need to
# cd into the app folder first:
#
#   wget -O reinstall.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/reinstall-but-retain-data.sh
#   chmod +x reinstall.sh
#   bash reinstall.sh
#
# (If you run it from inside the app directory it relaunches itself from
# /tmp automatically so the folder can be wiped safely.)
# =============================================================================

REPO_URL="https://github.com/rohcuan/rohtembak-xl"
REPO_BRANCH="main"
INSTALL_DIR="/opt/rohtembak"
SERVICE_NAME="rohtembak"
APP_PORT="${APP_PORT:-8000}"
INSTALL_SH_URL="https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install.sh"
RETAIN_ITEMS=(.env data ax.fp)
BACKUP_DIR="$(mktemp -d /tmp/rohtembak-backup.XXXXXX)"

# --- Detect container environment -----------------------------------------------
# In containers (docker, podman, distrobox) there is no systemd. The script must
# use --no-systemd for install.sh and start uvicorn directly instead of systemctl.
IS_CONTAINER=0
if [[ -f /.dockerenv ]] || [[ -f /run/.containerenv ]]; then
    IS_CONTAINER=1
elif grep -qE 'docker|podman|containerd|distrobox' /proc/1/cgroup 2>/dev/null; then
    IS_CONTAINER=1
fi

log() { echo -e "\033[1;32m[reinstall]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

# --- 0. Checks ---------------------------------------------------------------
# In containers the user is often non-root but has passwordless sudo.
# Only require EUID==0 on bare metal.
if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
        warn "Not root - re-running with sudo..."
        exec sudo "$0" "$@"
    fi
    die "Run as root (sudo)."
fi
command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This script targets Debian/Ubuntu."

# If run from inside the install dir, cd out first so the directory can be
# wiped safely. Linux won't let you rm -rf a directory that is any process's
# current working directory ("device or resource busy").
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${SCRIPT_DIR}" == "${INSTALL_DIR}"* ]]; then
    warn "Running from ${INSTALL_DIR} - changing to / ..."
    cd /
fi

# --- 1. Stop service / uvicorn ------------------------------------------------
if [[ "${IS_CONTAINER}" -eq 1 ]]; then
    log "Stopping uvicorn (container mode)..."
    # Find and kill uvicorn by port. Use fuser to get PIDs, then kill
    # individually — avoids fuser -k sending signals to our own process group.
    if command -v fuser >/dev/null 2>&1; then
        uvicorn_pids=$(fuser "${APP_PORT}"/tcp 2>/dev/null | tr -s ' ')
        for pid in $uvicorn_pids; do
            kill "$pid" 2>/dev/null || true
        done
    elif command -v lsof >/dev/null 2>&1; then
        kill $(lsof -ti :"${APP_PORT}") 2>/dev/null || true
    fi
    sleep 2
else
    if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
        log "Stopping ${SERVICE_NAME} service..."
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    fi
fi

# --- 2. Backup runtime data ---------------------------------------------------
if [[ -d "${INSTALL_DIR}" ]]; then
    log "Backing up runtime data to ${BACKUP_DIR}..."
    for item in "${RETAIN_ITEMS[@]}"; do
        for src in "${INSTALL_DIR}"/${item}; do
            if [[ -e "${src}" ]]; then
                cp -a "${src}" "${BACKUP_DIR}/"
                log "  saved ${src##*/}"
            fi
        done
    done
else
    warn "${INSTALL_DIR} not found - nothing to back up (fresh install)."
fi

# --- 3. Wipe old install ------------------------------------------------------
if [[ -d "${INSTALL_DIR}" ]]; then
    log "Removing old install at ${INSTALL_DIR}..."
    rm -rf "${INSTALL_DIR}"
fi

# --- 4. Reinstall from repo ---------------------------------------------------
log "Downloading install.sh from ${INSTALL_SH_URL}..."
cd /tmp
rm -f install.sh
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o install.sh "${INSTALL_SH_URL}" || die "Failed to download install.sh"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O install.sh "${INSTALL_SH_URL}" || die "Failed to download install.sh"
else
    die "Neither curl nor wget available."
fi
chmod +x install.sh

if [[ "${IS_CONTAINER}" -eq 1 ]]; then
    log "Running install.sh --no-systemd (container mode)..."
    ./install.sh --no-systemd
else
    ./install.sh
fi
rm -f /tmp/install.sh

# --- 5. Restore runtime data --------------------------------------------------
log "Restoring runtime data..."
    for item in "${RETAIN_ITEMS[@]}"; do
        for src in "${BACKUP_DIR}"/${item}; do
            if [[ -e "${src}" ]]; then
                cp -a "${src}" "${INSTALL_DIR}/"
                log "  restored ${src##*/}"
            fi
        done
    done

# --- 6. Start service / uvicorn -----------------------------------------------
if [[ "${IS_CONTAINER}" -eq 1 ]]; then
    log "Starting uvicorn (container mode)..."
    cd "${INSTALL_DIR}"
    rm -f /tmp/rohtembak.log
    setsid venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port "${APP_PORT}" > /tmp/rohtembak.log 2>&1 &
    sleep 3
    if curl -s -o /dev/null -w '' http://localhost:"${APP_PORT}"/login 2>/dev/null; then
        log "App is running on port ${APP_PORT}."
    else
        warn "App may not have started. Check: tail /tmp/rohtembak.log"
    fi
else
    log "Restarting ${SERVICE_NAME} service..."
    systemctl restart "${SERVICE_NAME}"
    sleep 2
    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        log "Service ${SERVICE_NAME} is running with retained data."
    else
        warn "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
    fi
fi

rm -rf "${BACKUP_DIR}"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
log "Done! App: http://${IP:-localhost}:8000/"
