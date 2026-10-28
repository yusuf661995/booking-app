#!/usr/bin/env python3
"""ATMS Conference Booking server.

Local:  python atms_booking_server.py [port]
Render: Start Command  python atms_booking_server.py   (Render supplies the PORT variable)

Storage: MongoDB Atlas when MONGODB_URI is set, otherwise bookings.json (see booking_store.py).
Optional site password: set ACCESS_PASSWORD (and optionally ACCESS_USER, default "atms") and the browser
asks for it once. Strongly recommended on a public Render address.
"""
import base64
import hmac
import json
import os
import re
import socket
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

import booking_store as store

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "atms_conference_booking.html")

OPEN_MIN, CLOSE_MIN, STEP = 9 * 60, 19 * 60, 30     # 9:00 AM - 7:00 PM, 30-min grid
AHEAD_DAYS = 31
ROOMS = ("conf", "lounge")
LOCK = threading.Lock()

# Cloud servers run on UTC, but the office clock is local. All "today / already past" checks use this zone.
IST = timezone(timedelta(hours=5, minutes=30))
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("APP_TIMEZONE") or "Asia/Kolkata")
except Exception:  # Python < 3.9, or Windows without the tzdata package
    TZ = IST
    print("NOTE: using fixed India time (UTC+05:30). Install 'tzdata' to use APP_TIMEZONE.", file=sys.stderr)

DEFAULT_PIN = "1234"                                            # same default as the web page


def hash_pin(pin):
    """Same 32-bit FNV-1a hash the web page uses for the admin PIN."""
    h = 2166136261
    for ch in "ob-salt:" + pin:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return format(h, "x")


ACCESS_USER = os.environ.get("ACCESS_USER") or "atms"
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD") or ""


def now_local():
    return datetime.now(TZ).replace(tzinfo=None)  # naive wall-clock time in the office time zone


def clean(rec, now=None, existing=None):
    """Validate a booking coming from a browser. Returns (record, error_code).
    existing = the stored booking when editing (lets a meeting already in progress keep its start time)."""
    now = now or now_local()
    try:
        room = rec["room"]
        date = rec["date"]
        start, end = int(rec["start"]), int(rec["end"])
        title = str(rec.get("title", "")).strip()[:80]
        who = str(rec.get("who", "")).strip()[:60]
    except Exception:
        return None, "invalid"
    if room not in ROOMS or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
        return None, "invalid"
    try:
        day = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return None, "invalid"
    if day.weekday() == 6 or day.date() > (now + timedelta(days=AHEAD_DAYS)).date():
        return None, "invalid"                                  # Sundays / too far ahead
    if (start % STEP or end % STEP or start < OPEN_MIN or end > CLOSE_MIN
            or end <= start or not title or not who):
        return None, "invalid"
    keep_start = bool(existing and existing.get("date") == date and existing.get("start") == start)
    now_min = now.hour * 60 + now.minute
    if day.date() < now.date() or (day.date() == now.date()
                                   and ((start < now_min and not keep_start) or end <= now_min)):
        return None, "past"
    return {
        "room": room, "date": date, "start": start, "end": end,
        "who": who, "title": title,
        "uid": str(rec.get("uid", ""))[:40],
        "blocked": bool(rec.get("blocked", False)),
        "createdAt": now.isoformat(timespec="seconds"),
    }, None


