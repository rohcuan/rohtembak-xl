import os
import io
import time
import json
from contextlib import asynccontextmanager, redirect_stdout
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Depends, Form, HTTPException, status
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from jinja2 import Environment, FileSystemLoader

from database import init_db, get_db
from models import User, XLAccount, Balance, BalanceTransaction, FamilyFee, QrisTransaction
from auth import (
    verify_password, create_access_token, decode_token,
    get_current_user, seed_users, hash_password
)

from datetime import datetime, timezone, timedelta
from app.client.ciam import get_otp as xl_get_otp, submit_otp as xl_submit_otp, get_new_token as xl_refresh_token
from app.client.encrypt import API_KEY, load_ax_fp
from app.client.engsel import login_info as xl_login_info, get_balance as xl_get_balance, get_transaction_history as xl_get_transactions, get_tiering_info as xl_get_tiering, send_api_request, get_family as xl_get_family, get_package as xl_get_package, get_addons as xl_get_addons
from app.menus.util import format_quota_byte
from app.type_dict import PaymentItem

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(BASE_DIR, "templates")
jinja_env = Environment(loader=FileSystemLoader(templates_dir), auto_reload=True)
WIB = timezone(timedelta(hours=7))


def _fmt_epoch_wib(ts):
    """Render a true unix epoch (UTC instant) as a WIB wall-clock in XL history format."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=WIB).strftime("%d %B %Y | %H:%M WIB")
    except (ValueError, OSError):
        return None


def _tgl_jam_wib(ts):
    """Split a true epoch into separate (tanggal, jam) WIB strings for the history table."""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=WIB)
        return dt.strftime("%d %B %Y"), dt.strftime("%H:%M WIB")
    except (ValueError, OSError):
        return None, None


def _fmt_xl_ts(ts):
    """XL transaction timestamps are WIB wall-clock stored as if UTC (+7h ahead of the
    real instant). Subtract 7h first so the rendered WIB matches XL's formated_date."""
    if not ts:
        return None
    try:
        return _fmt_epoch_wib(int(ts) - 7 * 3600)
    except (ValueError, OSError):
        return None


def _fmt_xl_expiry(ts):
    """Quota/balance expiry epochs are true UTC; render in WIB unchanged."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=WIB).strftime("%d %b %Y %H:%M WIB")
    except (ValueError, OSError):
        return None


def _fmt_wib(ts):
    if not ts:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(WIB).strftime("%Y-%m-%d %H:%M WIB")


def _parse_xl_dt(s):
    if not s:
        return 0
    s = str(s).strip()
    if s.endswith(" WIB"):
        s = s[:-4].rstrip()
    for fmt in ("%d %B %Y | %H:%M", "%d %b %Y | %H:%M", "%d %B %Y %H:%M", "%d %b %Y %H:%M"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


jinja_env.filters["datetimeformat"] = lambda ts: _fmt_xl_expiry(ts) or "—"
jinja_env.filters["quotabyte"] = format_quota_byte

API_DELAY = float(os.getenv("API_DELAY", "2.0"))
_XL_CALL_LIMIT = max(1, int(os.getenv("XL_CALL_LIMIT", "6")))

import app.client.engsel as _engsel

_xl_sem = __import__("threading").BoundedSemaphore(_XL_CALL_LIMIT)
_engsel_send_api_request = _engsel.send_api_request


def _bounded_send_api_request(*args, **kwargs):
    """Rate-limit concurrent XL API calls so many users don't hammer/timeout."""
    with _xl_sem:
        return _engsel_send_api_request(*args, **kwargs)


_engsel.send_api_request = _bounded_send_api_request
send_api_request = _bounded_send_api_request


def _api_delay():
    time.sleep(API_DELAY)


_XL_TOKEN_CACHE = {}
_TOKEN_CACHE_FALLBACK_TTL = 540
REFRESH_WARN_SECONDS = 3 * 86400
_token_lock = __import__("threading").Lock()


def _persist_refresh_token(account_id, refresh_token, refresh_expires_at):
    """Save the latest (rotated) XL refresh token and its expiry to DB."""
    try:
        db = next(get_db())
        acct = db.query(XLAccount).filter(XLAccount.id == account_id).first()
        if not acct:
            return
        changed = False
        if refresh_token and refresh_token != acct.refresh_token:
            acct.refresh_token = refresh_token
            changed = True
        if refresh_expires_at and refresh_expires_at != acct.refresh_expires_at:
            acct.refresh_expires_at = int(refresh_expires_at)
            changed = True
        if changed:
            db.commit()
    except Exception as e:
        print(f"[persist_refresh] Error: {e}")
    finally:
        try:
            db.close()
        except Exception:
            pass


def _get_xl_tokens(active_xl):
    """Return cached XL access tokens; refresh once per token lifetime (~10 min).

    Access token expires_in is ~599s, refresh token ~88 days. Tokens are cached
    per XL account for the access-token lifetime so the (slow) refresh call is
    only made occasionally instead of on every request.

    XL rotates the refresh token on every refresh, so the newest token is saved
    back to DB (sliding ~88-day window) together with its refresh_expires_at.
    """
    if not active_xl or not active_xl.refresh_token:
        return None
    key = active_xl.subscriber_id or active_xl.id
    now = time.time()
    entry = _XL_TOKEN_CACHE.get(key)
    if entry and entry.get("expires_at", 0) > now:
        return entry["tokens"]
    with _token_lock:
        entry = _XL_TOKEN_CACHE.get(key)
        if entry and entry.get("expires_at", 0) > now:
            return entry["tokens"]
        _api_delay()
        tokens = xl_refresh_token(API_KEY, active_xl.refresh_token, active_xl.subscriber_id)
        if not tokens:
            _XL_TOKEN_CACHE.pop(key, None)
            return None
        try:
            ttl = int(tokens.get("expires_in", 0)) - 30
        except (TypeError, ValueError):
            ttl = _TOKEN_CACHE_FALLBACK_TTL
        if ttl < 30:
            ttl = 30
        _persist_refresh_token(
            active_xl.id,
            tokens.get("refresh_token"),
            tokens.get("refresh_expires_in") and int(time.time()) + int(tokens["refresh_expires_in"]),
        )
        _XL_TOKEN_CACHE[key] = {"tokens": tokens, "expires_at": now + ttl}
        return tokens


def render(template_name: str, status_code: int = 200, context: dict | None = None, cache_control: str = "no-store"):
    template = jinja_env.get_template(template_name)
    html = template.render(**(context or {}))
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": cache_control})


def render_template_to_string(template_name: str, context: dict | None = None) -> str:
    template = jinja_env.get_template(template_name)
    return template.render(**(context or {}))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        import anyio
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = 256
    except Exception:
        pass
    init_db()
    db = next(get_db())
    seed_users(db)
    db.close()
    yield


app = FastAPI(title="RohTembak (XL)", lifespan=lifespan)

