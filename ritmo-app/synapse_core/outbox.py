"""Transactional Outbox no Ritmo (Ordem Final secoes 2-7, 10).

O evento e gravado por hook SINCRONO do Frappe (on_change / on_trash), na mesma
conexao/transacao do documento de negocio. O commit acontece uma unica vez, ao
fim da requisicao (frappe.app.sync_database) - ver docs/ORDEM_FINAL_MAPA.md para
o trecho do Frappe v15.121.1 que comprova isso. Nada aqui chama frappe.db.commit()
no caminho de escrita (o unico commit e o purge em lotes, executado por scheduler).
"""
import json

import frappe
import requests
from frappe.utils import add_days, add_to_date, now, now_datetime

from synapse_core import estado, lineage
from synapse_core.config import cfg, cfg_json

SCHEMA_VERSION = "1"
SUPPORTED_SCHEMAS = ("1",)
OUTBOX = "Synapse Outbox"
ACKED_STATES = ("ACK", "PROCESSED", "DEAD_LETTER")

# DocType -> (entity_type, campo do id, event_type). Estoque e preco sao estados
# do ITEM (Bin e atualizado por SQL direto; o fato auditavel e o Stock Ledger Entry).
ENTITY_MAP = {
    "Stock Ledger Entry": ("Item", "item_code", "item.stock_changed"),
    "Item Price": ("Item", "item_code", "item.price_changed"),
}
SNAPSHOT_FIELDS = (
    "name", "status", "docstatus", "customer", "grand_total", "item_code", "qty", "actual_qty",
    "warehouse", "price_list_rate", "posting_date", "company", "synapse_op_id", "modified",
)
ACTION_MAP = {"submit": "submitted", "cancel": "cancelled", "update_after_submit": "updated"}


# ------------------------------------------------------------------ hooks
def _monitored():
    cached = getattr(frappe.local, "synapse_monitored", None)
    if cached is None:
        cached = frappe.local.synapse_monitored = set(cfg_json("doctypes_monitorados"))
    return cached


def reset_cache():
    frappe.local.synapse_monitored = None


def _skip(doc):
    f = frappe.flags
    if f.in_install or f.in_migrate or f.in_patch or f.in_import:
        return True
    if doc.doctype.startswith("Synapse "):
        return True  # o Outbox nao emite evento sobre si mesmo
    return doc.doctype not in _monitored()


def _entity_of(doc):
    if doc.doctype in ENTITY_MAP:
        et, field, event = ENTITY_MAP[doc.doctype]
        return et, doc.get(field), event
    return doc.doctype, doc.name, None


def _snapshot(doc):
    out = {}
    for f in SNAPSHOT_FIELDS:
        v = doc.get(f)
        if v is not None and not isinstance(v, (list, dict)):
            out[f] = v
    return out


def on_change(doc, method=None):
    if _skip(doc):
        return
    et, eid, mapped = _entity_of(doc)
    if doc.flags.in_insert:
        action = "created"
    else:
        action = ACTION_MAP.get(doc.get("_action"), "updated")
    event_type = mapped or f"{doc.doctype.lower().replace(' ', '_')}.{action}"
    emit(et, eid, event_type, _snapshot(doc))


def on_trash(doc, method=None):
    if _skip(doc):
        return
    et, eid, mapped = _entity_of(doc)
    emit(et, eid, mapped or f"{doc.doctype.lower().replace(' ', '_')}.deleted", _snapshot(doc))


# ------------------------------------------------------------------ emissao
def _infer_actor():
    f = frappe.flags
    if f.in_install or f.in_migrate or f.in_patch or getattr(frappe.local, "job", None):
        return "SYSTEM"
    auto = cfg("usuario_automacao")
    if auto and frappe.session.user == auto:
        return "SYSTEM"
    return "HUMAN"


def _chain_stats(chain_id):
    rows = frappe.db.sql(
        f"SELECT event_type, origin_actor, COUNT(*) FROM `tab{OUTBOX}` WHERE chain_id=%s GROUP BY event_type, origin_actor",
        chain_id,
    )
    stats = {"event_count": 0, "event_types": {}, "origin_actors": {}}
    for et, actor, n in rows:
        stats["event_count"] += n
        stats["event_types"][et] = stats["event_types"].get(et, 0) + n
        stats["origin_actors"][actor] = stats["origin_actors"].get(actor, 0) + n
    return stats


