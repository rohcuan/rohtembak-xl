# RohTembak (XL)

Web UI untuk mengelola akun XL / paket XL (beli paket, info paket, riwayat, pembayaran QRIS) berbasis FastAPI.

## Fitur

- Login & registrasi pengguna (admin / user)
- Tambah & kelola akun XL
- Beli paket (Xtra Combo Plus, Addon, Xtra Conference) via XL API
- Info paket, pemakaian kuota, status perpanjangan otomatis
- Riwayat transaksi + pembayaran QRIS dengan rekonsiliasi otomatis
- QRIS duplikat otomatis dibersihkan (dedup by `transaction_id` / `qris_b64`)
- Konkurensi aman (semaphore + thread pool) untuk panggilan XL API

## Struktur

```
.
├── main.py               # FastAPI app (routes, middleware, reconciliation)
├── auth.py               # JWT auth + seed admin
├── database.py           # SQLAlchemy engine + init
├── models.py             # ORM models
├── app/                  # XL API client (engsel, ciam, purchase, crypto)
├── templates/            # Jinja2 templates
├── static/               # CSS / JS
├── requirements.txt
├── install-dev-staging.sh    # dev/staging installer (apt + venv + uvicorn)
├── reinstall-dev-staging.sh  # dev/staging clean reinstall (retain data)
├── entrypoint.sh         # production Docker entrypoint (install + run)
├── docker-compose.yml    # production container
├── .env                  # konfigurasi kredensial XL API (sudah terisi)
└── .env.example          # template konfigurasi
```

## Quick start (Docker, production)

```bash
git clone https://github.com/rohcuan/rohtembak-xl.git
cd rohtembak-xl
docker compose up -d
```

Buka `http://localhost:8000` — login `admin` / `admin`.

> Pada host cgroup v2, jika container gagal start, tambahkan `cgroupns: host` pada service di `docker-compose.yml`.

## Dev / Staging (bare metal, Debian 12)

Install atau reinstall ke `/opt/rohtembak`, start uvicorn di port 8000:

```bash
wget -O install-dev-staging.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install-dev-staging.sh
chmod +x install-dev-staging.sh
sudo ./install-dev-staging.sh
```

## Update / reinstall (retain data)

Untuk perubahan besar di repo: reinstall bersih tapi tetap menyimpan data
(`.env`, `data/` termasuk fingerprint per user). **Dev/staging only** — untuk production, backup volume lalu recreate container.

```bash
wget -O reinstall-dev-staging.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/reinstall-dev-staging.sh
chmod +x reinstall-dev-staging.sh
sudo ./reinstall-dev-staging.sh
```

## Konfigurasi

`.env` (kredensial XL API) **sudah termasuk** di repo dan dikloning bersama source — panel langsung siap pakai tanpa isi prompt.

Yang TIDAK ikut di-commit:

- `data/*.db` — database runtime berisi **refresh token / access token** akun XL tiap nomor. Instalasi baru = panel kosong, belum ada nomor yang login OTP.
- `data/ax.fp.{username}` — device fingerprint per user; dibuat otomatis saat pertama kali user berjalan. Setiap user mendapat fingerprint terpisah (max 10 nomor XL per fingerprint).
- `ax.fp` — shared device fingerprint (fallback untuk user baru sebelum per-user fp dibuat).

## Akun default

- Admin: `admin` / `admin` — **ganti segera setelah deploy** (di database `users`).

## Keamanan

- Kredensial API di `.env` sengaja dipublish karena sudah tersebar publik (didapat dari pencarian Google).
- Yang tetap dilindungi `gitignore`: `data/*.db` (token refresh/access XL per nomor), `data/ax.fp.*` (fingerprint per user), `ax.fp`, `venv/`, `__pycache__/`.
- Login nomor XL dilakukan via menu panel (input nomor + OTP) dan tersimpan hanya di database lokal.

## Troubleshooting

```bash
# Check uvicorn logs
tail -f /tmp/rohtembak.log

# Stop app
kill $(lsof -ti :8000)

# Restart app
cd /opt/rohtembak && setsid venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000 > /tmp/rohtembak.log 2>&1 &

# Check which process owns port 8000
lsof -i :8000
```
