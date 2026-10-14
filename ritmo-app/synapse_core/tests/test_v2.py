"""Ordem Definitiva: reject autorizado, agregacao recuperavel, ordem por state_version, evento desconhecido,
eventos reais de Item / Item Price / Stock Ledger Entry. Frappe/ERPNext/MariaDB REAIS, sem mock."""
import time, unittest
import frappe
from frappe.utils import add_to_date, now_datetime

from synapse_core import aggregation, api, autonomia, estado, execution, outbox
from synapse_core.config import set_cfg
from synapse_core.tests.test_cancao import (Base, CUST, ITEM, OB, drain, item_basis, op, touch_item, venda_args)
from synapse_core.tests.test_cancao import TestG_HarpaObsoleta as _G


class TestReject(Base):
    def _pendente(self, oid):
        g = _G("test_G_contexto_alterado"); name = g._venda_confirmada(oid)
        v = estado.current("Sales Order", name)
        execution.execute(op(oid + "-c", "cancelar_venda", {"id_pedido": name, "motivo": "x"},
                             [{"entity_type": "Sales Order", "entity_id": name, "decision_state_version": v}]))
        frappe.db.commit()
        return name, oid + "-c"

    def test_reject_sem_permissao_recusada_operacao_intacta(self):
        name, opid = self._pendente("R1")
        if not frappe.db.exists("User", "sem.papel@synapse.test"):
            frappe.get_doc({"doctype": "User", "email": "sem.papel@synapse.test", "first_name": "Sem", "send_welcome_email": 0, "roles": [{"role": "Guest"}]}).insert()
            frappe.db.commit()
        frappe.set_user("sem.papel@synapse.test")
        with self.assertRaises(frappe.PermissionError):
            execution.reject(opid, "tentativa")
        with self.assertRaises(frappe.PermissionError):
            execution.pending_approvals()
        frappe.set_user("Administrator")
        st = frappe.db.sql("SELECT status, approved_by FROM `tabSynapse Operacao` WHERE name=%s", opid, as_dict=True)[0]
        self.assertEqual((st["status"], st["approved_by"]), ("AWAITING_APPROVAL", None))       # intacta

    def test_reject_autorizado_e_fila_do_painel(self):
        name, opid = self._pendente("R2")
        fila = execution.pending_approvals()
        self.assertIn(opid, [f["operation_id"] for f in fila])
        r = execution.reject(opid, "cliente desistiu"); frappe.db.commit()
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(frappe.db.get_value("Sales Order", name, "docstatus"), 1)             # nao cancelou
        self.assertNotIn(opid, [f["operation_id"] for f in execution.pending_approvals()])


class TestAgregacaoRecuperavel(Base):
    def _mk(self, n, key):
        ids = []
        for _ in range(n):
            e = outbox.emit("Item", ITEM, "item.updated", {}, origin_actor="HUMAN")["event_id"]
            outbox.ack(e)
            frappe.db.sql(f"UPDATE `{OB}` SET gate_action='AGGREGATE', agg_key=%s WHERE name=%s", (key, e)); ids.append(e)
        frappe.db.commit(); return ids

    def test_falha_da_lyra_evento_continua_recuperavel_e_so_entao_processed(self):
        set_cfg("agg_max_events", 3); set_cfg("agg_max_wait_seconds", 600); set_cfg("agg_flush_lease_seconds", 2)
        ids = self._mk(3, "k-l")
        self.assertEqual(aggregation.due_groups()[0]["reason"], "MAX_EVENTS")
        fl = aggregation.flush("k-l"); frappe.db.commit()
        self.assertEqual(len(fl["events"]), 3)
        # Lyra indisponivel: Harmonia NAO chama mark_processed. Dentro do lease nada e reentregue (em voo)...
        self.assertEqual(aggregation.due_groups(), [])
        time.sleep(3)                                                # ...lease expira: os MESMOS eventos voltam
        self.assertEqual(aggregation.due_groups()[0]["count"], 3)
        fl2 = aggregation.flush("k-l"); frappe.db.commit()
        self.assertEqual(sorted(fl2["event_ids"]), sorted(ids))
        self.assertEqual({r[0] for r in frappe.db.sql(f"SELECT status FROM `{OB}` WHERE agg_key='k-l'")}, {"ACK"})   # nunca PROCESSED antes da execucao
        outbox.mark_processed(fl2["event_ids"]); frappe.db.commit()                          # Lyra voltou + execucao
        self.assertEqual({r[0] for r in frappe.db.sql(f"SELECT status FROM `{OB}` WHERE agg_key='k-l'")}, {"PROCESSED"})
        time.sleep(3); self.assertEqual(aggregation.due_groups(), [])                          # sem duplicacao


