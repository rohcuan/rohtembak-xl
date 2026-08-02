#!/usr/bin/env python3
"""Validasi jam server & semantik epoch XL terhadap sumber waktu otoritatif (HTTPS Date header / NTP).

Penggunaan:
  python3 check_clock.py            # cek akurasi jam server (UTC vs sumber online)
  python3 check_clock.py wib         # tampilkan waktu UTC/WIB lokal + selisih offset
  python3 check_clock.py xl          # tentukan konversi timestamp XL dari data riil (stdin JSON)

Contoh untuk mode xl (beri JSON transaksi dari API XL yang berisi timestamp & formated_date):
  echo '{"timestamp": 1785639600, "formated_date": "02 August 2026 | 10:00"}' | python3 check_clock.py xl
"""
import json
import sys
import statistics
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    requests = None

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

SOURCES = [
    "https://github.com",
    "https://cloudflare.com",
    "https://www.google.com",
    "https://www.youtube.com",
    "https://www.wikipedia.org",
    "https://stackoverflow.com",
]

FORMAT_DATE_XL = ("%d %B %Y | %H:%M", "%d %b %Y | %H:%M", "%d %B %Y %H:%M", "%d %b %Y %H:%M")


def http_date_utc(url):
    if requests is None:
        raise RuntimeError("library 'requests' tidak terpasang")
    r = requests.get(url, timeout=10, allow_redirects=True)
    d = r.headers.get("Date")
    if not d:
        raise ValueError("tidak ada header Date")
    return datetime.strptime(d, "%a, %d %b %Y %H:%M:%S GMT").replace(tzinfo=UTC)


def fetch_authoritative_utc():
    samples = []
    for url in SOURCES:
        try:
            samples.append(http_date_utc(url))
            print(f"[ok  ] {url:38s} {http_date_utc(url).isoformat()}")
        except Exception as e:
            print(f"[gagal] {url:38s} {type(e).__name__}: {e}")
    if not samples:
        print("Tidak ada sumber waktu online yang dapat diakses.")
        sys.exit(1)
    med = statistics.median(s.timestamp() for s in samples)
    return datetime.fromtimestamp(med, tz=UTC)


def cmd_clock():
    local_utc = datetime.now(UTC)
    print()
    print(f"waktu lokal UTC : {local_utc.isoformat()}")
    print(f"waktu lokal WIB : {local_utc.astimezone(WIB).isoformat()}")
    print()
    ref = fetch_authoritative_utc()
    print()
    offset = ref.timestamp() - local_utc.timestamp()
    print(f"referensi   UTC : {ref.isoformat()}")
    print(f"selisih jam     : {offset:+.1f} detik")
    if abs(offset) <= 60:
        print("VERDICT: jam server AKURAT (dalam 60 detik). Konversi WIB = UTC + 7 jam adalah benar.")
        return 0
    print(f"VERDICT: jam server TIDAK AKURAT ({offset:+.1f} detik). Perbaiki jam/zone dulu "
          "sebelum mempercayai timestamp aplikasi.")
    return 1


def cmd_wib():
    now = datetime.now(WIB)
    print(f"sekarang WIB : {now.strftime('%Y-%m-%d %H:%M:%S WIB')}")
    print(f"sekarang UTC : {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"offset zona  : +7 jam (WIB = UTC + 7)")
    return 0


def parse_formated_date(fd):
    for fmt in FORMAT_DATE_XL:
        try:
            return datetime.strptime(str(fd).strip(), fmt)
        except ValueError:
            continue
    return None


def cmd_xl():
    try:
        trx = json.loads(sys.stdin.read())
    except json.JSONDecodeError as e:
        print(f"JSON tidak valid: {e}")
        return 1
    ts = trx.get("timestamp")
    fd = trx.get("formated_date")
    if ts is None or not fd:
        print("Butuh field 'timestamp' (epoch detik) dan 'formated_date' (string waktu XL).")
        return 1
    ts = int(ts)
    wall = parse_formated_date(fd)
    if wall is None:
        print(f"formated_date tidak dikenal formatnya: {fd!r}")
        return 1

    true_epoch = wall.replace(tzinfo=WIB).timestamp()
    fake_epoch = wall.replace(tzinfo=UTC).timestamp()
    diff_true = ts - true_epoch
    diff_fake = ts - fake_epoch

    print(f"formated_date  : {fd!r}  (wall-clock WIB)")
    print(f"timestamp      : {ts}  ({datetime.fromtimestamp(ts, tz=UTC).isoformat()} UTC / "
          f"{datetime.fromtimestamp(ts, tz=WIB).isoformat()} WIB)")
    print(f"epoch jika benar instan WIB   : {int(true_epoch)}  (selisih {diff_true:+.0f}s)")
    print(f"epoch jika wall-clock palsu   : {int(fake_epoch)}  (selisih {diff_fake:+.0f}s)")
    print()
    if abs(diff_true) <= 2:
        print("VERDICT: timestamp XL = instan absolut. TAMPILKAN dalam WIB (konversi +7 jam) — "
              "konfigurasi sekarang sudah benar.")
    elif abs(diff_fake) <= 2:
        print("VERDICT: timestamp XL sudah wall-clock WIB (fake UTC). JANGAN tambah 7 jam — "
              "tampilkan apa adanya (UTC). Perlu revert _fmt_xl_ts ke tz=UTC.")
    else:
        print("VERDICT: tidak bisa dipastikan; selisih tidak cocok dengan kedua model. "
              "Cek kembali data.")
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "clock"
    if mode == "wib":
        return cmd_wib()
    if mode == "xl":
        return cmd_xl()
    return cmd_clock()


if __name__ == "__main__":
    sys.exit(main())
