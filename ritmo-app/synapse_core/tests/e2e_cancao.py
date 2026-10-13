"""Ciclo da Cancao com componentes REAIS: Ritmo (Frappe/MariaDB) -> relay -> n8n 2.40.7 (workflows reais, ativos)
-> ACK/gate/agregacao -> Lyra Central real (/v1/decide). Chatwoot = receptor HTTP (stand-in). Gemini NAO existe aqui."""
import json, time, urllib.request, os
import frappe
from synapse_core import outbox, autonomia
from synapse_core.config import set_cfg

KEY = os.environ.get("RITMO_KEY", "503ee1ae7d3efc6:95704671da1ea39")
OB = "tabSynapse Outbox"
def rest(method, path, body=None):
    r = urllib.request.Request("http://127.0.0.1:8011/api/resource/" + path, data=(json.dumps(body).encode() if body is not None else None), method=method,
                               headers={"Authorization": "token " + KEY, "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=60).read())
def snap(titulo, where="1=1"):
    frappe.db.commit()
    rows = frappe.db.sql(f"SELECT seq,event_type,status,gate_action,retry_count,flushed_at IS NOT NULL fl FROM `{OB}` WHERE {where} ORDER BY seq", as_dict=True)
    print(f"--- {titulo}")
    for r in rows: print(f"   seq={r['seq']:<3} {r['event_type']:<26} {r['status']:<10} gate={str(r['gate_action']):<9} retry={r['retry_count']} flushed={bool(r['fl'])}")
    return rows
def relay(): r = outbox.dispatch_pending(); frappe.db.commit(); return r

def main():
    frappe.set_user("Administrator")
    for t in ("Synapse Outbox", "Synapse Estado", "Synapse Operacao"): frappe.db.sql(f"DELETE FROM `tab{t}`")
    frappe.db.sql("DELETE FROM tabSingles WHERE doctype='Synapse Autonomia'"); frappe.db.commit(); frappe.db.value_cache.pop("Synapse Autonomia", None)
    for k, v in (("harmonia_ingest_url", "http://127.0.0.1:5678/webhook/synapse-evento"), ("redelivery_after_seconds", 2), ("agg_max_events", 3),
                 ("agg_max_wait_seconds", 600), ("agg_flush_lease_seconds", 6)): set_cfg(k, v)
    frappe.db.commit()
    item = "ITEM-CANCAO"
    print("A) alteracao REAL no Ritmo (HTTP PUT /api/resource/Item) -> Outbox transacional -> relay -> n8n")
    rest("PUT", f"Item/{item}", {"description": f"e2e {time.time()}"}); print("   relay:", relay())
    snap("apos 1 item.updated (esperado ACK + AGGREGATE: aguarda MAX_EVENTS/MAX_WAIT; NAO PROCESSED)")

    print("B/C) evento desconhecido e evento ignoravel")
    outbox.emit("Item", item, "algo.novo", {"x": 1}, origin_actor="HUMAN"); outbox.emit("Item", item, "operation_held", {}, origin_actor="SYSTEM"); frappe.db.commit()
    print("   relay:", relay()); snap("esperado: algo.novo -> UNKNOWN+PROCESSED ; operation_held -> IGNORE+PROCESSED", "event_type IN ('algo.novo','operation_held')")

    print("D) MAX_EVENTS=3: mais 2 alteracoes reais -> 3o evento aciona flush imediato -> /v1/decide com events completos -> Gemini indisponivel")
    for _ in range(2):
        rest("PUT", f"Item/{item}", {"description": f"e2e {time.time()}"}); relay()
    time.sleep(3); rows = snap("esperado: 3 item.updated ACK flushed=True, NENHUM PROCESSED (Lyra falhou => recuperavel)", "event_type='item.updated'")
    print("   PROCESSED entre os agregados:", sum(1 for r in rows if r["status"] == "PROCESSED"))

    print("E) evento de controle intent_expired (FORWARD) -> Lyra real responde SEM LLM -> notificacao -> Harpa(Chatwoot stand-in)")
    outbox.emit("Item", item, "intent_expired", {"operation_id": "op-teste", "conversa_id": "77"}, origin_actor="SYSTEM"); frappe.db.commit()
    print("   relay:", relay()); time.sleep(3)
    snap("esperado: intent_expired PROCESSED, gate=FORWARD", "event_type='intent_expired'")
    try: print("   Chatwoot recebeu:", open("/home/claude/work/e2e/chatwoot_received.jsonl").read().strip().splitlines()[-1][:230])
    except Exception as e: print("   Chatwoot recebeu: NADA", e)

    print("F) evento corrompido -> 422 -> retries -> DLQ -> RECONCILING")
    e = outbox.emit("Item", item, "veneno.teste", {"a": 1}, origin_actor="HUMAN")["event_id"]; frappe.db.commit()
    frappe.db.sql(f"UPDATE `{OB}` SET payload='{{corrompido' WHERE name=%s", e); frappe.db.commit()
    for i in range(3): relay(); time.sleep(1)
    snap("esperado: veneno.teste DEAD_LETTER retry=3", "event_type='veneno.teste'")
    print("   estado operacional:", autonomia.get_state()["estado_operacional"])

if __name__ == "__main__":
    main()


def fase_queda_lyra(fase):
    """Item 23 (sem Gemini): fase 'antes' = Lyra DESLIGADA; fase 'depois' = Lyra religada."""
    frappe.set_user("Administrator")
    if fase == "antes":
        for t in ("Synapse Outbox", "Synapse Estado", "Synapse Operacao"): frappe.db.sql(f"DELETE FROM `tab{t}`")
        frappe.db.sql("DELETE FROM tabSingles WHERE doctype='Synapse Autonomia'"); frappe.db.commit(); frappe.db.value_cache.pop("Synapse Autonomia", None)
        for k, v in (("harmonia_ingest_url", "http://127.0.0.1:5678/webhook/synapse-evento"), ("redelivery_after_seconds", 2)): set_cfg(k, v)
        outbox.emit("Item", "ITEM-CANCAO", "intent_expired", {"operation_id": "op-queda", "conversa_id": "88"}, origin_actor="SYSTEM"); frappe.db.commit()
    print("   relay:", relay()); time.sleep(2)
    snap(f"fase {fase}")
