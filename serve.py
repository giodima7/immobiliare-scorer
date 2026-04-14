#!/usr/bin/env python3
# Run with: python3 serve.py
"""
Serve the dashboard folder on http://localhost:8000
Usage: python serve.py
"""
import http.server
import os
import socketserver

PORT = 8000
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} {fmt % args}")


if __name__ == "__main__":
    os.chdir(DASHBOARD_DIR)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"\n  Dashboard → http://localhost:{PORT}/")
        print(f"  Serving:    {DASHBOARD_DIR}")
        print(f"  Ctrl+C to stop\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")