class TestOrdemPorStateVersion(Base):
    def test_10_12_11_nao_descarta_e_segura_o_sucessor(self):
        e = [outbox.emit("Item", ITEM, f"o.v{i}", {}, origin_actor="HUMAN") for i in range(3)]   # versoes v, v+1, v+2
        frappe.db.commit()
        ids = [x["event_id"] for x in e]
        for i in ids: frappe.db.sql(f"UPDATE `{OB}` SET status='DELIVERED' WHERE name=%s", i)
        r12 = outbox.ack(ids[2])                      # chega o 3o ANTES do 2o (10, 12, 11)
        self.assertEqual(r12["predecessor_pending"], [e[0]["state_version"], e[1]["state_version"]])
        self.assertEqual(autonomia.get_state()["estado_operacional"], "HEALTHY")   # atraso, nao lacuna: os eventos existem
        self.assertEqual(frappe.db.get_value("Synapse Outbox", ids[2], "status"), "ACK")   # NAO descartado
        outbox.ack(ids[0]); r11 = outbox.ack(ids[1])
        self.assertEqual(r11["predecessor_pending"], [])
        self.assertEqual(outbox.ack(ids[2])["duplicate"], True)
        self.assertEqual(outbox.predecessors_pending(frappe.db.sql(f"SELECT * FROM `{OB}` WHERE name=%s", ids[2], as_dict=True)[0]), [])

    def test_predecessor_perdido_vai_para_reconciling(self):
        e = [outbox.emit("Item", ITEM, f"o.v{i}", {}, origin_actor="HUMAN") for i in range(3)]; frappe.db.commit()
        frappe.db.sql(f"DELETE FROM `{OB}` WHERE name=%s", e[1]["event_id"]); frappe.db.commit()   # 11 perdido
        outbox.ack(e[0]["event_id"]); r = outbox.ack(e[2]["event_id"]); frappe.db.commit()
        self.assertEqual(r["gap"]["scope"] in ("entity", "stream"), True)
        self.assertEqual(autonomia.get_state()["estado_operacional"], "RECONCILING")


class TestEventosDeNegocioReais(Base):
    """Item 24: caminho real Item / Item Price / Stock Ledger Entry -> Outbox."""
    def test_item_price_e_stock_ledger_entry(self):
        company = frappe.db.get_single_value("Global Defaults", "default_company")
        code = "D-EST-1"
        if not frappe.db.exists("Item", code):
            frappe.get_doc({"doctype": "Item", "item_code": code, "item_name": code, "item_group": frappe.db.get_value("Item Group", {"is_group": 0}),
                            "stock_uom": "Nos", "is_stock_item": 1}).insert()
        frappe.db.commit()
        frappe.db.sql(f"DELETE FROM `{OB}`"); frappe.db.sql("DELETE FROM `tabSynapse Estado`"); frappe.db.commit()
        # preco
        frappe.get_doc({"doctype": "Item Price", "item_code": code, "price_list": "Standard Selling", "price_list_rate": 12.5}).insert(); frappe.db.commit()
        # estoque (Stock Entry -> Stock Ledger Entry real)
        se = frappe.get_doc({"doctype": "Stock Entry", "stock_entry_type": "Material Receipt", "company": company,
                             "items": [{"item_code": code, "qty": 5, "t_warehouse": "Stores - WP", "basic_rate": 10}]})
        se.insert(); se.submit(); frappe.db.commit()
        tipos = {r[0]: r[1] for r in frappe.db.sql(f"SELECT event_type, COUNT(*) FROM `{OB}` WHERE entity_type='Item' AND entity_id=%s GROUP BY event_type", code)}
        print("EVENTOS DO ITEM:", tipos)
        self.assertIn("item.price_changed", tipos)          # Item Price -> evento do ITEM
        self.assertIn("item.stock_changed", tipos)          # Stock Ledger Entry -> evento do ITEM
        ver = frappe.db.get_value("Synapse Estado", f"Item::{code}", "versao")
        self.assertGreaterEqual(ver, 2)                      # versoes da entidade avancaram
        seqs = [r[0] for r in frappe.db.sql(f"SELECT seq FROM `{OB}` ORDER BY seq")]
        self.assertEqual(seqs, list(range(seqs[0], seqs[0] + len(seqs))))
        # o gate classifica ambos de forma deterministica (regra registrada em cancao_logic.js)
        se.reload(); se.cancel(); frappe.db.commit()


if __name__ == "__main__":
    unittest.main()
