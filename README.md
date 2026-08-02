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
├── main.py             # FastAPI app (routes, middleware, reconciliation)
├── auth.py             # JWT auth + seed admin
├── database.py         # SQLAlchemy engine + init
├── models.py           # ORM models
├── app/                # XL API client (engsel, ciam, purchase, crypto)
├── templates/          # Jinja2 templates
├── static/             # CSS / JS
├── requirements.txt
├── install.sh          # one-command installer (Debian 12 + systemd)
├── docker-compose.yml  # container Debian 12 dengan systemd
├── .env                # konfigurasi kredensial XL API (sudah terisi)
└── .env.example        # template konfigurasi
```

## Quick start (Docker + systemd)

Instalasi dilakukan di dalam container Debian 12 yang ber-systemd, sehingga `install.sh` bisa membuat service systemd sungguhan.

```bash
git clone https://github.com/rohcuan/rohtembak-xl.git
cd rohtembak-xl
docker compose up -d

docker exec -it rohtembak bash
wget -O install.sh https://raw.githubusercontent.com/rohcuan/rohtembak-xl/main/install.sh
chmod +x install.sh
./install.sh
```

Buka `http://localhost:8000` — login `admin` / `admin`.

> Pada host cgroup v2, jika container gagal start, tambahkan `cgroupns: host` pada service di `docker-compose.yml`.

## Manual (Debian 12 tanpa Docker)

```bash
apt-get update
apt-get install -y python3 python3-venv python3-pip git
git clone https://github.com/rohcuan/rohtembak-xl.git /opt/rohtembak
cd /opt/rohtembak
./install.sh        # jalankan sebagai root
```

## Konfigurasi

`.env` (kredensial XL API) **sudah termasuk** di repo dan dikloning bersama source — panel langsung siap pakai tanpa isi prompt.

Yang TIDAK ikut di-commit:

- `data/*.db` — database runtime berisi **refresh token / access token** akun XL tiap nomor. Instalasi baru = panel kosong, belum ada nomor yang login OTP.
- `ax.fp` — device fingerprint; dibuat ulang otomatis dari `AX_FP_KEY` saat pertama kali app berjalan.

## Akun default

- Admin: `admin` / `admin` — **ganti segera setelah deploy** (di database `users`).

## Keamanan

- Kredensial API di `.env` sengaja dipublish karena sudah tersebar publik (didapat dari pencarian Google).
- Yang tetap dilindungi `gitignore`: `data/*.db` (token refresh/access XL per nomor), `ax.fp`, `venv/`, `__pycache__/`.
- Login nomor XL dilakukan via menu panel (input nomor + OTP) dan tersimpan hanya di database lokal.

## Troubleshooting

```bash
docker exec -it rohtembak bash
systemctl status rohtembak
journalctl -u rohtembak -n 50 -f
systemctl restart rohtembak
```
