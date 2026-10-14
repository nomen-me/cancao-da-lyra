"""API do Ritmo para Harmonia/Lyra/Harpa. Todas exigem o papel de automacao (permissoes do DocType)."""
import json

import frappe

from synapse_core import autonomia, estado, execution, outbox
from synapse_core.config import cfg


def _guard():
    if not frappe.has_permission("Synapse Outbox", "write"):
        frappe.throw("Synapse: sem permissao", frappe.PermissionError)


def _j(v):
    return json.loads(v) if isinstance(v, str) else v


@frappe.whitelist(methods=["POST"])
def outbox_ack(event_id):
    _guard(); return outbox.ack(event_id)


@frappe.whitelist(methods=["POST"])
def outbox_processed(event_ids):
    _guard(); return outbox.mark_processed(_j(event_ids))


@frappe.whitelist(methods=["POST"])
def outbox_failed(event_id, reason, schema_version=None):
    _guard(); return outbox.record_failure(event_id, reason, schema_version)


@frappe.whitelist(methods=["POST"])
def outbox_replay(from_seq=0, limit=500):
    _guard(); return outbox.replay(int(from_seq), int(limit))


@frappe.whitelist(methods=["POST"])
def heartbeat(processed_seq=None):
    _guard(); return autonomia.heartbeat(processed_seq)


@frappe.whitelist(methods=["POST"])
def reconcile():
    _guard(); return autonomia.reconcile()


@frappe.whitelist(methods=["POST"])
def declare_consistent():
    _guard(); return autonomia.declare_consistent()


@frappe.whitelist(methods=["GET", "POST"])
def state():
    _guard()
    return {**autonomia.get_state(), "head": autonomia.stream_head()}


@frappe.whitelist(methods=["POST"])
def execute(operation):
    """Deadlock do InnoDB e transitorio; a operacao e idempotente (operation_id unico), entao repetir e seguro."""
    import time
    _guard()
    op = _j(operation)
    for attempt in range(3):
        try:
            return execution.execute(op)
        except frappe.QueryDeadlockError:
            frappe.db.rollback()
            if attempt == 2:
                raise
            time.sleep(0.05 * (attempt + 1))


@frappe.whitelist(methods=["POST"])
def approve(operation_id):
    return execution.approve(operation_id)


@frappe.whitelist(methods=["POST"])
def reject(operation_id, reason=None):
    return execution.reject(operation_id, reason)


@frappe.whitelist(methods=["GET", "POST"])
def pending_approvals():
    return execution.pending_approvals()


@frappe.whitelist(methods=["POST"])
def gate_mark(event_id, gate_action, agg_key=None):
    _guard()
    frappe.db.sql("UPDATE `tabSynapse Outbox` SET gate_action=%s, agg_key=%s WHERE name=%s", (gate_action, agg_key, event_id))
    return {"event_id": event_id, "gate_action": gate_action, "agg_key": agg_key}


@frappe.whitelist(methods=["POST"])
def aggregation_due():
    from synapse_core import aggregation
    _guard(); return aggregation.due_groups()


@frappe.whitelist(methods=["GET", "POST"])
def unknown_events(limit=50):
    """Eventos sem contrato de gate: registrados e sinalizados; NUNCA viram acao autonoma."""
    _guard()
    return frappe.db.sql(
        "SELECT event_type, COUNT(*) n, MIN(event_timestamp) first_seen, MAX(event_timestamp) last_seen FROM `tabSynapse Outbox` "
        "WHERE gate_action='UNKNOWN' GROUP BY event_type ORDER BY n DESC LIMIT %s", (int(limit),), as_dict=True)


@frappe.whitelist(methods=["POST"])
def aggregation_flush(agg_key):
    from synapse_core import aggregation
    _guard(); return aggregation.flush(agg_key)


@frappe.whitelist(methods=["GET", "POST"])
def entity_state(entity_type, entity_id):
    """Leitura INFORMATIVA: continua funcionando em RECONCILING, mas explicita quando pode estar desatualizada."""
    _guard()
    st = autonomia.get_state()["estado_operacional"]
    pending = frappe.db.sql(
        "SELECT COUNT(*) FROM `tabSynapse Outbox` WHERE entity_type=%s AND entity_id=%s AND status IN ('PENDING','DELIVERED','ACK')",
        (entity_type, str(entity_id)))[0][0]
    return {**autonomia.authority_snapshot(entity_type, entity_id), "operational_state": st,
            "possibly_stale": st != "HEALTHY" or bool(pending), "unprocessed_events": pending}


@frappe.whitelist(methods=["POST"])
def purge():
    _guard(); return outbox.purge()
