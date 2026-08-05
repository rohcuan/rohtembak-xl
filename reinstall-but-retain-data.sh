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
#                       tokens, transactions, fees)
#   - ax.fp / *.fp      (device fingerprint - tied to XL refresh tokens)
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
INSTALL_SH_URL="https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install.sh"
RETAIN_ITEMS=(.env data ax.fp "*.fp")
BACKUP_DIR="$(mktemp -d /tmp/rohtembak-backup.XXXXXX)"

log() { echo -e "\033[1;32m[reinstall]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

# --- 0. Checks ---------------------------------------------------------------
[[ "${EUID}" -eq 0 ]] || die "Run as root (sudo)."
command -v apt-get >/dev/null 2>&1 || die "apt-get not found. This script targets Debian/Ubuntu."

# If run from inside the install dir, relaunch from a temp location so the
# directory can be wiped safely while this script is still executing.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${SCRIPT_DIR}" == "${INSTALL_DIR}"* ]]; then
    TMP_SELF="$(mktemp /tmp/reinstall-self.XXXXXX.sh)"
    cp "${BASH_SOURCE[0]}" "${TMP_SELF}"
    chmod +x "${TMP_SELF}"
    warn "Running from ${INSTALL_DIR} - relaunching from ${TMP_SELF}..."
    exec "${TMP_SELF}" "$@"
fi

# --- 1. Stop service ---------------------------------------------------------
if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    log "Stopping ${SERVICE_NAME} service..."
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
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
if command -v curl >/dev/null 2>&1; then
    curl -fsSL -o install.sh "${INSTALL_SH_URL}" || die "Failed to download install.sh"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O install.sh "${INSTALL_SH_URL}" || die "Failed to download install.sh"
else
    die "Neither curl nor wget available."
fi
chmod +x install.sh
./install.sh
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

# --- 6. Restart service -------------------------------------------------------
log "Restarting ${SERVICE_NAME} service..."
systemctl restart "${SERVICE_NAME}"

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    log "Service ${SERVICE_NAME} is running with retained data."
else
    warn "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
fi

rm -rf "${BACKUP_DIR}"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
log "Done! App: http://${IP:-localhost}:8000/"
