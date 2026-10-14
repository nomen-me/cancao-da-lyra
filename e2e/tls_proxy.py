import http.server, ssl, http.client, sys, threading, socketserver
LISTEN, TARGET = int(sys.argv[1]), int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _do(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else None
        c = http.client.HTTPConnection("127.0.0.1", TARGET, timeout=120)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
        c.request(self.command, self.path, body, hdrs); r = c.getresponse(); data = r.read()
        if '/api/method/synapse_core' in self.path:
            safe = {k: (v[:12] + '...' if k.lower() == 'authorization' else v) for k, v in hdrs.items()}
            open('/tmp/proxy_hdrs.log', 'a').write(f"{self.command} {self.path} -> {r.status} | {safe} | body={(body or b'')[:80]!r}\n")
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"): self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    do_GET = do_POST = do_PUT = do_DELETE = _do
    def log_message(self, *a): pass
class S(socketserver.ThreadingMixIn, http.server.HTTPServer): daemon_threads = True
s = S(("127.0.0.1", LISTEN), H)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain("cert.pem", "key.pem")
s.socket = ctx.wrap_socket(s.socket, server_side=True); s.serve_forever()
