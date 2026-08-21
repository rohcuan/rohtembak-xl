import os

import requests

GOPAY_GATEWAY_URL = (os.getenv("GOPAY_GATEWAY_URL") or "").rstrip("/")
GOPAY_API_KEY = os.getenv("GOPAY_API_KEY") or ""
GOPAY_TIMEOUT = int(os.getenv("GOPAY_TIMEOUT", "30"))


def is_configured() -> bool:
    return bool(GOPAY_GATEWAY_URL and GOPAY_API_KEY)


def create_qris(amount: int) -> dict:
    """Request a dynamic QRIS from the GoPay merchant gateway.

    Returns the gateway JSON dict on success, or {"success": False, "error": ...}.
    """
    if not is_configured():
        return {"success": False, "error": "Gateway QRIS belum dikonfigurasi"}
    try:
        res = requests.post(
            f"{GOPAY_GATEWAY_URL}/create-qris",
            json={"amount": int(amount)},
            headers={"X-Api-Key": GOPAY_API_KEY},
            timeout=GOPAY_TIMEOUT,
        )
        data = res.json()
    except Exception as e:
        return {"success": False, "error": f"Gagal menghubungi gateway QRIS: {e}"}
    if not isinstance(data, dict) or not data.get("success"):
        err = data.get("error") if isinstance(data, dict) else None
        return {"success": False, "error": err or "Gateway menolak permintaan QRIS"}
    return data


def check_payment(amount: int, trx_id: str) -> dict:
    """Server-to-server payment check scoped by trx_id (anti double-claim).

    Returns gateway JSON dict; `paid` is True only when the matching
    transaction was found and claimed for this trx_id.
    """
    if not is_configured():
        return {"success": False, "error": "Gateway QRIS belum dikonfigurasi"}
    try:
        res = requests.get(
            f"{GOPAY_GATEWAY_URL}/check-payment",
            params={"amount": int(amount), "trx_id": trx_id},
            headers={"X-Api-Key": GOPAY_API_KEY},
            timeout=GOPAY_TIMEOUT,
        )
        data = res.json()
    except Exception as e:
        return {"success": False, "error": f"Gagal menghubungi gateway QRIS: {e}"}
    if not isinstance(data, dict):
        return {"success": False, "error": "Respon gateway tidak valid"}
    return data
