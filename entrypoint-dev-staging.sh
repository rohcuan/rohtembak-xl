#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# RohTembak (XL) - dev/staging container entrypoint
# -----------------------------------------------------------------------------
# Single-file entrypoint: bootstraps deps, clones/pulls the DEV branch,
# prepares venv + .env, translates stale main-branch links to dev,
# waits for real secrets, then runs uvicorn as PID 1.
#
# Replaces the old trio (entrypoint.sh + install-dev-staging.sh +
# reinstall-dev-staging.sh). Re-running it on an existing volume behaves
# like "reinstall-lite": pull latest, re-sync deps, restart app.
#
# Environment:
#   INSTALL_DIR          target dir                     (default /opt/rohtembak)
#   APP_PORT             uvicorn port                   (default 8000)
#   ROHTEMBAK_ENV_FILE   optional .env to copy over     (host path inside container)
#   Any SECRET_KEY name  fills blank values into .env   (e.g. API_KEY=...)
#
# Persist ${INSTALL_DIR} (code, venv, .env, data/) in a named volume.
# =============================================================================

REPO_URL="https://github.com/rohcuan/rohtembak-xl"
REPO_BRANCH="dev"
INSTALL_DIR="${INSTALL_DIR:-/opt/rohtembak}"
APP_PORT="${APP_PORT:-8000}"

log()  { echo -e "\033[1;32m[entrypoint]\033[0m $*"; }
warn() { echo -e "\033[1;33m[entrypoint]\033[0m $*"; }

SECRET_KEYS=(
    BASE_API_URL
    BASE_CIAM_URL
    BASIC_AUTH
    AX_FP_KEY
    UA
    API_KEY
    ENCRYPTED_FIELD_KEY
    XDATA_KEY
    AX_API_SIG_KEY
    X_API_BASE_SECRET
    JWT_SECRET
)

# --- 1. System dependencies ----------------------------------------------------
if ! command -v git >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1 \
   || ! python3 -c "import venv" >/dev/null 2>&1 \
   || ! command -v curl >/dev/null 2>&1; then
    log "Installing system dependencies (git, python3-venv, curl)..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null
    apt-get install -y git python3 python3-venv python3-pip curl ca-certificates >/dev/null
fi

# --- 2. Source: clone dev, or pull latest ---------------------------------------
if [ -f "${INSTALL_DIR}/main.py" ] && [ -d "${INSTALL_DIR}/.git" ]; then
    log "Existing install found - pulling latest dev..."
    if ! git -C "${INSTALL_DIR}" pull --ff-only origin dev 2>/dev/null; then
        warn "Fast-forward pull failed (local patches?). Hard-resetting to origin/dev..."
        git -C "${INSTALL_DIR}" fetch origin dev
        git -C "${INSTALL_DIR}" reset --hard origin/dev
    fi
else
    log "Fresh install - cloning ${REPO_URL} (branch ${REPO_BRANCH})..."
    # Jangan pernah menghapus isi volume. Clone ke temp, lalu copy kode masuk.
    # - Gagal jaringan -> exit SEBELUM menyentuh isi volume (.env/data aman)
    # - .git korup di volume tidak relevan (sejarah datang dari clone baru)
    mkdir -p "${INSTALL_DIR}"
    tmp="$(mktemp -d)"
    if ! git clone --depth 1 -b "${REPO_BRANCH}" "${REPO_URL}" "${tmp}/repo"; then
        rm -rf "${tmp}"
        warn "Clone gagal (jaringan?). Tidak ada data yang disentuh — coba lagi nanti."
        exit 1
    fi
    ENV_KEEP=""
    if [ -f "${INSTALL_DIR}/.env" ]; then
        cp "${INSTALL_DIR}/.env" "${tmp}/.env.keep"
        ENV_KEEP=1
    fi
    cp -a "${tmp}/repo/." "${INSTALL_DIR}/"
    if [ -n "${ENV_KEEP}" ]; then
        mv "${tmp}/.env.keep" "${INSTALL_DIR}/.env"
    fi
    rm -rf "${tmp}"
fi
cd "${INSTALL_DIR}"