static_dir = os.path.join(BASE_DIR, "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ─── Error Pages ────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found(request: Request, exc):
    return render("error.html", context={
        "request": request,
        "code": 404,
        "message": "Halaman tidak ditemukan"
    }, status_code=404)


@app.exception_handler(500)
async def server_error(request: Request, exc):
    return render("error.html", context={
        "request": request,
        "code": 500,
        "message": "Terjadi kesalahan server"
    }, status_code=500)


def get_user_context(user: User, db: Session) -> dict:
    bal = db.query(Balance).filter(Balance.user_id == user.id).first()
    xl_accounts = db.query(XLAccount).filter(XLAccount.user_id == user.id).all()
    active_xl = next((x for x in xl_accounts if x.is_active), None)
    refresh_warning = None
    if active_xl and active_xl.refresh_expires_at:
        secs_left = int(active_xl.refresh_expires_at) - time.time()
        if secs_left <= REFRESH_WARN_SECONDS:
            refresh_warning = round(secs_left / 86400, 1)
    return {
        "request": None,
        "user": user,
        "balance": bal.balance if bal else 0,
        "xl_accounts": xl_accounts,
        "active_xl": active_xl,
        "refresh_warning": refresh_warning,
    }


# ─── Root redirect ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return RedirectResponse(url="/login", status_code=303)


# ─── Login ─────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = request.cookies.get("access_token")
    if token:
        payload = decode_token(token)
        if payload:
            role = payload.get("role", "user")
            if role == "admin":
                return RedirectResponse(url="/admin/dashboard", status_code=303)
            return RedirectResponse(url="/user/dashboard", status_code=303)
    return render("login.html", context={"request": request})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return render("login.html", context={
            "request": request,
            "error": "Username atau password salah"
        }, status_code=400)

    if role == "admin" and user.role != "admin":
        return render("login.html", context={
            "request": request,
            "error": "Akun ini tidak memiliki akses admin"
        }, status_code=403)

    if role == "user" and user.role != "user":
        return render("login.html", context={
            "request": request,
            "error": "Akun admin tidak bisa login sebagai pengguna biasa"
        }, status_code=403)

    token = create_access_token({"sub": str(user.id), "role": user.role})
    resp = RedirectResponse(
        url="/admin/dashboard" if role == "admin" else "/user/dashboard",
        status_code=303
    )
    resp.set_cookie(key="access_token", value=token, httponly=True, max_age=900)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("access_token")
    return resp


# ─── Admin Dashboard ────────────────────────────────────────────────────────

@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return RedirectResponse(url="/user/dashboard", status_code=303)
    db = next(get_db())
    users = db.query(User).filter(User.role == "user").all()
    user_data = []
    for u in users:
        bal = db.query(Balance).filter(Balance.user_id == u.id).first()
        xl_count = db.query(XLAccount).filter(XLAccount.user_id == u.id).count()
        user_data.append({
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "balance": bal.balance if bal else 0,
            "xl_count": xl_count,
        })
    admin_bal = db.query(Balance).filter(Balance.user_id == user.id).first()
    db.close()
    fees = _get_all_family_fees()
    return render("admin/dashboard.html", context={
        "request": request,
        "user": user,
        "users": user_data,
        "balance": admin_bal.balance if admin_bal else 0,
        "fees": fees,
    })


@app.post("/admin/fees/set")
def admin_set_fee(
    family_key: str = Form(...),
    fee: int = Form(...),
    user: User = Depends(get_current_user),
):
    if user.role != "admin":
        return RedirectResponse(url="/user/dashboard", status_code=303)
    if family_key not in FAMILY_FEE_DEFAULTS or fee < 0:
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    _set_family_fee(family_key, fee)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/balance/add")
def admin_add_balance(
    user_id: int = Form(...),
    amount: int = Form(...),
    description: str = Form(""),
    admin_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Jumlah harus positif")

    bal = db.query(Balance).filter(Balance.user_id == user_id).first()
    if not bal:
        bal = Balance(user_id=user_id, balance=0)
        db.add(bal)
    bal.balance += amount

    trx = BalanceTransaction(
        user_id=user_id,
        amount=amount,
        type="topup",
        description=description or "Topup dari admin"
    )
    db.add(trx)
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/add")
def admin_add_user(
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    initial_balance: int = Form(0),
    admin_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")

    existing = db.query(User).filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username atau email sudah terdaftar")

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        password=password,
        role="user"
    )
    db.add(user)
    db.flush()
    bal = Balance(user_id=user.id, balance=0)
    db.add(bal)
    if initial_balance > 0:
        bal.balance = initial_balance
        db.add(BalanceTransaction(
            user_id=user.id,
            amount=initial_balance,
            type="topup",
            description="Saldo awal dari admin"
        ))
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/users/delete")
def admin_delete_user(
    user_id: int = Form(...),
    admin_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    if user_id == admin_user.id:
        raise HTTPException(status_code=400, detail="Tidak bisa menghapus akun sendiri")

    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    db.query(BalanceTransaction).filter(BalanceTransaction.user_id == u.id).delete()
    db.delete(u)
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/balance/decrease")
def admin_decrease_balance(
    user_id: int = Form(...),
    amount: int = Form(...),
    description: str = Form(""),
    admin_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Jumlah harus positif")

    bal = db.query(Balance).filter(Balance.user_id == user_id).first()
    if not bal or bal.balance <= 0:
        raise HTTPException(status_code=400, detail="Saldo tidak cukup")
    if amount > bal.balance:
        raise HTTPException(status_code=400, detail="Saldo tidak cukup")

    bal.balance -= amount
    db.add(BalanceTransaction(
        user_id=user_id,
        amount=-amount,
        type="decrease",
        description=description or "Kurangi oleh admin"
    ))
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/balance/set")
def admin_set_balance(
    user_id: int = Form(...),
    amount: int = Form(...),
    description: str = Form(""),
    admin_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    if amount < 0:
        raise HTTPException(status_code=400, detail="Jumlah tidak boleh negatif")

    bal = db.query(Balance).filter(Balance.user_id == user_id).first()
    if not bal:
        bal = Balance(user_id=user_id, balance=0)
        db.add(bal)
    delta = amount - bal.balance
    bal.balance = amount
    if delta != 0:
        db.add(BalanceTransaction(
            user_id=user_id,
            amount=delta,
            type="set",
            description=description or "Set saldo oleh admin"
        ))
    db.commit()
    return RedirectResponse(url="/admin/dashboard", status_code=303)


# ─── Admin Backup ───────────────────────────────────────────────────────────

@app.post("/admin/penghasilan/hapus")
def admin_penghasilan_hapus(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return RedirectResponse(url="/user/dashboard", status_code=303)
    db = next(get_db())
    db.query(BalanceTransaction).delete()
    db.commit()
    db.close()
    return RedirectResponse(url="/admin/penghasilan", status_code=303)


def _income_range_start(period: str):
    now_wib = datetime.now(WIB)
    if period == "today":
        start = now_wib.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now_wib - timedelta(days=now_wib.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = now_wib.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now_wib - timedelta(days=3650)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


@app.get("/admin/penghasilan", response_class=HTMLResponse)
def admin_penghasilan(request: Request, user: User = Depends(get_current_user)):
    if user.role != "admin":
        return RedirectResponse(url="/user/dashboard", status_code=303)
    db = next(get_db())
    metric = request.query_params.get("metric", "topup")
    if metric not in ("topup", "used"):
        metric = "topup"
    period = request.query_params.get("range", "today")
    trx_type = "topup" if metric == "topup" else "purchase"
    start = _income_range_start(period)
    rows = db.query(BalanceTransaction).filter(
        BalanceTransaction.type == trx_type,
        BalanceTransaction.created_at >= start,
    ).order_by(BalanceTransaction.created_at.desc()).all()
    total = sum(abs(r.amount) for r in rows)
    details = []
    for r in rows:
        u = db.query(User).filter(User.id == r.user_id).first()
        details.append({
            "ts": _fmt_wib(r.created_at) if r.created_at else "—",
            "username": u.username if u else f"user #{r.user_id}",
            "amount": abs(r.amount),
        })
    db.close()
    return render("admin/penghasilan.html", context={
        "request": request, "user": user,
        "metric": metric,
        "period": period,
        "total": total,
        "details": details,
        "INCOME_METRICS": [("topup", "Total Topup"), ("used", "Total Dipakai")],
        "INCOME_RANGES": [("today", "Hari ini"), ("week", "Minggu ini"), ("month", "Bulan ini")],
    })


@app.get("/admin/backup", response_class=JSONResponse)
def admin_backup(format: str = "json", admin_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if admin_user.role != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    users = db.query(User).filter(User.role == "user").all()
    data = []
    for u in users:
        bal = db.query(Balance).filter(Balance.user_id == u.id).first()
        data.append({
            "username": u.username,
            "password": u.password or u.password_hash,
            "email": u.email,
            "saldo": bal.balance if bal else 0,
        })
    if format == "txt":
        lines = []
        for u in data:
            lines.append(f"username: {u['username']}")
            lines.append(f"password: {u['password']}")
            lines.append(f"email: {u['email']}")
            lines.append(f"saldo: {u['saldo']}")
            lines.append("")
        content = "\n".join(lines).strip()
        resp = PlainTextResponse(content)
        resp.headers["Content-Disposition"] = 'attachment; filename="backup.txt"'
        return resp
    payload = {
        "exported_at": datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB"),
        "users": data,
    }
    resp = JSONResponse(payload)
    resp.headers["Content-Disposition"] = 'attachment; filename="backup.json"'
    return resp


# ─── User Dashboard ─────────────────────────────────────────────────────────

@app.get("/user/dashboard", response_class=HTMLResponse)
def user_dashboard(request: Request, frag: int = 0, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()

    ctx["request"] = request
    if frag:
        html = render_template_to_string("user/_dashboard_xl_list.html", ctx)
        return JSONResponse({"ok": True, "html": html})
    return render("user/dashboard.html", context=ctx)


# ─── XL Number Management ──────────────────────────────────────────────────

@app.post("/user/xl/set-active")
def set_active_xl(
    xl_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(XLAccount).filter(
        XLAccount.user_id == user.id, XLAccount.is_active == True
    ).update({"is_active": False})
    xl = db.query(XLAccount).filter(
        XLAccount.id == xl_id, XLAccount.user_id == user.id
    ).first()
    if xl:
        xl.is_active = True
        db.commit()
    return RedirectResponse(url="/user/dashboard", status_code=303)


@app.post("/user/xl/add")
def add_xl(
    request: Request,
    phone_number: str = Form(...),
    label: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not phone_number.startswith("628") or len(phone_number) < 10 or len(phone_number) > 15:
        ctx = get_user_context(user, db)
        ctx.update({"request": request, "error": "Nomor tidak valid. Harus diawali 628 dan 10-15 digit"})
        return render("user/dashboard.html", context=ctx, status_code=400)

    existing = db.query(XLAccount).filter(
        XLAccount.user_id == user.id,
        XLAccount.phone_number == phone_number
    ).first()
    if existing:
        ctx = get_user_context(user, db)
        ctx.update({"request": request, "error": "Nomor ini sudah terdaftar"})
        return render("user/dashboard.html", context=ctx, status_code=400)

    has_active = db.query(XLAccount).filter(
        XLAccount.user_id == user.id, XLAccount.is_active == True
    ).count() > 0

    xl = XLAccount(
        user_id=user.id,
        phone_number=phone_number,
        label=label,
        is_active=not has_active,
    )
    db.add(xl)
    db.commit()
    return RedirectResponse(url="/user/dashboard", status_code=303)


@app.post("/user/xl/remove")
def remove_xl(
    request: Request,
    xl_id: int = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    xl = db.query(XLAccount).filter(
        XLAccount.id == xl_id, XLAccount.user_id == user.id
    ).first()
    if xl:
        remaining = db.query(XLAccount).filter(
            XLAccount.user_id == user.id, XLAccount.id != xl_id
        ).count()
        if remaining == 0:
            ctx = get_user_context(user, db)
            ctx.update({"request": request, "error": "Tidak bisa menghapus satu-satunya nomor XL"})
            return render("user/dashboard.html", context=ctx, status_code=400)
        was_active = xl.is_active
        db.delete(xl)
        if was_active:
            first_remaining = db.query(XLAccount).filter(
                XLAccount.user_id == user.id
            ).first()
            if first_remaining:
                first_remaining.is_active = True
        db.commit()
    return RedirectResponse(url="/user/dashboard", status_code=303)


# ─── XL OTP Login Flow ────────────────────────────────────────────────────

@app.get("/user/xl/otp/request", response_class=HTMLResponse)
def xl_otp_request_page(request: Request, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx["request"] = request
    return render("user/otp_request.html", context=ctx)


@app.post("/user/xl/otp/request")
def xl_otp_request(
    request: Request,
    phone_number: str = Form(...),
    label: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    ctx = get_user_context(user, db)

    if not phone_number.startswith("628") or len(phone_number) < 10 or len(phone_number) > 15:
        ctx.update({"request": request, "error": "Nomor tidak valid. Harus diawali 628 dan 10-15 digit"})
        return render("user/otp_request.html", context=ctx, status_code=400)

    existing = db.query(XLAccount).filter(
        XLAccount.user_id == user.id,
        XLAccount.phone_number == phone_number
    ).first()
    if existing:
        ctx.update({"request": request, "error": "Nomor ini sudah terdaftar"})
        return render("user/otp_request.html", context=ctx, status_code=400)

    try:
        _api_delay()
        subscriber_id = xl_get_otp(phone_number)
    except Exception as e:
        ctx.update({"request": request, "error": f"Gagal mengirim OTP: {e}"})
        return render("user/otp_request.html", context=ctx, status_code=400)
    if subscriber_id is None:
        ctx.update({"request": request, "error": "Gagal mengirim OTP. Periksa nomor atau tunggu beberapa saat."})
        return render("user/otp_request.html", context=ctx, status_code=400)

    return RedirectResponse(
        url=f"/user/xl/otp/submit?phone={phone_number}&label={label}&sid={subscriber_id}",
        status_code=303
    )


@app.get("/user/xl/otp/submit", response_class=HTMLResponse)
def xl_otp_submit_page(
    request: Request,
    phone: str = "",
    label: str = "",
    sid: str = "",
    user: User = Depends(get_current_user),
):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if not phone:
        return RedirectResponse(url="/user/xl/otp/request", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx.update({"request": request, "phone_number": phone, "label": label, "sid": sid})
    return render("user/otp_submit.html", context=ctx)


@app.post("/user/xl/otp/submit")
def xl_otp_submit(
    request: Request,
    phone_number: str = Form(...),
    otp_code: str = Form(...),
    label: str = Form(""),
    subscriber_id: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)

    ctx = get_user_context(user, db)

    if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
        ctx.update({"request": request, "phone_number": phone_number, "label": label,
            "error": "Kode OTP harus 6 digit angka"})
        return render("user/otp_submit.html", context=ctx, status_code=400)

    _api_delay()
    tokens = xl_submit_otp(API_KEY, "SMS", phone_number, otp_code)
    if tokens is None:
        ctx.update({"request": request, "phone_number": phone_number, "label": label,
            "error": "Kode OTP salah atau sudah kadaluarsa"})
        return render("user/otp_submit.html", context=ctx, status_code=400)

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    id_token = tokens.get("id_token", "")
    refresh_expires_at = None
    try:
        ref_exp = tokens.get("refresh_expires_in")
        if ref_exp:
            refresh_expires_at = int(time.time()) + int(ref_exp)
    except (TypeError, ValueError):
        refresh_expires_at = None

    _api_delay()
    profile = None
    try:
        profile = xl_login_info(API_KEY, {"access_token": access_token, "id_token": id_token})
    except Exception as e:
        print(f"[otp_submit] login_info gagal (akun tetap disimpan): {e}")
    subscription_type = "PREPAID"
    if profile and "subscription_type" in profile:
        subscription_type = profile["subscription_type"]

    has_active = db.query(XLAccount).filter(
        XLAccount.user_id == user.id, XLAccount.is_active == True
    ).count() > 0

    xl = XLAccount(
        user_id=user.id,
        phone_number=phone_number,
        label=label,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
        subscriber_id=subscriber_id,
        subscription_type=subscription_type,
        is_active=not has_active,
    )
    db.add(xl)
    db.commit()

    return RedirectResponse(url="/user/dashboard", status_code=303)


# ─── Riwayat Transaksi ──────────────────────────────────────────────────────

@app.get("/user/detail-saldo-token", response_class=HTMLResponse)
def user_history(request: Request, user: User = Depends(get_current_user)):
    db = next(get_db())
    ctx = get_user_context(user, db)
    transactions = db.query(BalanceTransaction).filter(
        BalanceTransaction.user_id == user.id
    ).order_by(BalanceTransaction.created_at.desc()).all()
    db.close()
    rows = [{
        "ts": _fmt_wib(t.created_at),
        "type": t.type,
        "amount": t.amount,
        "description": t.description or "—",
    } for t in transactions]
    ctx.update({"request": request, "transactions": rows})
    return render("user/history.html", context=ctx)


def _build_history_rows(active_xl, user):
    xl_transactions = None
    tokens = None
    if active_xl and active_xl.refresh_token:
        try:
            _api_delay()
            tokens = _get_xl_tokens(active_xl)
            if tokens:
                _api_delay()
                xl_transactions = xl_get_transactions(API_KEY, tokens)
        except Exception as e:
            print(f"[xl_history] Error: {e}")

    qris_txs, qris_codes = _fetch_pending_qris(
        active_xl, tokens=tokens, transactions=xl_transactions
    )
    if xl_transactions and isinstance(xl_transactions, dict) and xl_transactions.get("list"):
        xl_transactions["list"] = [
            t for t in xl_transactions["list"]
            if (t.get("code") or "") not in qris_codes
        ]
    db = next(get_db())
    db_qris = db.query(QrisTransaction).filter(
        QrisTransaction.user_id == user.id
    ).order_by(QrisTransaction.created_at.desc()).all()
    seen = {q.get("transaction_id") for q in qris_txs}
    seen_qr = {q.get("qris_b64") for q in qris_txs}
    for q in db_qris:
        if q.transaction_id in seen:
            continue
        seen.add(q.transaction_id)
        if q.qris_b64 in seen_qr:
            continue
        seen_qr.add(q.qris_b64)
        rec = _reconcile_qris_record(q, tokens, db)
        if rec:
            qris_txs.append(rec)
    db.close()

    rows = []
    for q in qris_txs:
        expired = bool(q.get("expired"))
        te = int(q.get("ts_epoch") or 0)
        tgl, jam = _tgl_jam_wib(te) if te > 0 else (None, None)
        raw = q.get("created_at") or ""
        if tgl is None:
            if " | " in raw:
                tgl, jam = raw.split(" | ", 1)
            else:
                tgl, jam = (raw or "—"), "—"
        rows.append({
            "tgl": tgl,
            "jam": jam,
            "ts": f"{tgl} | {jam}",
            "sort": te,
            "paket": q.get("option_name") or "—",
            "harga": ("Rp {:,}".format(q["amount"]) if q.get("amount") else "—"),
            "status": "Expired" if expired else "Pending",
            "status_color": "red" if expired else "amber",
            "expires_ts": q.get("expires_ts") or 0,
            "expired": expired,
            "img": q.get("img") or "",
            "kind": "qris",
        })

    if xl_transactions and isinstance(xl_transactions, dict) and xl_transactions.get("list"):
        for trx in xl_transactions["list"]:
            s = (trx.get("status") or "").lower()
            if s in ("success", "finished"):
                color = "green"
            elif s in ("failed", "refund-success"):
                color = "red"
            else:
                color = "amber"
            ts_raw = trx.get("timestamp")
            if ts_raw:
                te = int(ts_raw) - 7 * 3600
            else:
                encoded = _parse_xl_dt(trx.get("formated_date") or "")
                te = encoded - 7 * 3600 if encoded else 0
            tgl, jam = _tgl_jam_wib(te) if te > 0 else (None, None)
            raw = trx.get("formated_date") or ""
            if tgl is None:
                if " | " in raw:
                    tgl, jam = raw.split(" | ", 1)
                else:
                    tgl, jam = (raw or "—"), "—"
            rows.append({
                "tgl": tgl,
                "jam": jam,
                "ts": f"{tgl} | {jam}",
                "sort": te,
                "paket": trx.get("title") or "—",
                "harga": trx.get("price") or "—",
                "status": trx.get("status") or "—",
                "status_color": color,
                "expires_ts": 0,
                "expired": False,
                "img": "",
                "kind": "xl",
            })

    rows.sort(key=lambda r: r["sort"], reverse=True)
    return rows, bool(rows)


@app.get("/user/xl/history", response_class=HTMLResponse)
def user_xl_history(request: Request, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()

    active_xl = ctx.get("active_xl")
    if request.query_params.get("frag") == "1":
        rows, has_rows = _build_history_rows(active_xl, user)
        html = jinja_env.get_template("user/_history_data.html").render(
            active_xl=active_xl, rows=rows, has_rows=has_rows, error=None
        )
        return JSONResponse({"ok": True, "html": html}, headers={"Cache-Control": "no-store"})

    ctx.update({"request": request})
    return render("user/xl_history.html", context=ctx)


def _build_info_paket_data(active_xl):
    paket_list = None
    if active_xl and active_xl.refresh_token:
        try:
            _api_delay()
            tokens = _get_xl_tokens(active_xl)
            if tokens:
                id_token = tokens.get("id_token", "")
                path = "api/v8/packages/quota-details"
                payload = {"is_enterprise": False, "lang": "en", "family_member_id": ""}
                _api_delay()
                res = send_api_request(API_KEY, path, payload, id_token, "POST")
                if isinstance(res, dict) and res.get("status") == "SUCCESS":
                    paket_list = res["data"].get("quotas", [])
                    for q in paket_list:
                        pf = q.get("package_family") or {}
                        if not pf.get("package_family_code"):
                            code = FAMILY_CODE_BY_NAME.get(pf.get("name"))
                            if code:
                                pf["package_family_code"] = code
                                q["package_family"] = pf
        except Exception as e:
            print(f"[info_paket] Error: {e}")
    return paket_list


@app.get("/user/xl/info-paket", response_class=HTMLResponse)
def user_xl_info_paket(request: Request, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()

    active_xl = ctx.get("active_xl")
    if request.query_params.get("frag") == "1":
        paket_list = _build_info_paket_data(active_xl)
        html = jinja_env.get_template("user/_info_paket_data.html").render(
            active_xl=active_xl, paket_list=paket_list
        )
        return JSONResponse({"ok": True, "html": html}, headers={"Cache-Control": "no-store"})

    ctx.update({"request": request})
    return render("user/info_paket.html", context=ctx)


# ─── Beli Paket ─────────────────────────────────────────────────────────────

FAMILY_CODE_XTRA_COMBO = "23b71540-8785-4abe-816d-e9b4efa48f95"
FAMILY_CODE_ADDON = "7658c955-a0b9-405f-bb17-de7f43d1a946"
FAMILY_CODE_ADDON_15 = "45c3a622-8c06-4bb1-8e56-bba1f3434600"
FAMILY_CODE_XTRA_CONFERENCE = "5dab52d5-6f02-4678-b72f-088396ceb113"

FAMILY_CODE_BY_NAME = {
    "Xtra Combo Plus": FAMILY_CODE_XTRA_COMBO,
    "Addon Xtra Combo Plus 10GB": FAMILY_CODE_ADDON,
    "Addon Xtra Combo Plus 15GB": FAMILY_CODE_ADDON_15,
    "Xtra Conference": FAMILY_CODE_XTRA_CONFERENCE,
}

ADDON_APP_NAMES = {"instagram", "tiktok", "facebook", "whatsapp", "youtube"}
ADDON_SPEC_10 = (FAMILY_CODE_ADDON, "addon10-xcp")
ADDON_SPEC_15 = (FAMILY_CODE_ADDON_15, "addon15-xcp")
XTRA_CONF_SPEC = (FAMILY_CODE_XTRA_CONFERENCE, "xtraconf")


def _build_family_list(family_data):
    items = []
    if not (family_data and family_data.get("package_variants")):
        return items
    item_number = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            items.append({
                "number": item_number,
                "label": option["name"],
                "size": "",
                "price": option["price"],
                "variant_name": variant["name"],
                "option_code": option["package_option_code"],
            })
            item_number += 1
    return items


def _build_addon_list(family_data):
    import re
    addons = []
    if not (family_data and family_data.get("package_variants")):
        return addons
    addon_number = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            name_lower = option["name"].lower()
            if any(a in name_lower for a in ADDON_APP_NAMES):
                raw = option["name"]
                m = re.match(r'^(.+?)\s+(\d+\s*GB)$', raw, re.IGNORECASE)
                if m:
                    label = m.group(1)
                    size = m.group(2).upper()
                else:
                    label = raw
                    size = ""
                addons.append({
                    "number": addon_number,
                    "label": label,
                    "size": size,
                    "price": option["price"],
                    "variant_name": variant["name"],
                    "option_code": option["package_option_code"],
                })
            addon_number += 1
    return addons


def _extract_xcp_packages(family_data):
    packages = []
    if not (family_data and family_data.get("package_variants")):
        return packages
    TARGET_OPTIONS = {25, 33, 35}
    option_number = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            if option_number in TARGET_OPTIONS:
                packages.append({
                    "number": option_number,
                    "variant_name": variant["name"],
                    "option_name": option["name"],
                    "price": option["price"],
                    "option_code": option["package_option_code"],
                })
            option_number += 1
    return packages


# ─── Beli Paket ──────────────────────────────────────────────────────────────


def _get_xl_info(active_xl):
    if not active_xl or not active_xl.refresh_token:
        return None
    try:
        _api_delay()
        tokens = _get_xl_tokens(active_xl)
        if not tokens:
            return None
        id_token = tokens.get("id_token", "")
        _api_delay()
        balance_data = xl_get_balance(API_KEY, id_token)
        if not balance_data:
            return None
        expiry_ts = balance_data.get("expired_at")
        expiry_date = datetime.fromtimestamp(expiry_ts, tz=WIB).strftime("%d %b %Y") if expiry_ts else None
        return {
            "main_credit": balance_data.get("remaining"),
            "expiry_date": expiry_date,
            "label": active_xl.label,
        }
    except Exception as e:
        print(f"[xl_info] Failed to fetch XL info: {e}")
        return None


@app.get("/user/xl/beli-paket", response_class=HTMLResponse)
def user_xl_beli_paket(request: Request, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()

    ctx.update({
        "request": request,
        "family_name": "Xtra Combo Plus",
    })
    return render("user/beli_paket.html", context=ctx)


def _sse_event(event, obj):
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(obj, ensure_ascii=False))


def _stream_beli_paket_events(active_xl, want):
    error = False
    fetched_at = None
    xl_info = None
    fam_want = [f for f in ("xcp", "addon10", "addon15", "xtraconf") if f in want]
    need_tokens = bool(fam_want) or ("meta" in want)

    tokens = None
    if active_xl and active_xl.refresh_token and need_tokens:
        try:
            _api_delay()
            tokens = _get_xl_tokens(active_xl)
        except Exception as e:
            print(f"[beli-paket] Error: {e}")
            error = True
        if tokens:
            if "meta" in want:
                _api_delay()
                try:
                    balance_data = xl_get_balance(API_KEY, tokens.get("id_token", ""))
                    if balance_data:
                        expiry_ts = balance_data.get("expired_at")
                        expiry_date = datetime.fromtimestamp(expiry_ts, tz=WIB).strftime("%d %b %Y") if expiry_ts else None
                        xl_info = {
                            "main_credit": balance_data.get("remaining"),
                            "expiry_date": expiry_date,
                            "label": active_xl.label,
                        }
                except Exception as e:
                    print(f"[beli-paket] Error: {e}")
                    error = True
        else:
            error = True

    yield _sse_event("meta", {"xl_info": xl_info})

    if tokens:
        for f in fam_want:
            fam_code, builder, entry_key = _FAMILY_FETCHERS[f]
            is_ent, mig = _family_api_params(fam_code)
            _api_delay()
            ok = True
            result = []
            try:
                data = xl_get_family(API_KEY, tokens, fam_code, is_enterprise=is_ent, migration_type=mig)
                result = builder(data) if data else []
            except Exception as e:
                print(f"[beli-paket] Error: {e}")
                ok = False
                error = True
            yield _sse_event("family", {"key": entry_key, "items": result, "ok": ok})
        fetched_at = time.time()

    yield _sse_event("done", {
        "error": error,
        "fetched_at": fetched_at,
        "last_refresh": datetime.now(WIB).strftime("%d %b %Y, %H:%M WIB"),
    })


@app.get("/user/xl/beli-paket/stream")
def user_xl_beli_paket_stream(request: Request, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"error": True}, status_code=403)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    want = {x for x in (request.query_params.get("families") or "").split(",") if x in ("meta", "xcp", "addon10", "addon15", "xtraconf")}
    return StreamingResponse(
        _stream_beli_paket_events(ctx.get("active_xl"), want),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


_FAMILY_FETCHERS = {
    "xcp": (FAMILY_CODE_XTRA_COMBO, _extract_xcp_packages, "packages"),
    "addon10": (FAMILY_CODE_ADDON, _build_addon_list, "addons"),
    "addon15": (FAMILY_CODE_ADDON_15, _build_addon_list, "addons_15"),
    "xtraconf": (FAMILY_CODE_XTRA_CONFERENCE, _build_family_list, "xtra_conf"),
}


def _family_api_params(family_code):
    if family_code == FAMILY_CODE_XTRA_CONFERENCE:
        return True, "PRIOH_TO_PRIO"
    return False, "NONE"


# ─── Detail Paket ───────────────────────────────────────────────────────────

def _build_pkg_detail(pkg, option, variant, url_id, number=None):
    po = pkg.get("package_option", {})
    pf = pkg.get("package_family", {})
    pv = pkg.get("package_detail_variant", {})
    return {
        "number": number,
        "url_id": url_id,
        "option_name": option["name"],
        "variant_name": variant["name"],
        "family_name": pf.get("name", ""),
        "price": option["price"],
        "validity": po.get("validity", ""),
        "point": po.get("point", 0),
        "plan_type": pf.get("plan_type", ""),
        "benefits": po.get("benefits", []),
        "tnc": po.get("tnc", ""),
        "option_code": option["package_option_code"],
        "token_confirmation": pkg.get("token_confirmation", ""),
        "payment_for": pf.get("payment_for", "BUY_PACKAGE"),
    }


@app.get("/user/xl/beli-paket/xcp-{option_number}/detail", response_class=HTMLResponse)
def user_xl_detail_paket(request: Request, option_number: int, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx.update({"request": request, "family": "xcp", "n": option_number})
    return render("user/detail_paket.html", context=ctx)


def _fetch_xcp_detail(option_number, active_xl, tokens=None):
    if not tokens:
        _api_delay()
        tokens = _get_xl_tokens(active_xl)
        if not tokens:
            return None
    _api_delay()
    family_data = xl_get_family(API_KEY, tokens, FAMILY_CODE_XTRA_COMBO, is_enterprise=False, migration_type="NONE")
    if not (family_data and family_data.get("package_variants")):
        return None
    option_number_local = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            if option_number_local == option_number:
                _api_delay()
                pkg = xl_get_package(API_KEY, tokens, option["package_option_code"], FAMILY_CODE_XTRA_COMBO, variant["package_variant_code"])
                if pkg:
                    return _build_pkg_detail(pkg, option, variant, f"xcp-{option_number}", option_number)
                return None
            option_number_local += 1
    return None


@app.get("/user/xl/beli-paket/addon10-xcp-{option_number}/detail", response_class=HTMLResponse)
def user_xl_detail_addon(request: Request, option_number: int, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx.update({"request": request, "family": "addon10", "n": option_number})
    return render("user/detail_paket.html", context=ctx)


@app.get("/user/xl/beli-paket/addon15-xcp-{option_number}/detail", response_class=HTMLResponse)
def user_xl_detail_addon15(request: Request, option_number: int, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx.update({"request": request, "family": "addon15", "n": option_number})
    return render("user/detail_paket.html", context=ctx)


@app.get("/user/xl/beli-paket/xtraconf-{option_number}/detail", response_class=HTMLResponse)
def user_xl_detail_xtraconf(request: Request, option_number: int, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    ctx.update({"request": request, "family": "xtraconf", "n": option_number})
    return render("user/detail_paket.html", context=ctx)


def _stream_detail_events(family, option_number, active_xl, want):
    """Single refresh-token stream for detail page: meta (banner) -> delay -> detail."""
    error = False
    want_meta = "meta" in want
    want_detail = "detail" in want
    xl_info = None
    detail = None

    tokens = None
    if active_xl and active_xl.refresh_token and (want_meta or want_detail):
        try:
            _api_delay()
            tokens = _get_xl_tokens(active_xl)
        except Exception as e:
            print(f"[detail-stream] Error: {e}")
            error = True
        if tokens:
            if want_meta:
                _api_delay()
                try:
                    balance_data = xl_get_balance(API_KEY, tokens.get("id_token", ""))
                    if balance_data:
                        expiry_ts = balance_data.get("expired_at")
                        expiry_date = datetime.fromtimestamp(expiry_ts, tz=WIB).strftime("%d %b %Y") if expiry_ts else None
                        xl_info = {
                            "main_credit": balance_data.get("remaining"),
                            "expiry_date": expiry_date,
                            "label": active_xl.label,
                        }
                except Exception as e:
                    print(f"[detail-stream] Error: {e}")
                    error = True
        else:
            error = True

    yield _sse_event("meta", {"xl_info": xl_info})

    if want_detail:
        if tokens:
            _api_delay()
            try:
                if family == "xcp":
                    detail = _fetch_xcp_detail(option_number, active_xl, tokens)
                elif family == "addon10":
                    detail = _get_addon_detail(option_number, ADDON_SPEC_10, active_xl, tokens)
                elif family == "addon15":
                    detail = _get_addon_detail(option_number, ADDON_SPEC_15, active_xl, tokens)
                elif family == "xtraconf":
                    detail = _get_addon_detail(option_number, XTRA_CONF_SPEC, active_xl, tokens)
            except Exception as e:
                print(f"[detail-stream] {family}-{option_number} error: {e}")
                error = True
        yield _sse_event("detail", {"ok": bool(detail), "detail": detail})
    yield _sse_event("done", {"error": error})


@app.get("/user/xl/beli-paket/detail-stream")
def user_xl_detail_stream(request: Request, family: str, n: int, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    want = {x for x in (request.query_params.get("families") or "meta,detail").split(",") if x in ("meta", "detail")}
    return StreamingResponse(
        _stream_detail_events(family, n, ctx.get("active_xl"), want),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/user/xl/banner-info")
def user_xl_banner_info(user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "xl_info": None}, status_code=403)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    return JSONResponse({"ok": True, "xl_info": _get_xl_info(ctx.get("active_xl"))})


# ─── Payment Routes ─────────────────────────────────────────────────────────

def _get_payment_items_and_detail(option_number, active_xl):
    """Helper: get package detail + payment items for a given option_number."""
    _api_delay()
    tokens = _get_xl_tokens(active_xl)
    if not tokens:
        return None, None
    _api_delay()
    family_data = xl_get_family(API_KEY, tokens, FAMILY_CODE_XTRA_COMBO, is_enterprise=False, migration_type="NONE")
    if not family_data:
        return None, None
    option_number_local = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            if option_number_local == option_number:
                _api_delay()
                pkg = xl_get_package(API_KEY, tokens, option["package_option_code"], FAMILY_CODE_XTRA_COMBO, variant["package_variant_code"])
                if not pkg:
                    return None, None
                detail = _build_pkg_detail(pkg, option, variant, f"xcp-{option_number}", option_number)
                price = int(option["price"])
                items = [PaymentItem(
                    item_code=option["package_option_code"],
                    product_type="",
                    item_price=price,
                    item_name=f"{variant['name']} {option['name']}".strip(),
                    tax=0,
                    token_confirmation=pkg.get("token_confirmation", ""),
                )]
                return items, detail
            option_number_local += 1
    return None, None


def _get_addon_items_and_detail(option_number, addon_spec, active_xl, tokens=None):
    """Helper: get addon detail + payment items for a given option_number."""
    family_code, url_id_prefix = addon_spec
    if not tokens:
        _api_delay()
        tokens = _get_xl_tokens(active_xl)
        if not tokens:
            return None, None
    is_ent, mig = _family_api_params(family_code)
    _api_delay()
    family_data = xl_get_family(API_KEY, tokens, family_code, is_enterprise=is_ent, migration_type=mig)
    if not family_data:
        return None, None
    option_number_local = 1
    for variant in family_data["package_variants"]:
        for option in variant["package_options"]:
            if option_number_local == option_number:
                _api_delay()
                pkg = xl_get_package(API_KEY, tokens, option["package_option_code"], family_code, variant["package_variant_code"])
                if not pkg:
                    return None, None
                detail = _build_pkg_detail(pkg, option, variant, f"{url_id_prefix}-{option_number}", option_number)
                price = int(option["price"])
                items = [PaymentItem(
                    item_code=option["package_option_code"],
                    product_type="",
                    item_price=price,
                    item_name=f"{variant['name']} {option['name']}".strip(),
                    tax=0,
                    token_confirmation=pkg.get("token_confirmation", ""),
                )]
                return items, detail
            option_number_local += 1
    return None, None


def _get_addon_detail(option_number, addon_spec, active_xl, tokens=None):
    items, detail = _get_addon_items_and_detail(option_number, addon_spec, active_xl, tokens)
    return detail


def _process_payment(active_xl, option_number, addon_spec, method):
    pay_error = None
    pay_success = None
    detail = None
    pay_extra = {}
    _stdout_buf = io.StringIO()
    with redirect_stdout(_stdout_buf):
        if active_xl and active_xl.refresh_token:
            if addon_spec:
                items, detail = _get_addon_items_and_detail(option_number, addon_spec, active_xl)
            else:
                items, detail = _get_payment_items_and_detail(option_number, active_xl)
            if items:
                try:
                    _api_delay()
                    tokens = _get_xl_tokens(active_xl)
                    if method == "balance":
                        from app.client.purchase.balance import settlement_balance as pay_balance
                        _api_delay()
                        res = pay_balance(API_KEY, tokens, items, detail["payment_for"], False, overwrite_amount=detail["price"])
                        if res and res.get("status") == "SUCCESS":
                            pay_success = "Pembelian berhasil! Silakan cek aplikasi MyXL."
                        else:
                            pay_error = f"Pembayaran gagal: {res.get('message', 'Unknown error') if res else 'No response'}"
                    elif method == "qris":
                        from app.client.purchase.qris import show_qris_payment
                        _api_delay()
                        qris_result = show_qris_payment(API_KEY, tokens, items, detail["payment_for"], False, overwrite_amount=detail["price"])
                        if qris_result:
                            qris_b64, qris_txn_id = qris_result
                            pay_success = "QRIS berhasil dibuat. Silakan pindai kode QR untuk menyelesaikan pembayaran."
                            pay_extra["qris_b64"] = qris_b64
                            pay_extra["qris_transaction_id"] = qris_txn_id
                        else:
                            pay_error = "Gagal membuat QRIS."
                    elif method == "ewallet":
                        from app.client.purchase.ewallet import show_multipayment
                        _api_delay()
                        show_multipayment(API_KEY, tokens, items, detail["payment_for"], False)
                        pay_success = "Silakan selesaikan pembayaran di aplikasi E-Wallet Anda."
                    else:
                        pay_error = "Metode pembayaran tidak dikenal."
                except Exception as e:
                    pay_error = f"Error: {e}"
            else:
                pay_error = "Paket tidak ditemukan."
        else:
            pay_error = "Akun XL tidak aktif."
    pay_extra["terminal_output"] = _stdout_buf.getvalue()
    return detail, pay_error, pay_success, pay_extra


def _deduct_token_balance(user, amount, description):
    db = next(get_db())
    try:
        bal = db.query(Balance).filter(Balance.user_id == user.id).first()
        if not bal:
            return None
        bal.balance = max(0, bal.balance - amount)
        db.add(BalanceTransaction(user_id=user.id, amount=-amount, type="purchase", description=description))
        db.commit()
        return bal.balance
    finally:
        db.close()


PAY_METHOD_LABELS = {
    "balance": "Pulsa XL",
    "qris": "QRIS",
}

FAMILY_FEE_DEFAULTS = {
    "xcp": 2000,
    "addon10": 1000,
    "addon15": 1000,
    "xtraconf": 1000,
}

FAMILY_LABELS = {
    "xcp": "Xtra Combo Plus",
    "addon10": "Addon Xtra Combo Plus 10GB",
    "addon15": "Addon Xtra Combo Plus 15GB",
    "xtraconf": "Xtra Conference",
}


def _family_key_from_url_id(url_id):
    for key in ("addon10", "addon15", "xtraconf", "xcp"):
        if str(url_id).startswith(key):
            return key
    return "xcp"


def _get_family_fee(family_key):
    db = next(get_db())
    try:
        row = db.query(FamilyFee).filter(FamilyFee.family_key == family_key).first()
        return row.fee if row else FAMILY_FEE_DEFAULTS.get(family_key, 0)
    finally:
        db.close()


def _set_family_fee(family_key, fee):
    db = next(get_db())
    try:
        row = db.query(FamilyFee).filter(FamilyFee.family_key == family_key).first()
        if row:
            row.fee = fee
        else:
            db.add(FamilyFee(family_key=family_key, fee=fee))
        db.commit()
    finally:
        db.close()


def _get_all_family_fees():
    db = next(get_db())
    try:
        rows = db.query(FamilyFee).all()
        stored = {r.family_key: r.fee for r in rows}
        return {k: stored.get(k, v) for k, v in FAMILY_FEE_DEFAULTS.items()}
    finally:
        db.close()


def _checkout_detail(active_xl, fetch_fn):
    if not (active_xl and active_xl.refresh_token):
        return None
    return fetch_fn()


def _checkout_context(active_xl, user, detail, method, family_key):
    db = next(get_db())
    try:
        bal = db.query(Balance).filter(Balance.user_id == user.id).first()
        balance = bal.balance if bal else 0
    finally:
        db.close()
    fee = _get_family_fee(family_key)
    remaining = balance - fee
    return {
        "detail": detail,
        "method": method,
        "method_label": PAY_METHOD_LABELS.get(method, method),
        "balance": balance,
        "price": detail.get("price") or 0,
        "fee": fee,
        "family_label": FAMILY_LABELS.get(family_key, family_key),
        "remaining": remaining,
        "insufficient": remaining < 0,
        "pay_url": f"/user/xl/beli-paket/{detail.get('url_id')}/pay/{method}",
    }


@app.get("/user/xl/beli-paket/xcp-{option_number}/checkout/{method}")
def checkout_paket(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if method not in PAY_METHOD_LABELS:
        return RedirectResponse(url="/user/xl/beli-paket", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    active_xl = ctx.get("active_xl")
    detail = _checkout_detail(active_xl, lambda: _fetch_xcp_detail(option_number, active_xl))
    if not detail:
        return RedirectResponse(url=f"/user/xl/beli-paket/xcp-{option_number}/detail", status_code=303)
    cc = _checkout_context(active_xl, user, detail, method, "xcp")
    ctx.update({"request": request, **cc})
    return render("user/checkout.html", context=ctx)


@app.get("/user/xl/beli-paket/addon10-xcp-{option_number}/checkout/{method}")
def checkout_addon(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if method not in PAY_METHOD_LABELS:
        return RedirectResponse(url="/user/xl/beli-paket", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    active_xl = ctx.get("active_xl")
    detail = _checkout_detail(active_xl, lambda: _get_addon_detail(option_number, ADDON_SPEC_10, active_xl))
    if not detail:
        return RedirectResponse(url=f"/user/xl/beli-paket/addon10-xcp-{option_number}/detail", status_code=303)
    cc = _checkout_context(active_xl, user, detail, method, "addon10")
    ctx.update({"request": request, **cc})
    return render("user/checkout.html", context=ctx)


@app.get("/user/xl/beli-paket/addon15-xcp-{option_number}/checkout/{method}")
def checkout_addon15(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if method not in PAY_METHOD_LABELS:
        return RedirectResponse(url="/user/xl/beli-paket", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    active_xl = ctx.get("active_xl")
    detail = _checkout_detail(active_xl, lambda: _get_addon_detail(option_number, ADDON_SPEC_15, active_xl))
    if not detail:
        return RedirectResponse(url=f"/user/xl/beli-paket/addon15-xcp-{option_number}/detail", status_code=303)
    cc = _checkout_context(active_xl, user, detail, method, "addon15")
    ctx.update({"request": request, **cc})
    return render("user/checkout.html", context=ctx)


@app.get("/user/xl/beli-paket/xtraconf-{option_number}/checkout/{method}")
def checkout_xtraconf(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return RedirectResponse(url="/admin/dashboard", status_code=303)
    if method not in PAY_METHOD_LABELS:
        return RedirectResponse(url="/user/xl/beli-paket", status_code=303)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    active_xl = ctx.get("active_xl")
    detail = _checkout_detail(active_xl, lambda: _get_addon_detail(option_number, XTRA_CONF_SPEC, active_xl))
    if not detail:
        return RedirectResponse(url=f"/user/xl/beli-paket/xtraconf-{option_number}/detail", status_code=303)
    cc = _checkout_context(active_xl, user, detail, method, "xtraconf")
    ctx.update({"request": request, **cc})
    return render("user/checkout.html", context=ctx)


@app.post("/user/xl/beli-paket/xcp-{option_number}/pay/{method}")
def pay_paket(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "message": "Akses ditolak"}, status_code=403)
    if method not in PAY_METHOD_LABELS:
        return JSONResponse({"ok": False, "message": "Metode pembayaran tidak tersedia."}, status_code=400)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    detail, pay_error, pay_success, pay_extra = _process_payment(ctx.get("active_xl"), option_number, False, method)
    return _pay_response(user, detail, pay_error, pay_success, method, "xcp", pay_extra)


@app.post("/user/xl/beli-paket/addon10-xcp-{option_number}/pay/{method}")
def pay_addon(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "message": "Akses ditolak"}, status_code=403)
    if method not in PAY_METHOD_LABELS:
        return JSONResponse({"ok": False, "message": "Metode pembayaran tidak tersedia."}, status_code=400)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    detail, pay_error, pay_success, pay_extra = _process_payment(ctx.get("active_xl"), option_number, ADDON_SPEC_10, method)
    return _pay_response(user, detail, pay_error, pay_success, method, "addon10", pay_extra)


@app.post("/user/xl/beli-paket/addon15-xcp-{option_number}/pay/{method}")
def pay_addon15(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "message": "Akses ditolak"}, status_code=403)
    if method not in PAY_METHOD_LABELS:
        return JSONResponse({"ok": False, "message": "Metode pembayaran tidak tersedia."}, status_code=400)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    detail, pay_error, pay_success, pay_extra = _process_payment(ctx.get("active_xl"), option_number, ADDON_SPEC_15, method)
    return _pay_response(user, detail, pay_error, pay_success, method, "addon15", pay_extra)


@app.post("/user/xl/beli-paket/xtraconf-{option_number}/pay/{method}")
def pay_xtraconf(request: Request, option_number: int, method: str, user: User = Depends(get_current_user)):
    if user.role != "user":
        return JSONResponse({"ok": False, "message": "Akses ditolak"}, status_code=403)
    if method not in PAY_METHOD_LABELS:
        return JSONResponse({"ok": False, "message": "Metode pembayaran tidak tersedia."}, status_code=400)
    db = next(get_db())
    ctx = get_user_context(user, db)
    db.close()
    detail, pay_error, pay_success, pay_extra = _process_payment(ctx.get("active_xl"), option_number, XTRA_CONF_SPEC, method)
    return _pay_response(user, detail, pay_error, pay_success, method, "xtraconf", pay_extra)


def _qris_png_data_uri(qris_b64):
    import base64 as _b64
    import io as _io
    import qrcode as _qrcode
    try:
        data = _b64.urlsafe_b64decode(qris_b64.encode()).decode()
        qr = _qrcode.QRCode(error_correction=_qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _fetch_pending_qris(active_xl, tokens=None, transactions=None):
    import base64 as _b64
    qris_txs = []
    matched_codes = set()
    if not active_xl or not active_xl.refresh_token:
        return qris_txs, matched_codes
    try:
        if tokens is None:
            _api_delay()
            tokens = _get_xl_tokens(active_xl)
            if not tokens:
                return qris_txs, matched_codes
        if transactions is None:
            _api_delay()
            hist = xl_get_transactions(API_KEY, tokens) or {}
        else:
            hist = transactions or {}
        for trx in hist.get("list", []):
            pm = (trx.get("payment_method") or "").upper()
            st = (trx.get("status") or "").upper()
            if "QRIS" not in pm or st not in ("READY", "PENDING", "WAITING_PAYMENT", "ONGOING", "PROCESS"):
                continue
            code = trx.get("code") or ""
            if not code or code in matched_codes:
                continue
            matched_codes.add(code)
            payload = {"transaction_id": code, "is_enterprise": False, "lang": "en", "status": ""}
            _api_delay()
            res = send_api_request(API_KEY, "payments/api/v8/pending-detail", payload, tokens["id_token"], "POST")
            if not isinstance(res, dict) or res.get("status") != "SUCCESS":
                continue
            detail = res.get("data") or {}
            qr_raw = detail.get("qr_code")
            if not qr_raw:
                continue
            qris_b64 = _b64.urlsafe_b64encode(qr_raw.encode()).decode()
            remaining = int(detail.get("remaining_time") or 0)
            expires_ts = int(time.time()) + remaining if remaining > 0 else 0
            st_detail = (detail.get("status") or "").upper()
            pending_st = ("READY", "PENDING", "WAITING_FOR_PAYMENT", "WAITING_PAYMENT", "ONGOING", "PROCESS")
            expired = st_detail == "EXPIRED" or (remaining <= 0 and st_detail not in pending_st)
            raw_ts = trx.get("timestamp")
            qris_txs.append({
                "transaction_id": detail.get("payment_id") or code,
                "option_name": trx.get("title") or trx.get("product_name") or "Paket",
                "amount": trx.get("raw_price") or 0,
                "status": trx.get("status"),
                "created_at": detail.get("formated_date") or trx.get("formated_date") or "",
                "ts_epoch": (int(raw_ts) - 7 * 3600) if raw_ts else 0,
                "expires_ts": expires_ts,
                "expired": expired,
                "img": _qris_png_data_uri(qris_b64),
                "qris_b64": qris_b64,
            })
    except Exception as e:
        print(f"[fetch_pending_qris] Error: {e}")
    return qris_txs, matched_codes


def _reconcile_qris_record(q, tokens, db):
    """Resolve a stored QRIS record against XL pending-detail.

    FINISHED/SUCCESS -> mark done & drop (paid; the XL history row covers it).
    EXPIRED/timeout   -> mark expired & show as expired.
    Still pending     -> show as pending with live remaining time.
    API failure       -> keep showing as pending (no data loss).
    """
    st = (q.status or "PENDING").upper()
    remaining = 0
    resolved = False
    if st == "PENDING" and tokens:
        try:
            _api_delay()
            payload = {"transaction_id": q.transaction_id, "is_enterprise": False, "lang": "en", "status": ""}
            res = send_api_request(API_KEY, "payments/api/v8/pending-detail", payload, tokens["id_token"], "POST")
            if isinstance(res, dict) and res.get("status") == "SUCCESS":
                d = res.get("data") or {}
                st = (d.get("status") or "PENDING").upper()
                remaining = int(d.get("remaining_time") or 0)
                resolved = True
                if st != (q.status or "PENDING").upper():
                    q.status = st
                    db.commit()
        except Exception as e:
            print(f"[history] reconcile qris {q.transaction_id}: {e}")

    if st in ("FINISHED", "SUCCESS"):
        return None

    if resolved or st == "EXPIRED":
        pending_st = ("PENDING", "READY", "WAITING_FOR_PAYMENT", "WAITING_PAYMENT", "ONGOING", "PROCESS")
        expired = st == "EXPIRED" or (remaining <= 0 and st not in pending_st)
    else:
        expired = False

    return {
        "transaction_id": q.transaction_id,
        "option_name": q.option_name,
        "amount": q.amount,
        "status": st,
        "created_at": _fmt_wib(q.created_at) if q.created_at else "",
        "ts_epoch": int(q.created_at.replace(tzinfo=timezone.utc).timestamp()) if q.created_at else 0,
        "expires_ts": int(time.time()) + remaining if remaining > 0 else 0,
        "expired": expired,
        "img": _qris_png_data_uri(q.qris_b64),
    }


def _save_qris_transaction(user, transaction_id, qris_b64, detail):
    db = next(get_db())
    try:
        db.add(QrisTransaction(
            user_id=user.id,
            transaction_id=transaction_id,
            qris_b64=qris_b64,
            option_name=detail.get("option_name", "") if detail else "",
            amount=detail.get("price") or 0 if detail else 0,
            status="PENDING",
        ))
        db.commit()
    finally:
        db.close()


def _pay_response(user, detail, pay_error, pay_success, method, family_key, pay_extra=None):
    if pay_success:
        fee = _get_family_fee(family_key)
        new_balance = _deduct_token_balance(
            user,
            fee,
            f"Konsumsi saldo token {FAMILY_LABELS.get(family_key, family_key)} via {PAY_METHOD_LABELS.get(method, method)}"
        )
        resp = {"ok": True, "message": pay_success, "deducted": fee, "new_balance": new_balance}
        qris_b64 = (pay_extra or {}).get("qris_b64")
        qris_txn_id = (pay_extra or {}).get("qris_transaction_id")
        if qris_b64:
            resp["qris_img"] = _qris_png_data_uri(qris_b64)
            if qris_txn_id:
                _save_qris_transaction(user, qris_txn_id, qris_b64, detail)
        terminal_output = (pay_extra or {}).get("terminal_output", "")
        if terminal_output:
            resp["terminal_output"] = terminal_output
        return JSONResponse(resp)
    resp = {"ok": False, "message": pay_error or "Pembayaran gagal."}
    terminal_output = (pay_extra or {}).get("terminal_output", "")
    if terminal_output:
        resp["terminal_output"] = terminal_output
    return JSONResponse(resp)


# ─── Register ───────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render("register.html", context={"request": request})


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    existing = db.query(User).filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing:
        return render("register.html", context={
            "request": request,
            "error": "Username atau email sudah terdaftar"
        }, status_code=400)

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        password=password,
        role="user"
    )
    db.add(user)
    db.flush()
    db.add(Balance(user_id=user.id, balance=0))
    db.commit()

    token = create_access_token({"sub": str(user.id), "role": "user"})
    resp = RedirectResponse(url="/user/dashboard", status_code=303)
    resp.set_cookie(key="access_token", value=token, httponly=True, max_age=900)
    return resp


# ─── Run ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
