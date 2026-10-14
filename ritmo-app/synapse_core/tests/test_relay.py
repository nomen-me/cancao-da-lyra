"""Relay Outbox->HTTP. Receptor HTTP real (stand-in local da Harmonia): NAO substitui o teste na Harmonia real."""
import http.server, json, threading, time, unittest
import frappe
from synapse_core import outbox
from synapse_core.config import set_cfg
from synapse_core.tests.test_cancao import Base, touch_item, OB

RECEIVED, MODE = [], {"code": 200}


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        RECEIVED.append(body)
        self.send_response(MODE["code"]); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass


class TestRelay(Base):
    def test_fuso_nao_causa_reentrega_imediata(self):
        """Regressao: NOW() do MariaDB (UTC) vs now() do Frappe (fuso do site) reentregava a todo tick."""
        RECEIVED.clear(); MODE["code"] = 200
        set_cfg("harmonia_ingest_url", "http://127.0.0.1:18099/webhook/synapse-evento"); set_cfg("redelivery_after_seconds", 3600)
        touch_item()
        outbox.dispatch_pending(); frappe.db.commit()
        n = len(RECEIVED); self.assertEqual(n, 1)
        outbox.dispatch_pending(); outbox.dispatch_pending(); frappe.db.commit()
        self.assertEqual(len(RECEIVED), n)      # DELIVERED ha segundos, janela de 1h: NAO reentrega

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.srv = http.server.HTTPServer(("127.0.0.1", 18099), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_entrega_e_reentrega(self):
        RECEIVED.clear(); MODE["code"] = 500
        set_cfg("harmonia_ingest_url", "http://127.0.0.1:18099/webhook/synapse-evento"); set_cfg("redelivery_after_seconds", 1)
        touch_item(); touch_item()
        r = outbox.dispatch_pending()                      # ponte fora: 500
        self.assertEqual((r["sent"], r["failed"]), (0, 2))
        st = {x[0] for x in frappe.db.sql(f"SELECT status FROM `{OB}`")}
        self.assertEqual(st, {"PENDING"})                  # nada se perde: continua PENDING
        MODE["code"] = 200                                 # ponte volta
        r = outbox.dispatch_pending(); frappe.db.commit()
        self.assertEqual(r["sent"], 2)
        self.assertEqual([e["seq"] for e in RECEIVED[-2:]], sorted(e["seq"] for e in RECEIVED[-2:]))   # em ordem de seq
        env = RECEIVED[-1]
        for k in ("event_id", "root_event_id", "chain_id", "origin_actor", "entity_type", "entity_id", "event_type", "state_version", "event_timestamp", "causal_depth", "schema_version"):
            self.assertIn(k, env)
        self.assertEqual({x[0] for x in frappe.db.sql(f"SELECT status FROM `{OB}`")}, {"DELIVERED"})
        time.sleep(2)                                      # sem ACK: reentrega
        n0 = len(RECEIVED); outbox.dispatch_pending(); frappe.db.commit()
        self.assertEqual(len(RECEIVED) - n0, 2)
        # apos ACK/PROCESSED nao reentrega
        for e in RECEIVED[-2:]:
            outbox.ack(e["event_id"])
        outbox.mark_processed([e["event_id"] for e in RECEIVED[-2:]]); frappe.db.commit()
        time.sleep(2); n1 = len(RECEIVED); outbox.dispatch_pending()
        self.assertEqual(len(RECEIVED), n1)
