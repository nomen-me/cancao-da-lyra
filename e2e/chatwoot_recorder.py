import http.server, json, sys
LOG = "/home/claude/work/e2e/chatwoot_received.jsonl"
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); b = self.rfile.read(n).decode()
        open(LOG, "a").write(json.dumps({"path": self.path, "token": self.headers.get("api_access_token"), "body": json.loads(b or "{}")}) + "\n")
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 5681), H).serve_forever()
