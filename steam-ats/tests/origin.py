"""Two HTTP origins and an HTTPS origin with deterministic Steam-like chunks."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit

BODY = bytes(range(256)) * 1024
COUNTS = Counter()
REQUESTS = []
COMPLETED = set()
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_HEAD(self):
        self.handle_request(head=True)

    def do_GET(self):
        self.handle_request()

    def log_message(self, *_args):
        pass

    def send(self, status, body=b"", headers=None, head=False):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def handle_request(self, head=False):
        if self.path == "/__stats":
            with LOCK:
                body = json.dumps({"counts": dict(COUNTS), "requests": REQUESTS, "completed": sorted(COMPLETED)}).encode()
            return self.send(200, body, head=head)
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        case = query.get("case", ["direct"])[0]
        with LOCK:
            COUNTS[self.path] += 1
            occurrence = COUNTS[self.path]
            REQUESTS.append({
                "path": self.path, "method": self.command,
                "host": self.headers.get("Host"), "range": self.headers.get("Range"),
                "authorization": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"), "port": self.server.server_port,
            })
        if self.server.server_port == 80 and self.headers.get("Host") not in (
            "cache-a.steamcontent.com", "cache-a.steamcontent.com:80",
            "cache-b.steamcontent.com", "cache-b.steamcontent.com:80",
        ):
            return self.send(421, b"wrong client Host", head=head)
        if self.server.server_port == 8001 and self.headers.get("Host") not in (
            "origin2:8001", "cache-a.steamcontent.com:8001",
        ):
            return self.send(421, b"wrong redirect Host", head=head)
        if self.server.server_port == 8443 and self.headers.get("Host") != "origin2:8443":
            return self.send(421, b"wrong TLS Host", head=head)

        if parsed.path.startswith("/depot/"):
            if case in ("query_redirect", "query_redirect_stale") and "step" not in query:
                return self.send(302, headers={"Location": f"?case={case}&step=done&token=a%2Fb"}, head=head)
            if case == "chain":
                return self.send(302, headers={"Location": "/redirect/start?token=a%2Fb"}, head=head)
            if case == "https":
                return self.send(307, headers={"Location": "https://origin2:8443/final?case=https"}, head=head)
            if case == "badtls":
                return self.send(302, headers={"Location": "https://origin:8443/final?case=badtls"}, head=head)
            if case == "cross":
                return self.send(303, headers={"Location": "//origin2:8001/final?case=cross"}, head=head)
            if case == "loop":
                return self.send(302, headers={"Location": self.path}, head=head)
            if case == "loop_case":
                return self.send(302, headers={"Location": "http://CACHE-A.STEAMCONTENT.COM:80" + self.path}, head=head)
            if case == "relative":
                return self.send(302, headers={"Location": "/relative/dir/start"}, head=head)
            if case == "long":
                return self.send(302, headers={"Location": "/long/0"}, head=head)
            if case == "missing":
                return self.send(302, head=head)
            if case == "invalid":
                return self.send(302, headers={"Location": "file:///etc/passwd"}, head=head)
            if case == "error":
                return self.send(503, b"temporary upstream error", head=head)
            if case == "collapse":
                time.sleep(0.5)
            if case in ("rww", "abandon"):
                body = BODY * 32
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                for start in range(0, len(body), 128 * 1024):
                    self.wfile.write(body[start:start + 128 * 1024])
                    self.wfile.flush()
                    time.sleep(0.04)
                with LOCK:
                    COMPLETED.add(self.path)
                return
            if case == "truncated" and occurrence == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY[:4096])
                self.wfile.flush()
                self.close_connection = True
                return
        if self.path == "/redirect/start?token=a%2Fb":
            return self.send(301, headers={"Location": "/next?case=chain"}, head=head)
        if self.path == "/relative/dir/start":
            return self.send(307, headers={"Location": "../final?token=a%2Fb"}, head=head)
        if self.path == "/relative/final?token=a%2Fb":
            return self.send(308, headers={"Location": "?token=a%2Fb&ready=1"}, head=head)
        if self.path == "/relative/final?token=a%2Fb&ready=1":
            return self.send(302, headers={"Location": "//origin2:8001/final?case=relative"}, head=head)
        if self.path.startswith("/long/"):
            number = int(self.path.rsplit("/", 1)[1])
            return self.send(302, headers={"Location": f"/long/{number + 1}"}, head=head)
        if self.path == "/next?case=chain":
            return self.send(308, headers={"Location": "http://origin2:8001/final?token=a%2Fb"}, head=head)
        if self.path == "/next?case=chain&ready=1":
            return self.send(307, headers={"Location": "http://origin2:8001/final?token=a%2Fb"}, head=head)

        body = BODY
        if parsed.path.startswith("/depot/") and (
            self.headers.get("Host", "").split(":", 1)[0] == "cache-b.steamcontent.com"
            or self.server.server_port == 8001
        ):
            body = BODY[::-1]
        headers = {"Content-Type": "application/octet-stream", "ETag": '"chunk-v1"',
                   "Cache-Control": "public, max-age=3600", "Accept-Ranges": "bytes"}
        if case == "query_redirect_stale" and occurrence == 1:
            headers["Cache-Control"] = "public, max-age=0"
        if case == "private":
            headers["Cache-Control"] = "private, no-store"
        if case == "cookie":
            headers["Set-Cookie"] = "session=test"
        if case == "partial":
            headers["Content-Range"] = f"bytes 0-15/{len(body)}"
            return self.send(206, body[:16], headers, head=head)
        requested_range = self.headers.get("Range")
        if requested_range:
            start, end = requested_range.removeprefix("bytes=").split("-")
            start, end = int(start), min(int(end), len(body) - 1)
            headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
            return self.send(206, body[start:end + 1], headers, head=head)
        return self.send(200, body, headers, head=head)


if __name__ == "__main__":
    for port in (80, 8001, 8443):
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        if port == 8443:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain("/certs/origin.crt", "/certs/origin.key")
            server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()