def emit(entity_type, entity_id, event_type, payload=None, *, origin_actor=None, origin_operation_id=None, parent=None, bump=True):
    """Grava um evento no Outbox NA TRANSACAO CORRENTE. Nao faz commit."""
    ctx = frappe.flags.get("synapse_ctx") or {}
    origin_actor = origin_actor or ctx.get("origin_actor") or _infer_actor()
    origin_operation_id = origin_operation_id or ctx.get("origin_operation_id")
    parent = parent if parent is not None else ctx.get("parent")

    event_id = lineage.new_id()
    limit = cfg("causal_depth_limit")
    stats = None
    if parent and (int(parent.get("causal_depth") or 0) + 1) > limit:
        stats = _chain_stats(parent["chain_id"])
    lin = lineage.next_lineage(parent, event_id, limit, stats)

    seq = estado.bump_stream()
    # bump=False: eventos de observacao (state_snapshot) nao mudam o estado da entidade,
    # logo nao podem invalidar decision_state_version de decisoes ainda validas.
    version = estado.bump(entity_type, entity_id) if bump else estado.current(entity_type, entity_id)

    doc = frappe.get_doc({
        "doctype": OUTBOX, "event_id": event_id, "seq": seq, "status": "PENDING",
        "event_type": event_type, "entity_type": entity_type, "entity_id": str(entity_id),
        "state_version": version, "event_timestamp": now_datetime(), "origin_actor": origin_actor,
        "origin_operation_id": origin_operation_id, "root_event_id": lin["root_event_id"],
        "parent_event_id": lin["parent_event_id"], "chain_id": lin["chain_id"],
        "previous_chain_id": lin["previous_chain_id"], "causal_depth": lin["causal_depth"],
        "causal_summary": frappe.as_json(lin["causal_summary"]) if lin["causal_summary"] else None,
        "schema_version": SCHEMA_VERSION, "payload": frappe.as_json(payload or {}), "retry_count": 0,
    })
    doc.flags.ignore_permissions = True
    doc.flags.ignore_links = True
    doc.insert()
    _register_dispatch()
    return {"event_id": event_id, "seq": seq, "state_version": version, **lin}


def _register_dispatch():
    if frappe.flags.in_test or frappe.flags.in_install or frappe.flags.in_migrate:
        return
    if frappe.local.flags.get("synapse_dispatch_registered"):
        return
    frappe.local.flags.synapse_dispatch_registered = True

    def _reset():
        frappe.local.flags.synapse_dispatch_registered = False

    def _after_commit():
        _reset()
        frappe.enqueue("synapse_core.outbox.dispatch_pending", queue="short", job_id="synapse_dispatch", deduplicate=True)

    frappe.db.after_commit.add(_after_commit)
    frappe.db.after_rollback.add(_reset)


# ------------------------------------------------------------------ leitura / envelope
def envelope(row):
    """Formato entregue a Harmonia (campos da secao 2 da Ordem Final)."""
    d = {k: row.get(k) for k in (
        "event_id", "root_event_id", "parent_event_id", "chain_id", "previous_chain_id", "origin_actor",
        "origin_operation_id", "entity_type", "entity_id", "event_type", "state_version", "causal_depth",
        "schema_version", "seq", "retry_count")}
    d["event_timestamp"] = str(row.get("event_timestamp"))
    for k in ("payload", "causal_summary"):
        v = row.get(k)
        try:
            d[k] = json.loads(v) if v else None
        except (TypeError, ValueError):
            d[k] = {"__unparseable__": True}  # evento corrompido: a validacao da Harmonia o levara a DLQ
    return d


def _row(event_id, for_update=False):
    rows = frappe.db.sql(
        f"SELECT * FROM `tab{OUTBOX}` WHERE name=%s" + (" FOR UPDATE" if for_update else ""), event_id, as_dict=True
    )
    if not rows:
        frappe.throw(f"Synapse Outbox: evento inexistente: {event_id}", frappe.DoesNotExistError)
    return rows[0]


# ------------------------------------------------------------------ entrega (relay) / ACK / PROCESSED
def dispatch_pending(limit=100):
    """Relay do Outbox -> Harmonia. Reentrega o que ficou sem ACK/PROCESSED (queda da ponte)."""
    url = cfg("harmonia_ingest_url")
    if not url:
        return {"skipped": "harmonia_ingest_url nao configurada"}
    # Corte calculado em Python (mesmo fuso que grava delivered_at). NUNCA misturar com NOW() do MariaDB:
    # o Frappe grava no fuso do site e o servidor SQL costuma rodar em UTC.
    cutoff = add_to_date(now_datetime(), seconds=-cfg("redelivery_after_seconds"))
    rows = frappe.db.sql(
        f"""SELECT * FROM `tab{OUTBOX}`
            WHERE status='PENDING'
               OR (status IN ('DELIVERED','ACK') AND COALESCE(delivered_at, creation) < %s)
            ORDER BY seq LIMIT %s""",
        (cutoff, limit), as_dict=True,
    )
    sent = failed = 0
    for r in rows:
        try:
            resp = requests.post(url, json=envelope(r), timeout=10)
            ok = 200 <= resp.status_code < 300
        except requests.RequestException:
            ok = False
        if ok:
            frappe.db.sql(
                f"UPDATE `tab{OUTBOX}` SET status=IF(status='PENDING','DELIVERED',status), delivered_at=%s WHERE name=%s",
                (now(), r["name"]))
            sent += 1
        else:
            failed += 1
    frappe.db.commit()  # job de scheduler: nao esta dentro de transacao de negocio
    return {"sent": sent, "failed": failed}


