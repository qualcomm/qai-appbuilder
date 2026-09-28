"""Minimal transparent HTTP forward proxy that logs full request bodies.

Listens on --listen_port, forwards every request byte-for-byte to
--target_host:--target_port, and appends the raw request body (plus a
few headers) to --log_file BEFORE forwarding. Uses only the Python
standard library (http.server + http.client) so it needs no extra deps
on the remote machine.

Read-only and transparent: it never rewrites request/response bodies
and only drops the Host / Transfer-Encoding / Connection headers that
would otherwise break re-forwarding, so it is safe to sit in front of a
real QAIModelBuilder <-> GenieAPIService link during a real capture
session (e.g. as an alternative way to inspect the raw wire-level
image_url.url / input_audio.data fields when the prompt-snapshot debug
endpoint used by test_qaimodelbuilder_remote.py is not available).
"""
from __future__ import annotations

import argparse
import http.client
import http.server
import socketserver
import threading
import time


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    target_host = "127.0.0.1"
    target_port = 8910
    log_file = "capture_proxy.log"
    log_lock = threading.Lock()

    def _log(self, method: str, path: str, headers, body: bytes) -> None:
        with self.log_lock:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 80 + "\n")
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {method} {path}\n")
                for k, v in headers.items():
                    f.write(f"  HDR {k}: {v}\n")
                f.write(f"  BODY_LEN: {len(body)}\n")
                try:
                    f.write("  BODY: " + body.decode("utf-8", errors="replace") + "\n")
                except Exception as exc:  # noqa: BLE001
                    f.write(f"  BODY_DECODE_ERROR: {exc}\n")

    def _proxy(self, method: str) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length > 0 else b""
        self._log(method, self.path, self.headers, body)
        try:
            conn = http.client.HTTPConnection(self.target_host, self.target_port, timeout=120)
            fwd_headers = {k: v for k, v in self.headers.items() if k.lower() != "host"}
            conn.request(method, self.path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
            resp_body = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)
            conn.close()
        except Exception as exc:  # noqa: BLE001
            with self.log_lock:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(f"  PROXY_ERROR: {exc}\n")
            self.send_response(502)
            self.end_headers()

    def do_GET(self) -> None:
        self._proxy("GET")

    def do_POST(self) -> None:
        self._proxy("POST")

    def do_PUT(self) -> None:
        self._proxy("PUT")

    def do_DELETE(self) -> None:
        self._proxy("DELETE")

    def log_message(self, format, *args):  # noqa: A002 - silence default stderr logging
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only transparent HTTP forward proxy that logs full request "
                     "bodies before forwarding them unmodified.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--listen_port", type=int, default=8912, help="Local port to listen on")
    parser.add_argument("--target_host", default="127.0.0.1", help="Upstream host to forward requests to")
    parser.add_argument("--target_port", type=int, default=8910, help="Upstream port to forward requests to")
    parser.add_argument("--log_file", default="capture_proxy.log",
                         help="Path to append captured requests to (relative to the current "
                              "working directory unless an absolute path is given)")
    args = parser.parse_args()

    ProxyHandler.target_host = args.target_host
    ProxyHandler.target_port = args.target_port
    ProxyHandler.log_file = args.log_file

    server = ThreadingHTTPServer(("0.0.0.0", args.listen_port), ProxyHandler)
    print(f"proxy listening on 0.0.0.0:{args.listen_port} -> {args.target_host}:{args.target_port}", flush=True)
    print(f"logging captured requests to {args.log_file}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
