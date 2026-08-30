import os
import time

import requests

GOPAY_GATEWAY_URL = (os.getenv("GOPAY_GATEWAY_URL") or "").rstrip("/")
GOPAY_API_KEY = os.getenv("GOPAY_API_KEY") or ""
GOPAY_TIMEOUT = int(os.getenv("GOPAY_TIMEOUT", "30"))

SETTING_URL_KEY = "gopay_gateway_url"
SETTING_API_KEY = "gopay_api_key"

# Cache config DB 5 detik — qr_page_url dipanggil per baris riwayat;
# tanpa cache itu = buka session DB baru untuk tiap baris.
_CFG_TTL = 5.0
_cfg_cache = {"ts": 0.0, "url": "", "key": ""}


def get_config() -> tuple[str, str]:
    """Resolve (gateway_url, api_key): DB settings first, env as fallback."""
    now = time.time()
    if now - _cfg_cache["ts"] < _CFG_TTL:
        return _cfg_cache["url"], _cfg_cache["key"]
    url = GOPAY_GATEWAY_URL
    key = GOPAY_API_KEY
    try:
        from database import get_db
        from models import AppSetting
        db = next(get_db())
        try:
            rows = (
                db.query(AppSetting)
                .filter(AppSetting.key.in_([SETTING_URL_KEY, SETTING_API_KEY]))
                .all()
            )
            stored = {r.key: (r.value or "").strip() for r in rows}
        finally:
            db.close()
        url = stored.get(SETTING_URL_KEY) or url
        key = stored.get(SETTING_API_KEY) or key
    except Exception as e:
        # Jangan diam-diam jatuh ke env — bisa menyembunyikan masalah konfigurasi.
        print(f"[gopay.get_config] gagal baca DB settings, pakai fallback env: {e}")
    _cfg_cache.update(ts=now, url=(url or "").rstrip("/"), key=key or "")
    return _cfg_cache["url"], _cfg_cache["key"]


def set_config(url: str, api_key: str) -> None:
    """Persist gateway config to DB (empty value clears the override)."""
    _cfg_cache.update(ts=0.0)  # invalidasi cache — perubahan admin harus langsung efektif
    from database import get_db
    from models import AppSetting
    db = next(get_db())
    try:
        for key, val in ((SETTING_URL_KEY, (url or "").strip().rstrip("/")), (SETTING_API_KEY, (api_key or "").strip())):
            row = db.query(AppSetting).filter(AppSetting.key == key).first()
            if row:
                row.value = val
            else:
                db.add(AppSetting(key=key, value=val))
        db.commit()
    finally:
        db.close()


def is_configured() -> bool:
    url, key = get_config()
    return bool(url and key)


def _request(path: str, url: str, api_key: str, timeout: int, **kwargs) -> dict:
    """Call a gateway endpoint; never raises. Returns structured dict."""
    if not url or not api_key:
        return {"ok": False, "http_status": 0, "data": None,
                "error": "Endpoint atau API key belum diisi"}
    try:
        res = requests.get(
            f"{url.rstrip('/')}{path}",
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            **kwargs,
        )
    except Exception as e:
        return {"ok": False, "http_status": 0, "data": None,
                "error": f"Tidak dapat menghubungi endpoint: {e}"}
    try:
        data = res.json()
    except Exception:
        data = None
    return {"ok": res.status_code == 200 and isinstance(data, dict) and data.get("success") is True,
            "http_status": res.status_code, "data": data if isinstance(data, dict) else None,
            "error": None}


def token_status(url: str | None = None, api_key: str | None = None, timeout: int | None = None) -> dict:
    """GET /token-status — verify gateway reachability, API key, and GoPay session."""
    if url is None or api_key is None:
        url, api_key = get_config()
    return _request("/token-status", url, api_key, timeout or min(GOPAY_TIMEOUT, 15))


def create_qris(amount: int) -> dict:
    """Request a dynamic QRIS from the GoPay merchant gateway.

    Returns the gateway JSON dict on success, or {"success": False, "error": ...}.
    """
    url, api_key = get_config()
    if not url or not api_key:
        return {"success": False, "error": "Gateway QRIS belum dikonfigurasi"}
    try:
        res = requests.post(
            f"{url}/create-qris",
            json={"amount": int(amount)},
            headers={"X-Api-Key": api_key},
            timeout=GOPAY_TIMEOUT,
        )
        data = res.json()
    except Exception as e:
        return {"success": False, "error": f"Gagal menghubungi gateway QRIS: {e}"}
    if not isinstance(data, dict) or not data.get("success"):
        err = data.get("error") if isinstance(data, dict) else None
        return {"success": False, "error": err or "Gateway menolak permintaan QRIS"}
    return data


def qr_page_url(qris_id: str) -> str:
    """Hosted payment page URL (gateway /qr/:id) for a QRIS.

    The gateway page handles QR display, countdown, status checking, and
    expiry messaging on its own. Returns "" when not configurable.
    """
    url, _ = get_config()
    if not url or not qris_id:
        return ""
    return f"{url}/qr/{qris_id}"


def get_qris_image(qris_id: str) -> dict:
    """Fetch the QRIS PNG live from the gateway (/qr/:id?format=raw).

    Returns {"ok": True, "data": <png bytes>} or {"ok": False, "error": ...}.
    Gateway offline / QRIS hilang / kedaluwarsa -> not ok, sehingga panel
    menolak menampilkan QR yang tidak bisa dipakai bayar (PG wajib online).
    """
    url, api_key = get_config()
    if not url or not qris_id:
        return {"ok": False, "error": "Gateway QRIS belum dikonfigurasi"}
    try:
        res = requests.get(
            f"{url}/qr/{qris_id}",
            params={"format": "raw"},
            headers={"X-Api-Key": api_key},
            timeout=GOPAY_TIMEOUT,
            allow_redirects=True,
        )
    except Exception as e:
        return {"ok": False, "error": f"Gagal menghubungi gateway QRIS: {e}"}
    if res.status_code == 410:
        return {"ok": False, "error": "QRIS sudah kedaluwarsa."}
    if res.status_code == 404:
        return {"ok": False, "error": "QRIS tidak ditemukan."}
    if res.status_code != 200 or not res.content:
        return {"ok": False, "error": f"Gateway merespons HTTP {res.status_code}."}
    return {"ok": True, "data": res.content}


def check_payment(amount: int, trx_id: str, start_time: str | None = None) -> dict:
    """Server-to-server payment check scoped by trx_id (anti double-claim).

    Returns gateway JSON dict; `paid` is True only when the matching
    transaction was found and claimed for this trx_id.
    """
    url, api_key = get_config()
    if not url or not api_key:
        return {"success": False, "error": "Gateway QRIS belum dikonfigurasi"}
    params = {"amount": int(amount), "trx_id": trx_id}
    if start_time:
        params["startTime"] = start_time
    try:
        res = requests.get(
            f"{url}/check-payment",
            params=params,
            headers={"X-Api-Key": api_key},
            timeout=GOPAY_TIMEOUT,
        )
        data = res.json()
    except Exception as e:
        return {"success": False, "error": f"Gagal menghubungi gateway QRIS: {e}"}
    if not isinstance(data, dict):
        return {"success": False, "error": "Respon gateway tidak valid"}
    return data