def ack(event_id):
    from synapse_core import autonomia
    r = _row(event_id, for_update=True)
    if r["status"] in ACKED_STATES:
        return {"duplicate": True, "status": r["status"], "event_id": event_id}
    higher = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tab{OUTBOX}` WHERE seq > %s AND status IN ('ACK','PROCESSED')", r["seq"])[0][0]
    frappe.db.sql(f"UPDATE `tab{OUTBOX}` SET status='ACK', ack_at=%s WHERE name=%s", (now(), event_id))
    gap = autonomia.advance_ack_watermark(r)
    return {"duplicate": False, "status": "ACK", "event_id": event_id, "late": bool(higher), "gap": gap,
            "predecessor_pending": predecessors_pending(r)}


def predecessors_pending(r):
    """Versoes ANTERIORES da mesma entidade que ainda nao chegaram a Harmonia (PENDING/DELIVERED).
    state_version e a autoridade de ordem: quem chega antes (10, 12, 11) NAO e descartado - fica
    ACK e a Harmonia so o processa quando nao houver predecessor pendente. Predecessor AUSENTE
    (linha inexistente) e lacuna real e ja levou a RECONCILING em advance_ack_watermark."""
    if not r.get("entity_type") or not r.get("state_version"):
        return []
    rows = frappe.db.sql(
        f"""SELECT state_version FROM `tab{OUTBOX}` WHERE entity_type=%s AND entity_id=%s AND state_version < %s
            AND status IN ('PENDING','DELIVERED') ORDER BY state_version""", (r["entity_type"], r["entity_id"], r["state_version"]))
    return [x[0] for x in rows]


def mark_processed(event_ids):
    from synapse_core import autonomia
    done, refused = [], []
    for eid in event_ids:
        r = _row(eid, for_update=True)
        if r["status"] == "PROCESSED":
            done.append(eid)
        elif r["status"] == "ACK":
            frappe.db.sql(f"UPDATE `tab{OUTBOX}` SET status='PROCESSED', processed_at=%s WHERE name=%s", (now(), eid))
            done.append(eid)
        else:
            refused.append({"event_id": eid, "status": r["status"]})
    autonomia.advance_processed_watermark()
    return {"processed": done, "refused": refused}


def record_failure(event_id, reason, schema_version=None):
    """Retry -> DLQ. Apos dlq_max_tentativas falhas o evento e ISOLADO (nao apagado) e a
    Synapse passa a RECONCILING: DLQ nao significa HEALTHY (secao 10)."""
    from synapse_core import autonomia
    r = _row(event_id, for_update=True)
    if r["status"] in ("PROCESSED", "DEAD_LETTER"):
        return {"event_id": event_id, "status": r["status"], "dead_lettered": r["status"] == "DEAD_LETTER",
                "retry_count": r["retry_count"]}
    n = int(r["retry_count"] or 0) + 1
    t = now()
    limit = cfg("dlq_max_tentativas")
    frappe.db.sql(
        f"""UPDATE `tab{OUTBOX}` SET retry_count=%s, first_failure_at=COALESCE(first_failure_at,%s), last_failure_at=%s,
            failure_reason=%s, schema_version=COALESCE(%s, schema_version) WHERE name=%s""",
        (n, t, t, (reason or "")[:1000], schema_version, event_id))
    dead = n >= limit
    if dead:
        frappe.db.sql(
            f"UPDATE `tab{OUTBOX}` SET status='DEAD_LETTER', dead_lettered_at=%s, dlq_impact_resolved=0 WHERE name=%s",
            (t, event_id))
        autonomia.enter_reconciling(f"DLQ: evento {event_id} isolado apos {n} falhas", event_id=event_id)
    return {"event_id": event_id, "retry_count": n, "max": limit, "dead_lettered": dead,
            "status": "DEAD_LETTER" if dead else r["status"]}


def replay(from_seq, limit=500):
    rows = frappe.db.sql(
        f"""SELECT * FROM `tab{OUTBOX}` WHERE seq >= %s AND status IN ('PENDING','DELIVERED','ACK')
            ORDER BY seq LIMIT %s""", (from_seq, limit), as_dict=True)
    return [envelope(r) for r in rows]


# ------------------------------------------------------------------ retencao
def purge(retention_days=None, batch_size=None):
    """Remove SOMENTE eventos PROCESSED, mais velhos que a retencao e ja cobertos pelo
    watermark. Nunca toca PENDING/DELIVERED/ACK/DEAD_LETTER (replay/auditoria).
    Em lotes, com commit por lote (job de scheduler, fora de transacao de negocio)."""
    from synapse_core import autonomia
    days = cfg("outbox_retention_days") if retention_days is None else retention_days
    size = cfg("purge_batch_size") if batch_size is None else batch_size
    cutoff = add_days(now_datetime(), -days)
    wm = autonomia.get_state()["watermark_processed_seq"]
    total, batches = 0, 0
    while True:
        frappe.db.sql(
            f"""DELETE FROM `tab{OUTBOX}` WHERE status='PROCESSED' AND processed_at < %s AND seq <= %s
                ORDER BY seq LIMIT %s""", (cutoff, wm, size))
        n = frappe.db.sql("SELECT ROW_COUNT()")[0][0]
        frappe.db.commit()
        if not n:
            break
        total += n
        batches += 1
    return {"deleted": total, "batches": batches, "cutoff": str(cutoff), "watermark": wm}