# --- 3. Virtualenv + Python dependencies ----------------------------------------
NEED_PIP=0
if [ ! -x "${INSTALL_DIR}/venv/bin/python" ]; then
    log "Creating virtualenv..."
    python3 -m venv venv
    NEED_PIP=1
else
    STAMP=".venv-requirements.sha"
    CUR="$(sha256sum requirements.txt 2>/dev/null | cut -d' ' -f1 || echo none)"
    PREV="$(cat "${STAMP}" 2>/dev/null || echo "")"
    if [ "${CUR}" != "${PREV}" ]; then
        NEED_PIP=1
    fi
fi
if [ "${NEED_PIP}" -eq 1 ]; then
    log "Installing Python dependencies..."
    venv/bin/pip install --upgrade pip --quiet
    venv/bin/pip install -r requirements.txt --quiet
    sha256sum requirements.txt | cut -d' ' -f1 > .venv-requirements.sha
fi

# --- 4. Translate stale main-branch links to dev ---------------------------------
HEAD_SHA="$(git rev-parse HEAD)"
LINK_STAMP=".branch-links-patched"
if [ "${HEAD_SHA}" != "$(cat "${LINK_STAMP}" 2>/dev/null || echo "")" ]; then
    PATCHED=0
    while IFS= read -r f; do
        if grep -q "rohtembak-xl/main/" "$f" 2>/dev/null; then
            sed -i 's|rohtembak-xl/main/|rohtembak-xl/dev/|g' "$f"
            PATCHED=$((PATCHED + 1))
        fi
    done < <(git ls-files | grep -E '\.(sh|py|yml|yaml|md)$' || true)
    if [ "${PATCHED}" -gt 0 ]; then
        log "Patched main→dev branch references in ${PATCHED} file(s)."
    fi
    echo "${HEAD_SHA}" > "${LINK_STAMP}"
fi

# --- 5. Configuration (.env) -----------------------------------------------------
# Repo ships a ready-to-run .env (tracked). Override paths below.
ENV_PROVIDED=0
for key in "${SECRET_KEYS[@]}"; do
    if [ -n "${!key:-}" ]; then
        ENV_PROVIDED=1
        break
    fi
done

ENV_WRITTEN=0
if [ -n "${ROHTEMBAK_ENV_FILE:-}" ] && [ -f "${ROHTEMBAK_ENV_FILE}" ]; then
    log "Using env file: ${ROHTEMBAK_ENV_FILE}"
    cp "${ROHTEMBAK_ENV_FILE}" "${INSTALL_DIR}/.env"
    ENV_WRITTEN=1
elif [ "${ENV_PROVIDED}" -eq 1 ]; then
    # Fill provided values line-by-line (delete + append: immune to | & \ in values)
    log "Applying secrets from environment variables..."
    touch "${INSTALL_DIR}/.env"
    for key in "${SECRET_KEYS[@]}"; do
        val="${!key:-}"
        if [ -n "${val}" ]; then
            sed -i "/^${key}=/d" "${INSTALL_DIR}/.env"
            printf '%s=%s\n' "${key}" "${val}" >> "${INSTALL_DIR}/.env"
            ENV_WRITTEN=1
        fi
    done
fi
if [ "${ENV_WRITTEN}" -eq 0 ]; then
    log "Keeping existing .env"
fi

# --- 6. Wait for real secrets (no crash-loop) ------------------------------------
if ! grep -qE "^BASE_CIAM_URL=.+" "${INSTALL_DIR}/.env" 2>/dev/null; then
    echo "[entrypoint] .env belum berisi secret (BASE_CIAM_URL kosong)."
    echo "[entrypoint] Letakkan .env asli, mis.: podman cp .env rohtembak-dev:/opt/rohtembak/.env"
    echo "[entrypoint] lalu: podman restart rohtembak-dev"
    echo "[entrypoint] Menunggu .env... (container tetap hidup, bukan crash-loop)"
    sleep infinity
fi

# --- 7. Run the app as PID 1 ------------------------------------------------------
log "Starting uvicorn on :${APP_PORT} ..."
cd "${INSTALL_DIR}"
exec "${INSTALL_DIR}/venv/bin/python" -m uvicorn main:app --host 0.0.0.0 --port "${APP_PORT}"
