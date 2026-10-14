"""Contrato Lyra -> Ritmo: a operacao vem do CODIGO REAL da Lyra (autonomy.js), executada no Ritmo real."""
import json, os, subprocess, tempfile, unittest, frappe
from synapse_core import execution, estado
from synapse_core.tests.test_cancao import Base, so_count, touch_item, ITEM


class TestContratoLyra(Base):
    def test_operacao_da_lyra_e_aceita_e_idempotente(self):
        touch_item()
        lyra_dir = os.environ.get("LYRA_API_DIR")
        if not lyra_dir:
            self.skipTest("defina LYRA_API_DIR (pasta lyra-central-api) para gerar a operacao com o codigo real da Lyra")
        out = tempfile.mktemp(suffix=".json")           # operacao gerada AGORA pelo autonomy.js real (TTL fresco)
        subprocess.run(["node", "--test", "test/autonomy.test.js"], cwd=lyra_dir, env={**os.environ, "CONTRACT_OUT": out}, check=True, capture_output=True)
        op = json.load(open(out))
        op["basis"][0]["decision_state_version"] = estado.current("Item", ITEM)   # versao real vigente
        r1 = execution.execute(json.loads(json.dumps(op))); frappe.db.commit()
        self.assertEqual(r1["status"], "executed", r1)
        r2 = execution.execute(json.loads(json.dumps(op))); frappe.db.commit()   # reprocessamento do mesmo evento
        self.assertTrue(r2.get("replayed")); self.assertEqual(so_count(op["operation_id"]), 1)
        # linhagem da Lyra propagada aos eventos gerados pela execucao
        row = frappe.db.sql("SELECT origin_actor, root_event_id, parent_event_id, chain_id, causal_depth, origin_operation_id FROM `tabSynapse Outbox` WHERE entity_type='Sales Order' ORDER BY seq LIMIT 1", as_dict=True)[0]
        self.assertEqual((row["origin_actor"], row["root_event_id"], row["parent_event_id"], row["chain_id"], row["causal_depth"], row["origin_operation_id"]),
                         ("LYRA", "root-1", "contrato-1", "ch-1", 3, op["operation_id"]))
