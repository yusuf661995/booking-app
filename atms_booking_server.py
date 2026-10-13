#!/usr/bin/env python3

import json
import os
import re
import socket
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(HERE, "atms_conference_booking.html")
DATA_FILE = os.path.join(HERE, "bookings.json")

OPEN_MIN, CLOSE_MIN, STEP = 9 * 60, 19 * 60, 30     # 9:00 AM - 7:00 PM, 30-min grid
AHEAD_DAYS = 31
ROOMS = ("conf", "lounge")
LOCK = threading.Lock()


def load():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("bookings", [])
        d.setdefault("pinHash", None)
        return d
    except FileNotFoundError:
        return {"bookings": [], "pinHash": None}


def save(d):
    fd, tmp = tempfile.mkstemp(dir=HERE, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)            # atomic: never leaves a half-written file


def clean(rec):
    """Validate a booking coming from a browser. Returns (record, error_code)."""
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
    now = datetime.now()
    if day.weekday() == 6 or day.date() > (now + timedelta(days=AHEAD_DAYS)).date():
        return None, "invalid"                                  # Sundays / too far ahead
    if (start % STEP or end % STEP or start < OPEN_MIN or end > CLOSE_MIN
            or end <= start or not title or not who):
        return None, "invalid"
    if day.date() < now.date() or (day.date() == now.date() and start < now.hour * 60 + now.minute):
        return None, "past"
    return {
        "room": room, "date": date, "start": start, "end": end,
        "who": who, "title": title,
        "uid": str(rec.get("uid", ""))[:40],
        "blocked": bool(rec.get("blocked", False)),
        "createdAt": datetime.now().isoformat(timespec="seconds"),
    }, None


class Handler(BaseHTTPRequestHandler):
    server_version = "ATMSBooking/1.0"

    def log_message(self, fmt, *args):                          # quieter console
        sys.stderr.write("%s  %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20000:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(HTML_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, b"atms_conference_booking.html not found next to the server script.", "text/plain")
        elif path == "/api/state":
            with LOCK:
                self._send(200, load())
        else:
            self._send(404, {"error": {"code": "notfound"}})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._json_body()
        except Exception:
            return self._send(400, {"error": {"code": "invalid"}})
        if path == "/api/bookings":
            rec, err = clean(body)
            if err:
                return self._send(400, {"error": {"code": err}})
            with LOCK:                                          # check + write is one atomic step
                d = load()
                for b in d["bookings"]:
                    if b["room"] == rec["room"] and b["date"] == rec["date"] \
                            and rec["start"] < b["end"] and b["start"] < rec["end"]:
                        return self._send(409, {"error": {"code": "clash", "c": b}})
                rec["id"] = uuid.uuid4().hex[:12]
                d["bookings"].append(rec)
                save(d)
            return self._send(201, {"id": rec["id"]})
        if path == "/api/pin":
            h = str(body.get("pinHash", ""))[:32]
            if not re.fullmatch(r"[0-9a-f]{1,16}", h):
                return self._send(400, {"error": {"code": "invalid"}})
            with LOCK:
                d = load()
                d["pinHash"] = h
                save(d)
            return self._send(200, {"ok": True})
        self._send(404, {"error": {"code": "notfound"}})
    def do_DELETE(self):
        m = re.fullmatch(r"/api/bookings/([\w-]+)", unquote(urlparse(self.path).path))
        if not m:
            return self._send(404, {"error": {"code": "notfound"}})
        with LOCK:
            d = load()
            d["bookings"] = [b for b in d["bookings"] if b.get("id") != m.group(1)]
            save(d)
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("=" * 60)
    print(" ATMS Conference Booking is running")
    print(f" Share this address with the team:  http://{lan_ip()}:{port}")
    print(f" On this PC:                        http://localhost:{port}")
    print(f" Bookings file: {DATA_FILE}")
    print(" Keep this window open. Press Ctrl+C to stop.")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
