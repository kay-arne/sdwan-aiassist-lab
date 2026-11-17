#!/usr/bin/env python3
"""Kleine 'datacenter'-server: bedrijfs-apps (HTTP), health en een bulk-download voor throughput."""
import json
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = socket.gethostname()
APPS = {"erp": "ERP", "crm": "CRM", "files": "Bestandsserver", "voip": "VoIP-portaal", "intranet": "Intranet"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        if path in ("health", "healthz"):
            return self._send(json.dumps({"host": HOST, "time": time.time(), "ok": True}).encode(), "application/json")
        if path.startswith("bulk"):      # /bulk/<MB>: bulk-download voor throughput-tests over de overlay
            mb = min(int(path.split("/")[1]) if "/" in path else 10, 500)
            self.send_response(200)
            self.send_header("Content-Length", str(mb * 1024 * 1024))
            self.end_headers()
            chunk = b"x" * 65536
            for _ in range(mb * 16):
                self.wfile.write(chunk)
            return
        app = path.split("/")[0] or "intranet"
        title = APPS.get(app, app)
        self._send(f"<h1>{title}</h1><p>Server {HOST} (datacenter-applicatie)</p>".encode())


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 80), H).serve_forever()