class Handler(BaseHTTPRequestHandler):
    server_version = "ATMSBooking/1.2"

    def log_message(self, fmt, *args):                          # quieter console
        sys.stderr.write("%s  %s\n" % (now_local().strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        """Optional shared password (HTTP Basic). Off unless ACCESS_PASSWORD is set."""
        if not ACCESS_PASSWORD:
            return True
        try:
            kind, _, token = (self.headers.get("Authorization") or "").partition(" ")
            user, _, pw = base64.b64decode(token).decode("utf-8").partition(":")
            if kind.lower() == "basic" and hmac.compare_digest(user.encode(), ACCESS_USER.encode()) \
                    and hmac.compare_digest(pw.encode(), ACCESS_PASSWORD.encode()):
                return True
        except Exception:
            pass
        self._send(401, b"Password required.", "text/plain",
                   {"WWW-Authenticate": 'Basic realm="ATMS Booking", charset="UTF-8"'})
        return False

    def _busy(self):
        self._send(503, {"error": {"code": "busy"}})

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":                                  # for Render's health check; no data, no password
            return self._send(200, b"ok", "text/plain")
        if not self._authorized():
            return
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, b"atms_conference_booking.html not found next to the server script.", "text/plain")
        elif path == "/api/state":
            try:
                with LOCK:
                    d = store.load()
            except store.StorageError:
                return self._busy()
            d.pop("_v", None)                                   # internal version number, not for browsers
            self._send(200, d)
        else:
            self._send(404, {"error": {"code": "notfound"}})

    def do_POST(self):
        if not self._authorized():
            return
        path = urlparse(self.path).path
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, {"error": {"code": "invalid"}})
        try:
            if path == "/api/bookings":
                rec, err = clean(body)
                if err:
                    return self._send(400, {"error": {"code": err}})
                with LOCK:                                      # check + write is one atomic step
                    d = store.load()
                    for b in d["bookings"]:
                        if b["room"] == rec["room"] and b["date"] == rec["date"] \
                                and rec["start"] < b["end"] and b["start"] < rec["end"]:
                            return self._send(409, {"error": {"code": "clash", "c": b}})
                    rec["id"] = uuid.uuid4().hex[:12]
                    d["bookings"].append(rec)
                    store.save(d)
                return self._send(201, {"id": rec["id"]})
            if path == "/api/pin":
                h = str(body.get("pinHash", ""))[:32]
                if not re.fullmatch(r"[0-9a-f]{1,16}", h):
                    return self._send(400, {"error": {"code": "invalid"}})
                with LOCK:
                    d = store.load()
                    d["pinHash"] = h
                    store.save(d)
                return self._send(200, {"ok": True})
        except store.StorageError:
            return self._busy()
        self._send(404, {"error": {"code": "notfound"}})

    def do_PUT(self):
        """Admin only: edit a booking's title, room, date or time. Owner (who/uid) and created time are kept."""
        if not self._authorized():
            return
        m = re.fullmatch(r"/api/bookings/([\w-]+)", unquote(urlparse(self.path).path))
        if not m:
            return self._send(404, {"error": {"code": "notfound"}})
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, {"error": {"code": "invalid"}})
        try:
            with LOCK:                                          # check + write is one atomic step
                d = store.load()
                old = next((b for b in d["bookings"] if b.get("id") == m.group(1)), None)
                if old is None:
                    return self._send(404, {"error": {"code": "notfound"}})
                expected = d.get("pinHash") or hash_pin(DEFAULT_PIN)
                if not hmac.compare_digest(str(body.get("pinHash", "")).encode(), str(expected).encode()):
                    return self._send(403, {"error": {"code": "forbidden"}})   # editing is admin-only
                merged = dict(body)
                merged["who"] = old.get("who", "")              # people cannot change the owner or block flag
                merged["uid"] = old.get("uid", "")
                merged["blocked"] = old.get("blocked", False)
                rec, err = clean(merged, existing=old)
                if err:
                    return self._send(400, {"error": {"code": err}})
                for b in d["bookings"]:
                    if b.get("id") != old.get("id") and b["room"] == rec["room"] and b["date"] == rec["date"] \
                            and rec["start"] < b["end"] and b["start"] < rec["end"]:
                        return self._send(409, {"error": {"code": "clash", "c": b}})
                for k in ("room", "date", "start", "end", "title"):
                    old[k] = rec[k]
                old["updatedAt"] = rec["createdAt"]
                store.save(d)
            return self._send(200, {"id": old["id"]})
        except store.StorageError:
            return self._busy()

    def do_DELETE(self):
        if not self._authorized():
            return
        m = re.fullmatch(r"/api/bookings/([\w-]+)", unquote(urlparse(self.path).path))
        if not m:
            return self._send(404, {"error": {"code": "notfound"}})
        try:
            with LOCK:
                d = store.load()
                d["bookings"] = [b for b in d["bookings"] if b.get("id") != m.group(1)]
                store.save(d)
        except store.StorageError:
            return self._busy()
        self._send(200, {"ok": True})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))                        # no traffic is sent
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    cloud = bool(os.environ.get("PORT"))                        # Render sets PORT
    port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8000))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(" ATMS Conference Booking is running")
    if cloud:
        print(f" Listening on port {port}")
    else:
        print(f" Share this address with the team:  http://{lan_ip()}:{port}")
        print(f" On this PC:                        http://localhost:{port}")
    print(f" Storage: {store.backend()}   Time zone: {getattr(TZ, 'key', 'UTC+05:30')}")
    print(f" Site password: {'ON' if ACCESS_PASSWORD else 'off (anyone with the address can use it)'}")
    print(" Keep this window open. Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")