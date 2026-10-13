"""Testes A-J no lado do Ritmo. Frappe v15.121.1 + ERPNext v15.121.4 + MariaDB REAIS. Sem mock.

O papel da Harmonia (ACK/PROCESSED/retry) e exercido chamando as MESMAS funcoes que a API
whitelisted expoe (helper drain()). Isso valida a autoridade (Ritmo); o fluxo Harmonia->Ritmo
real e coberto pelo roteiro de VPS (tests/vps/).  Falhas sao injetadas no banco real
(DELETE de linha, UPDATE de payload/processed_at) - injecao de falha, nao substituicao de componente.
"""
import json
import threading
import time
import unittest

import frappe
from frappe.utils import add_to_date, now_datetime

from synapse_core import aggregation, autonomia, estado, execution, lineage, outbox
from synapse_core.config import DT, cfg, set_cfg

OB = "tabSynapse Outbox"
CUST, ITEM = "CLI-CANCAO", "ITEM-CANCAO"


def reset():
    frappe.db.rollback()
    for t in ("Synapse Outbox", "Synapse Estado", "Synapse Operacao"):
        frappe.db.sql(f"DELETE FROM `tab{t}`")
    frappe.db.sql("DELETE FROM `tabSales Order Item` WHERE parent IN (SELECT name FROM `tabSales Order` WHERE customer=%s)", CUST)
    frappe.db.sql("DELETE FROM `tabSales Order` WHERE customer=%s", CUST)
    frappe.db.sql("DELETE FROM `tabItem Price` WHERE item_code LIKE 'D-%%'")
    frappe.db.sql("DELETE FROM `tabItem` WHERE name LIKE 'D-%%'")
    frappe.db.sql("DELETE FROM `tabSingles` WHERE doctype=%s", DT)
    frappe.db.value_cache.pop(DT, None)
    frappe.local.synapse_monitored = None
    frappe.db.commit()


