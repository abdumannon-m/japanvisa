import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

from check_slots import cycle


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = os.environ.get("CRON_SECRET")
        auth = self.headers.get("authorization") or self.headers.get("Authorization")
        if secret and auth != f"Bearer {secret}":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        try:
            new = cycle()
            payload, code = json.dumps({"ok": True, "new": sorted(new)}), 200
        except Exception as e:
            payload, code = json.dumps({"ok": False, "error": str(e)}), 500
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload.encode())