def fixtures():
    frappe.set_user("Administrator")
    if not frappe.db.exists("Customer", CUST):
        frappe.get_doc({"doctype": "Customer", "customer_name": CUST, "customer_group": "Commercial",
                        "territory": "Rest Of The World"}).insert()
    if not frappe.db.exists("Item", ITEM):
        frappe.get_doc({"doctype": "Item", "item_code": ITEM, "item_name": ITEM, "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                        "stock_uom": "Nos", "is_stock_item": 0}).insert()
    frappe.db.commit()


def drain(upto=None):
    """Papel da Harmonia: ACK + PROCESSED de tudo que esta pendente, em ordem de seq."""
    rows = frappe.db.sql(f"SELECT name, status FROM `{OB}` WHERE status IN ('PENDING','DELIVERED','ACK') ORDER BY seq", as_dict=True)
    ids = []
    for r in rows:
        if r["status"] != "ACK":
            outbox.ack(r["name"])
        ids.append(r["name"])
    outbox.mark_processed(ids)
    frappe.db.commit()


def op(oid, action, args, basis, **kw):
    d = {"operation_id": oid, "action": action, "args": args, "basis": basis, "origin_actor": "LYRA"}
    d.update(kw)
    return d


def venda_args(qty=1):
    return {"customer": CUST, "company": frappe.db.get_single_value("Global Defaults", "default_company") or "Wind Power LLC",
            "itens": [{"produto_id": ITEM, "quantidade": qty, "preco_unitario": 10}]}


def item_basis():
    return [{"entity_type": "Item", "entity_id": ITEM, "decision_state_version": estado.current("Item", ITEM)}]


def touch_item():
    it = frappe.get_doc("Item", ITEM)
    it.description = f"alterado {time.time()}"
    it.save()
    frappe.db.commit()


def so_count(op_id):
    return frappe.db.sql("SELECT COUNT(*) FROM `tabSales Order` WHERE synapse_op_id=%s", op_id)[0][0]


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        fixtures()

    def setUp(self):
        reset()
        frappe.set_user("Administrator")


class TestA_Reconciliacao(Base):
    def test_A(self):
        # 3 eventos reais gerados por alteracoes reais no Ritmo
        for _ in range(3):
            touch_item()
        seqs = [r[0] for r in frappe.db.sql(f"SELECT seq FROM `{OB}` WHERE entity_type='Item' ORDER BY seq")]
        self.assertGreaterEqual(len(seqs), 3)
        # "ponte interrompida": o evento do meio nunca chega/ e perdido (injecao de falha no banco real)
        lost = seqs[1]
        frappe.db.sql(f"DELETE FROM `{OB}` WHERE seq=%s", lost)
        frappe.db.commit()
        # Harmonia so recebe/ACK dos demais
        for s in seqs:
            if s != lost:
                outbox.ack(frappe.db.sql(f"SELECT name FROM `{OB}` WHERE seq=%s", s)[0][0])
        frappe.db.commit()
        st = autonomia.get_state()
        self.assertEqual(st["estado_operacional"], "RECONCILING", st)          # gap detectado
        self.assertEqual((st["gap_from"], st["gap_to"]), (lost, lost))
        hb = autonomia.heartbeat()
        self.assertEqual(hb["trusted_until_seq"], lost - 1)                     # ate onde a visao e confiavel
        # leitura informativa continua, marcada como possivelmente desatualizada
        from synapse_core import api
        info = api.entity_state("Item", ITEM)
        self.assertTrue(info["possibly_stale"]); self.assertEqual(info["operational_state"], "RECONCILING")
        self.assertTrue(info["exists"])
        # mutacao critica entra em HOLD (nao executa)
        r = execution.execute(op("A-op-1", "criar_venda", venda_args(), item_basis()))
        frappe.db.commit()
        self.assertEqual(r["status"], "hold"); self.assertEqual(so_count("A-op-1"), 0)
        # nao ha CONSISTENT_STATE enquanto a lacuna nao foi reconciliada
        self.assertFalse(autonomia.declare_consistent()["declared"])
        self.assertEqual(autonomia.get_state()["estado_operacional"], "RECONCILING")
        # reconciliar: reconstrucao pela autoridade + processamento
        rep = autonomia.reconcile(); frappe.db.commit()
        self.assertEqual(rep["missing"], [lost, lost]); self.assertGreaterEqual(rep["resync_entities"], 1)
        drain()
        res = autonomia.declare_consistent(); frappe.db.commit()
        self.assertTrue(res["declared"], res)
        self.assertEqual(autonomia.get_state()["estado_operacional"], "HEALTHY")
        # HOLD revalidado e executado SO agora
        self.assertEqual(res["holds"][0]["status"], "executed", res)
        self.assertEqual(so_count("A-op-1"), 1)
        states = [r[0] for r in frappe.db.sql(f"SELECT payload FROM `{OB}` WHERE event_type='synapse.state_changed' ORDER BY seq")]
        self.assertIn("CONSISTENT_STATE", " ".join(states))


class TestB_HoldExpirado(Base):
    def test_B(self):
        autonomia.enter_reconciling("teste B"); frappe.db.commit()
        r = execution.execute(op("B-op-1", "criar_venda", venda_args(), item_basis(), intent_ttl=2))
        frappe.db.commit()
        self.assertEqual(r["status"], "hold")
        row = frappe.db.sql("SELECT hold_created_at, intent_ttl, intent_expires_at, decision_state_version FROM `tabSynapse Operacao` WHERE name='B-op-1'", as_dict=True)[0]
        for k in ("hold_created_at", "intent_ttl", "intent_expires_at", "decision_state_version"):
            self.assertIsNotNone(row[k], k)
        time.sleep(3)                                # ultrapassa o TTL (tempo real)
        drain()
        res = autonomia.declare_consistent(); frappe.db.commit()
        self.assertTrue(res["declared"], res)
        self.assertEqual(res["holds"][0]["status"], "intent_expired")
        self.assertEqual(so_count("B-op-1"), 0)      # NAO executou, mesmo com estado/versao validos
        self.assertEqual(estado.current("Item", ITEM), row["decision_state_version"])  # versao ainda valida!
        ev = frappe.db.sql(f"SELECT event_type FROM `{OB}` WHERE origin_operation_id='B-op-1' AND event_type='intent_expired'")
        self.assertEqual(len(ev), 1)                 # gera INTENT_EXPIRED para a Lyra


class TestC_Concurrency(Base):
    def test_C(self):
        touch_item()
        v = estado.current("Item", ITEM)
        touch_item()                                  # estado muda antes da execucao
        self.assertEqual(estado.current("Item", ITEM), v + 1)
        r = execution.execute(op("C-op-1", "criar_venda", venda_args(), [{"entity_type": "Item", "entity_id": ITEM, "decision_state_version": v}]))
        frappe.db.commit()
        self.assertEqual(r["status"], "concurrency_conflict")
        self.assertEqual(so_count("C-op-1"), 0)
        ctx = r["current_context"][0]
        self.assertEqual((ctx["expected_state_version"], ctx["current_state_version"]), (v, v + 1))   # contexto atualizado devolvido
        self.assertEqual(len(frappe.db.sql(f"SELECT 1 FROM `{OB}` WHERE event_type='concurrency_conflict' AND origin_operation_id='C-op-1'")), 1)
        # recalcula: nova decisao (novo operation_id) sobre a versao atual
        r2 = execution.execute(op("C-op-2", "criar_venda", venda_args(), [{"entity_type": "Item", "entity_id": ITEM, "decision_state_version": ctx["current_state_version"]}]))
        frappe.db.commit()
        self.assertEqual(r2["status"], "executed"); self.assertEqual(so_count("C-op-2"), 1)

    def test_C_sem_versao_e_rejeitada(self):
        with self.assertRaises(frappe.ValidationError):
            execution.execute({"operation_id": "C-op-x", "action": "criar_venda", "args": venda_args(), "basis": [{"entity_type": "Item", "entity_id": ITEM}]})


class TestD_OutboxTransacional(Base):
    def test_D_commit_junto(self):
        it = frappe.get_doc({"doctype": "Item", "item_code": "D-OK", "item_name": "D", "item_group": frappe.db.get_value("Item Group", {"is_group": 0}), "stock_uom": "Nos"}).insert()
        n_in_txn = frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}` WHERE entity_id='D-OK'")[0][0]
        self.assertEqual(n_in_txn, 1)                          # evento gravado ANTES do commit, na mesma transacao
        # enxergado por outra conexao? Nao: so depois do commit (prova de transacao unica)
        other = frappe.db.get_db_table_columns  # noqa
        import pymysql
        c = pymysql.connect(host="127.0.0.1", user=frappe.conf.db_name, password=frappe.conf.db_password, database=frappe.conf.db_name)
        cur = c.cursor()
        cur.execute("SELECT (SELECT COUNT(*) FROM `tabItem` WHERE name='D-OK'), (SELECT COUNT(*) FROM `tabSynapse Outbox` WHERE entity_id='D-OK')")
        self.assertEqual(cur.fetchone(), (0, 0))               # nenhum dos dois visivel pre-commit
        frappe.db.commit()
        c.commit(); cur.execute("SELECT (SELECT COUNT(*) FROM `tabItem` WHERE name='D-OK'), (SELECT COUNT(*) FROM `tabSynapse Outbox` WHERE entity_id='D-OK')")
        self.assertEqual(cur.fetchone(), (1, 1))               # ambos visiveis apos o MESMO commit
        c.close()

    def test_D_falha_desfaz_ambos(self):
        frappe.get_doc({"doctype": "Item", "item_code": "D-FAIL", "item_name": "D", "item_group": frappe.db.get_value("Item Group", {"is_group": 0}), "stock_uom": "Nos"}).insert()
        self.assertEqual(frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}` WHERE entity_id='D-FAIL'")[0][0], 1)
        # falha real (violacao de unicidade do proprio Frappe) no mesmo request -> rollback
        with self.assertRaises(frappe.DuplicateEntryError):
            frappe.get_doc({"doctype": "Item", "item_code": "D-FAIL", "item_name": "D", "item_group": frappe.db.get_value("Item Group", {"is_group": 0}), "stock_uom": "Nos"}).insert()
        frappe.db.rollback()
        self.assertFalse(frappe.db.exists("Item", "D-FAIL"))
        self.assertEqual(frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}` WHERE entity_id='D-FAIL'")[0][0], 0)   # sem negocio => sem evento
        self.assertEqual(estado.current("Item", "D-FAIL"), 0)   # versao tambem desfeita

    def test_D_seq_sem_buracos_apos_rollback(self):
        touch_item()
        h1 = autonomia.stream_head()
        frappe.get_doc({"doctype": "Item", "item_code": "D-RB", "item_name": "D", "item_group": frappe.db.get_value("Item Group", {"is_group": 0}), "stock_uom": "Nos"}).insert()
        frappe.db.rollback()
        touch_item()
        seqs = [r[0] for r in frappe.db.sql(f"SELECT seq FROM `{OB}` ORDER BY seq")]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))   # contiguo, sem lacuna artificial


class TestE_Poison(Base):
    def test_E(self):
        touch_item(); touch_item()
        ids = [r[0] for r in frappe.db.sql(f"SELECT name FROM `{OB}` ORDER BY seq")]
        poison, others = ids[0], ids[1:]
        frappe.db.sql(f"UPDATE `{OB}` SET payload='{{corrompido' WHERE name=%s", poison); frappe.db.commit()
        env = outbox.envelope(frappe.db.sql(f"SELECT * FROM `{OB}` WHERE name=%s", poison, as_dict=True)[0])
        self.assertTrue(env["payload"].get("__unparseable__"))
        r1 = outbox.record_failure(poison, "payload invalido", "1"); r2 = outbox.record_failure(poison, "payload invalido", "1")
        self.assertEqual((r1["retry_count"], r1["dead_lettered"], r2["dead_lettered"]), (1, False, False))
        self.assertEqual(autonomia.get_state()["estado_operacional"], "HEALTHY")
        r3 = outbox.record_failure(poison, "payload invalido", "1"); frappe.db.commit()
        self.assertTrue(r3["dead_lettered"]); self.assertEqual(r3["retry_count"], 3)
        row = frappe.db.sql(f"SELECT * FROM `{OB}` WHERE name=%s", poison, as_dict=True)[0]
        self.assertEqual(row["status"], "DEAD_LETTER")
        for k in ("retry_count", "first_failure_at", "last_failure_at", "failure_reason", "schema_version", "dead_lettered_at"):
            self.assertTrue(row[k] not in (None, ""), k)
        self.assertIsNotNone(row["payload"])                                      # preservado para auditoria
        self.assertEqual(autonomia.get_state()["estado_operacional"], "RECONCILING")   # DLQ != HEALTHY
        # demais eventos continuam fluindo
        for i in others:
            outbox.ack(i)
        outbox.mark_processed(others); frappe.db.commit()
        self.assertEqual(frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}` WHERE name IN %s AND status='PROCESSED'", (tuple(others),))[0][0], len(others))
        # sem reconciliar nao ha consistencia
        self.assertFalse(autonomia.declare_consistent()["declared"])
        rep = autonomia.reconcile(); frappe.db.commit()
        self.assertEqual(rep["dlq_validated"], [poison])
        drain()
        res = autonomia.declare_consistent(); frappe.db.commit()
        self.assertTrue(res["declared"], res)

    def test_E_impacto_indeterminado_mantem_bloqueio(self):
        e = outbox.emit("", "", "evento.sem_entidade", {}, origin_actor="SYSTEM"); frappe.db.commit()
        for _ in range(3):
            outbox.record_failure(e["event_id"], "sem entidade")
        frappe.db.commit()
        rep = autonomia.reconcile(); frappe.db.commit()
        self.assertEqual(rep["dlq_blocking"], [e["event_id"]])
        drain()
        self.assertFalse(autonomia.declare_consistent()["declared"])
        r = execution.execute(op("E-op-1", "criar_venda", venda_args(), item_basis()))
        self.assertEqual(r["status"], "hold")                                       # mutacao segue bloqueada


class TestF_Purge(Base):
    def test_F(self):
        for _ in range(6):
            touch_item()
        drain()
        rows = frappe.db.sql(f"SELECT name, seq FROM `{OB}` ORDER BY seq", as_dict=True)
        old, recent = rows[:4], rows[4:5]
        pend = rows[5:]
        old_ts = add_to_date(now_datetime(), days=-30)
        for r in old:
            frappe.db.sql(f"UPDATE `{OB}` SET processed_at=%s WHERE name=%s", (old_ts, r["name"]))
        # elegibilidade negativa: ACK antigo (nao PROCESSED) e DEAD_LETTER antigo
        e1 = outbox.emit("Item", ITEM, "x.ack_old", {}, origin_actor="SYSTEM")["event_id"]
        e2 = outbox.emit("Item", ITEM, "x.dlq_old", {}, origin_actor="SYSTEM")["event_id"]
        outbox.ack(e1)
        for _ in range(3):
            outbox.record_failure(e2, "x")
        frappe.db.sql(f"UPDATE `{OB}` SET ack_at=%s, dead_lettered_at=%s WHERE name IN (%s,%s)", (old_ts, old_ts, e1, e2))
        frappe.db.commit()
        before = frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}`")[0][0]
        res = outbox.purge(retention_days=7, batch_size=2)
        self.assertEqual(res["deleted"], 4); self.assertGreaterEqual(res["batches"], 2)       # em lotes
        left = {r[0] for r in frappe.db.sql(f"SELECT name FROM `{OB}`")}
        for r in old:
            self.assertNotIn(r["name"], left)
        for x in [recent[0]["name"], e1, e2] + [p["name"] for p in pend]:
            self.assertIn(x, left)                                                             # necessarios p/ replay/auditoria
        self.assertEqual(frappe.db.sql(f"SELECT COUNT(*) FROM `{OB}`")[0][0], before - 4)

    def test_F_config_retencao_e_nao_hardcoded(self):
        self.assertEqual(cfg("outbox_retention_days"), 7)
        set_cfg("outbox_retention_days", 30); self.assertEqual(cfg("outbox_retention_days"), 30)


class TestG_HarpaObsoleta(Base):
    def _venda_confirmada(self, oid):
        r = execution.execute(op(oid, "criar_venda", venda_args(), item_basis())); frappe.db.commit()
        name = r["result"]["name"]
        r = execution.execute(op(oid + "-conf", "confirmar_venda", {"id_pedido": name},
                                 [{"entity_type": "Sales Order", "entity_id": name, "decision_state_version": estado.current("Sales Order", name)}]))
        frappe.db.commit()
        self.assertEqual(r["status"], "executed", r)
        return name

    def test_G_contexto_alterado(self):
        name = self._venda_confirmada("G1")
        v = estado.current("Sales Order", name)
        r = execution.execute(op("G-cancel", "cancelar_venda", {"id_pedido": name, "motivo": "cliente desistiu"},
                                 [{"entity_type": "Sales Order", "entity_id": name, "decision_state_version": v}]))
        frappe.db.commit()
        self.assertEqual(r["status"], "awaiting_approval")
        self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)      # nada executado antes da aprovacao
        # estado muda depois do pedido de aprovacao
        so = frappe.get_doc("Sales Order", name); so.add_comment("Comment", "x"); so.db_set("per_delivered", 5, notify=True)
        so.run_method("on_change"); frappe.db.commit()
        self.assertGreater(estado.current("Sales Order", name), v)
        a = execution.approve("G-cancel"); frappe.db.commit()
        self.assertEqual(a["status"], "context_changed")                                # rejeitada por contexto alterado
        self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)      # NAO cancelou
        self.assertEqual(len(frappe.db.sql(f"SELECT 1 FROM `{OB}` WHERE event_type='context_changed' AND origin_operation_id='G-cancel'")), 1)
        self.assertIn("current_context", a)

    def test_G_controle_positivo_e_permissao(self):
        name = self._venda_confirmada("G2")
        v = estado.current("Sales Order", name)
        execution.execute(op("G-ok", "cancelar_venda", {"id_pedido": name, "motivo": "cliente desistiu"},
                             [{"entity_type": "Sales Order", "entity_id": name, "decision_state_version": v}]))
        frappe.db.commit()
        # usuario sem papel aprovador nao aprova
        if not frappe.db.exists("User", "sem.papel@synapse.test"):
            frappe.get_doc({"doctype": "User", "email": "sem.papel@synapse.test", "first_name": "Sem", "send_welcome_email": 0, "roles": [{"role": "Guest"}]}).insert()
            frappe.db.commit()
        frappe.set_user("sem.papel@synapse.test")
        with self.assertRaises(frappe.PermissionError):
            execution.approve("G-ok")
        frappe.set_user("Administrator")
        a = execution.approve("G-ok"); frappe.db.commit()
        self.assertEqual(a["status"], "executed", a)
        self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 2)
        again = execution.approve("G-ok")
        self.assertTrue(again.get("replayed"))                                          # duplo clique nao reexecuta

    def test_G_aprovacao_expirada(self):
        name = self._venda_confirmada("G3")
        v = estado.current("Sales Order", name)
        execution.execute(op("G-exp", "cancelar_venda", {"id_pedido": name, "motivo": "x"},
                             [{"entity_type": "Sales Order", "entity_id": name, "decision_state_version": v}], intent_ttl=1))
        frappe.db.commit(); time.sleep(2)
        a = execution.approve("G-exp"); frappe.db.commit()
        self.assertEqual(a["status"], "intent_expired")
        self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)


class TestH_CausalDepth(Base):
    def test_H_funcao_pura(self):
        p = {"event_id": "e5", "root_event_id": "r", "chain_id": "c1", "causal_depth": 5}
        self.assertEqual(lineage.next_lineage(p, "e6", 5, {"event_count": 6})["chain_closed"], True)
        self.assertEqual(lineage.next_lineage({**p, "causal_depth": 4}, "e6", 5)["causal_depth"], 5)

    def test_H(self):
        set_cfg("causal_depth_limit", 3)
        parent, chains, sizes = None, [], []
        for i in range(9):
            r = outbox.emit("Item", ITEM, f"h.step{i}", {"i": i, "blob": "x" * 200}, origin_actor="LYRA", parent=parent)
            row = frappe.db.sql(f"SELECT event_id, root_event_id, chain_id, causal_depth, entity_type, entity_id FROM `{OB}` WHERE event_id=%s", r["event_id"], as_dict=True)[0]
            parent = row; chains.append(r)
            sizes.append(len(frappe.db.sql(f"SELECT COALESCE(causal_summary,'') FROM `{OB}` WHERE event_id=%s", r["event_id"])[0][0]))
        frappe.db.commit()
        depths = [c["causal_depth"] for c in chains]
        self.assertEqual(depths, [0, 1, 2, 3, 0, 1, 2, 3, 0])                    # limite 3 respeitado
        ids = [c["chain_id"] for c in chains]
        self.assertEqual(len(set(ids)), 3)                                        # 3 cadeias
        self.assertEqual(chains[4]["previous_chain_id"], ids[0])                  # referencia auditavel a anterior
        self.assertEqual(chains[8]["previous_chain_id"], ids[4])
        s = json.loads(frappe.db.sql(f"SELECT causal_summary FROM `{OB}` WHERE event_id=%s", chains[4]["event_id"])[0][0])
        self.assertEqual((s["closed_chain_id"], s["depth_reached"], s["event_count"]), (ids[0], 3, 4))
        self.assertNotIn("blob", json.dumps(s))                                   # nao transporta payload historico
        self.assertEqual(chains[4]["root_event_id"], chains[4]["event_id"])       # novo root
        # o resumo tem tamanho limitado e NAO cresce com o comprimento da cadeia
        self.assertGreater(sizes[4], 0); self.assertGreater(sizes[8], 0)
        self.assertLess(abs(sizes[8] - sizes[4]), 120)

    def test_H_eventos_da_lyra_geram_consequencias(self):
        r = outbox.emit("Item", ITEM, "lyra.acao", {}, origin_actor="LYRA")
        frappe.db.commit()
        row = frappe.db.sql(f"SELECT origin_actor, status FROM `{OB}` WHERE event_id=%s", r["event_id"], as_dict=True)[0]
        self.assertEqual((row["origin_actor"], row["status"]), ("LYRA", "PENDING"))   # nao suprimido


class TestI_Aggregation(Base):
    def _mk(self, n, key):
        for _ in range(n):
            e = outbox.emit("Item", ITEM, "i.evt", {}, origin_actor="HUMAN")["event_id"]
            outbox.ack(e)
            frappe.db.sql(f"UPDATE `{OB}` SET gate_action='AGGREGATE', agg_key=%s WHERE name=%s", (key, e))
        frappe.db.commit()

    def test_I_max_wait(self):
        set_cfg("agg_max_wait_seconds", 3); set_cfg("agg_max_events", 10)
        self._mk(3, "k-wait")
        self.assertEqual(aggregation.due_groups(), [])                 # ainda nao
        time.sleep(4)
        due = aggregation.due_groups()
        self.assertEqual([(d["agg_key"], d["count"], d["reason"]) for d in due], [("k-wait", 3, "MAX_WAIT")])
        fl = aggregation.flush("k-wait")
        self.assertEqual(len(fl["events"]), 3)
        for k in ("event_id", "root_event_id", "parent_event_id", "chain_id", "origin_actor", "origin_operation_id", "entity_type",
                  "entity_id", "event_type", "state_version", "event_timestamp", "payload", "schema_version"):
            self.assertIn(k, fl["events"][0])                          # MESMO envelope do Outbox, nao lista de ids
        self.assertEqual(aggregation.due_groups(), [])                 # em voo (lease): nao reflushar
        self.assertEqual({r[0] for r in frappe.db.sql(f"SELECT status FROM `{OB}` WHERE agg_key='k-wait'")}, {"ACK"})   # flush NAO e PROCESSED

    def test_I_max_events_imediato(self):
        set_cfg("agg_max_wait_seconds", 120); set_cfg("agg_max_events", 10)
        t0 = time.time(); self._mk(10, "k-max")
        due = aggregation.due_groups()
        self.assertLess(time.time() - t0, 60)
        self.assertEqual([(d["count"], d["reason"]) for d in due], [(10, "MAX_EVENTS")])   # 10 em segundos => flush imediato


class TestK_Heartbeat(Base):
    def test_agregacao_em_espera_nao_e_atraso(self):
        set_cfg("agg_max_wait_seconds", 120); set_cfg("heartbeat_max_lag_segundos", 5)
        e = outbox.emit("Item", ITEM, "item.updated", {}, origin_actor="HUMAN")["event_id"]
        outbox.ack(e)
        ago = lambda s: add_to_date(now_datetime(), seconds=-s)
        frappe.db.sql(f"UPDATE `{OB}` SET gate_action='AGGREGATE', agg_key='k', event_timestamp=%s, ack_at=%s WHERE name=%s", (ago(60), ago(60), e))
        frappe.db.commit()
        hb = autonomia.heartbeat()
        self.assertEqual(hb["problems"], [])                    # 60s < MAX_WAIT: espera legitima
        self.assertEqual(autonomia.get_state()["estado_operacional"], "HEALTHY")
        frappe.db.sql(f"UPDATE `{OB}` SET ack_at=%s WHERE name=%s", (ago(200), e)); frappe.db.commit()
        hb = autonomia.heartbeat()                               # 200s > MAX_WAIT(120)+tolerancia(5): flush parado
        self.assertEqual([p["type"] for p in hb["problems"]], ["lag"])
        self.assertEqual(autonomia.get_state()["estado_operacional"], "RECONCILING")

    def test_ponte_parada_e_detectada(self):
        set_cfg("heartbeat_max_lag_segundos", 2)
        outbox.emit("Item", ITEM, "x.y", {}, origin_actor="HUMAN"); frappe.db.commit()
        time.sleep(4)
        hb = autonomia.heartbeat(); frappe.db.commit()
        self.assertEqual(hb["state"], "RECONCILING"); self.assertEqual(hb["problems"][0]["type"], "lag")

    def test_divergencia_do_consumidor(self):
        hb = autonomia.heartbeat(processed_seq=999)
        self.assertIn("divergence", [p["type"] for p in hb["problems"]])


class TestJ_Idempotencia(Base):
    def test_J_sequencial(self):
        o = op("J-op-1", "criar_venda", venda_args(), item_basis())
        r1 = execution.execute(dict(o)); frappe.db.commit()
        r2 = execution.execute(dict(o)); frappe.db.commit()
        self.assertEqual(r1["status"], "executed"); self.assertTrue(r2.get("replayed"))
        self.assertEqual(r1["result"], r2["result"])
        self.assertEqual(so_count("J-op-1"), 1)
        self.assertEqual(frappe.db.sql("SELECT COUNT(*) FROM `tabSynapse Operacao` WHERE operation_id='J-op-1'")[0][0], 1)

    def test_J_reenvio_de_conflito_nao_reexecuta(self):
        v = estado.current("Item", ITEM); touch_item()
        o = op("J-op-2", "criar_venda", venda_args(), [{"entity_type": "Item", "entity_id": ITEM, "decision_state_version": v}])
        a = execution.execute(dict(o)); frappe.db.commit()
        b = execution.execute(dict(o)); frappe.db.commit()
        self.assertEqual((a["status"], b["status"]), ("concurrency_conflict", "concurrency_conflict"))
        self.assertEqual(so_count("J-op-2"), 0)

    def test_J_concorrente_duas_conexoes(self):
        o = op("J-op-3", "criar_venda", venda_args(), item_basis())
        out = []
        def run():
            frappe.init(site="synapse.test", sites_path="."); frappe.connect(); frappe.set_user("Administrator")
            try:
                r = execution.execute(dict(o)); frappe.db.commit(); out.append(r["status"] + (":replayed" if r.get("replayed") else ""))
            except Exception as e:
                frappe.db.rollback(); out.append("ERR " + type(e).__name__)
            finally:
                frappe.destroy()
        ts = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        frappe.connect() if not frappe.db else None
        frappe.db.commit()   # descarta o snapshot REPEATABLE READ antigo desta conexao
        self.assertEqual(sorted(out), ["executed"] + ["executed:replayed"] * 5, out)   # NENHUM erro para o chamador
        self.assertEqual(so_count("J-op-3"), 1)


if __name__ == "__main__":
    unittest.main()
