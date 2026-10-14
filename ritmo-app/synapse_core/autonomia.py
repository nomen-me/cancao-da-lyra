"""Estado operacional, watermark, heartbeat, reconciliacao (Ordem Final secoes 1, 8, 9, 10, 16).

HEALTHY -> RECONCILING -> CONSISTENT_STATE -> HEALTHY. CONSISTENT_STATE so e declarado por
declare_consistent() depois de verify_consistency() comprovar: sem lacuna nao resolvida,
sem evento pendente ate o head, sem DLQ com impacto nao validado.
"""
import frappe
from frappe.utils import now, now_datetime, time_diff_in_seconds

from synapse_core import estado, outbox
from synapse_core.config import DT, cfg

STATES = ("HEALTHY", "RECONCILING", "CONSISTENT_STATE")
OUTBOX = outbox.OUTBOX
UNPROCESSED = ("PENDING", "DELIVERED", "ACK")
MAX_GAP_SCAN = 100000


def get_state():
    keys = ("estado_operacional", "estado_desde", "motivo", "watermark_processed_seq", "watermark_ack_seq",
            "gap_from", "gap_to", "gap_resolvido_ate", "last_heartbeat_at")
    s = {k: cfg(k) for k in keys}
    s["estado_operacional"] = s["estado_operacional"] or "HEALTHY"
    return s


def state_for_execution():
    """Leitura com lock compartilhado: a execucao e a transicao para RECONCILING se serializam."""
    rows = frappe.db.sql(
        "SELECT value FROM `tabSingles` WHERE doctype=%s AND field='estado_operacional' LOCK IN SHARE MODE", DT)
    return (rows[0][0] if rows and rows[0][0] else "HEALTHY")


def _set(**fields):
    frappe.db.set_single_value(DT, fields, update_modified=False)
    frappe.db.value_cache.pop(DT, None)


def set_state(new, motivo=None, **extra):
    assert new in STATES, new
    old = get_state()["estado_operacional"]
    _set(estado_operacional=new, estado_desde=now(), motivo=(motivo or "")[:500], **extra)
    if old != new:
        outbox.emit("Synapse", "autonomia", "synapse.state_changed",
                    {"from": old, "to": new, "motivo": motivo}, origin_actor="SYSTEM")
    return {"from": old, "to": new}


def enter_reconciling(motivo, event_id=None, gap_from=None, gap_to=None):
    extra = {}
    if gap_from is not None:
        extra.update(gap_from=gap_from, gap_to=gap_to)
    if get_state()["estado_operacional"] == "RECONCILING":
        _set(motivo=(motivo or "")[:500], **extra)
        return {"from": "RECONCILING", "to": "RECONCILING"}
    return set_state("RECONCILING", motivo, **extra)


# ------------------------------------------------------------------ watermarks
def stream_head():
    rows = frappe.db.sql("SELECT versao FROM `tabSynapse Estado` WHERE name=%s", estado.STREAM_KEY)
    return int(rows[0][0]) if rows else 0


def _status_of_seq(seq):
    r = frappe.db.sql(f"SELECT status, dlq_impact_resolved FROM `tab{OUTBOX}` WHERE seq=%s", seq)
    return (r[0][0], r[0][1]) if r else (None, None)


def advance_processed_watermark():
    s = get_state()
    wm, resolved = s["watermark_processed_seq"], s["gap_resolvido_ate"]
    start = wm
    while True:
        nxt = wm + 1
        status, dlq_ok = _status_of_seq(nxt)
        if status == "PROCESSED" or (status == "DEAD_LETTER" and dlq_ok):
            wm = nxt
        elif status is None and nxt <= resolved:
            wm = nxt  # lacuna irrecuperavel ja resolvida por resync da autoridade
        else:
            break
    if wm != start:
        _set(watermark_processed_seq=wm)
    return wm


def advance_ack_watermark(row):
    """Avanca as marcas de ACK (stream e por entidade) e detecta lacunas reais
    (linha ausente), diferenciando de simples atraso/desordem (linha presente, nao ACK)."""
    s = get_state()
    wm = s["watermark_ack_seq"]
    start = wm
    while True:
        status, _ = _status_of_seq(wm + 1)
        if status in outbox.ACKED_STATES:
            wm += 1
        else:
            break
    if wm != start:
        _set(watermark_ack_seq=wm)
    gap = None
    if row["seq"] > wm + 1:
        present = {r[0] for r in frappe.db.sql(f"SELECT seq FROM `tab{OUTBOX}` WHERE seq > %s AND seq < %s", (wm, row["seq"]))}
        missing = [x for x in range(wm + 1, row["seq"]) if x not in present and x > s["gap_resolvido_ate"]]
        if missing:
            gap = {"scope": "stream", "from": missing[0], "to": missing[-1], "missing": missing[:50]}
    egap = _advance_entity_ack(row)
    gap = gap or egap
    if gap:
        enter_reconciling(f"lacuna detectada: {gap['scope']} {gap.get('entity', '')} {gap['from']}..{gap['to']}",
                          gap_from=gap["from"], gap_to=gap["to"])
    return gap


def _advance_entity_ack(row):
    et, eid, v = row["entity_type"], row["entity_id"], row["state_version"]
    if not et or eid in (None, "") or not v:
        return None
    key = estado.key_of(et, eid)
    rows = frappe.db.sql("SELECT versao_ack FROM `tabSynapse Estado` WHERE name=%s FOR UPDATE", key)
    if not rows:
        return None
    va = int(rows[0][0] or 0)
    start = va
    acked = {r[0]: r[1] for r in frappe.db.sql(
        f"SELECT state_version, status FROM `tab{OUTBOX}` WHERE entity_type=%s AND entity_id=%s AND state_version > %s",
        (et, str(eid), va))}
    while acked.get(va + 1) in outbox.ACKED_STATES:
        va += 1
    if va != start:
        frappe.db.sql("UPDATE `tabSynapse Estado` SET versao_ack=%s WHERE name=%s", (va, key))
    if v > va + 1:
        missing = [x for x in range(va + 1, v) if x not in acked]
        if missing:
            return {"scope": "entity", "entity": f"{et}::{eid}", "from": missing[0], "to": missing[-1], "missing": missing}
    return None


def _missing_seqs(wm, head):
    if head - wm > MAX_GAP_SCAN:
        head = wm + MAX_GAP_SCAN
    present = {r[0] for r in frappe.db.sql(f"SELECT seq FROM `tab{OUTBOX}` WHERE seq > %s AND seq <= %s", (wm, head))}
    resolved = cfg("gap_resolvido_ate")
    return [x for x in range(wm + 1, head + 1) if x not in present and x > resolved]


# ------------------------------------------------------------------ heartbeat
def heartbeat(processed_seq=None):
    """Nao responde 'a ponte esta viva?' e sim 'ate qual evento a visao e confiavel?'."""
    s = get_state()
    wm, head = s["watermark_processed_seq"], stream_head()
    problems = []
    missing = _missing_seqs(wm, head)
    if missing:
        problems.append({"type": "gap", "from": missing[0], "to": missing[-1]})
    # Evento aguardando agregacao e ESPERA LEGITIMA ate agg_max_wait_seconds: so vira atraso o que passar disso.
    oldest = frappe.db.sql(
        f"""SELECT MIN(event_timestamp) FROM `tab{OUTBOX}` WHERE status IN ('PENDING','DELIVERED','ACK')
            AND NOT (status='ACK' AND gate_action='AGGREGATE' AND flushed_at IS NULL)""")[0][0]
    oldest_agg = frappe.db.sql(
        f"""SELECT MIN(ack_at) FROM `tab{OUTBOX}` WHERE status='ACK' AND gate_action='AGGREGATE' AND flushed_at IS NULL""")[0][0]
    lag = time_diff_in_seconds(now_datetime(), oldest) if oldest else 0
    if oldest_agg:
        lag = max(lag, time_diff_in_seconds(now_datetime(), oldest_agg) - cfg("agg_max_wait_seconds"))
    if lag > cfg("heartbeat_max_lag_segundos"):
        problems.append({"type": "lag", "seconds": lag})
    if processed_seq is not None and int(processed_seq) > wm:
        problems.append({"type": "divergence", "consumer_processed_seq": int(processed_seq), "ritmo_watermark": wm})
    dlq = frappe.db.sql(f"SELECT COUNT(*) FROM `tab{OUTBOX}` WHERE status='DEAD_LETTER' AND dlq_impact_resolved=0")[0][0]
    if dlq:
        problems.append({"type": "dlq_unresolved", "count": dlq})
    _set(last_heartbeat_at=now())
    if problems and s["estado_operacional"] == "HEALTHY":
        first = problems[0]
        enter_reconciling(f"heartbeat: {first['type']}", gap_from=first.get("from"), gap_to=first.get("to"))
    trusted = (missing[0] - 1) if missing else wm
    return {"state": get_state()["estado_operacional"], "head": head, "watermark_processed_seq": wm,
            "trusted_until_seq": trusted, "lag_seconds": lag, "problems": problems}


# ------------------------------------------------------------------ reconciliacao
def authority_snapshot(entity_type, entity_id):
    """Estado reconstruido da FONTE DE AUTORIDADE (o proprio Ritmo), nao do fluxo de eventos."""
    snap = {"entity_type": entity_type, "entity_id": str(entity_id), "state_version": estado.current(entity_type, entity_id)}
    if entity_type == "Synapse":
        return snap
    if frappe.db.exists(entity_type, entity_id):
        doc = frappe.get_doc(entity_type, entity_id)
        snap["doc"] = outbox._snapshot(doc)
        snap["exists"] = True
        if entity_type == "Item":
            snap["stock_total"] = float(frappe.db.sql("SELECT COALESCE(SUM(actual_qty),0) FROM `tabBin` WHERE item_code=%s", entity_id)[0][0])
    else:
        snap["exists"] = False
    return snap


def reconcile():
    s = get_state()
    wm, head = s["watermark_processed_seq"], stream_head()
    report = {"missing": [], "resync_entities": 0, "dlq_validated": [], "dlq_blocking": [], "pending_replay": 0}
    missing = _missing_seqs(wm, head)
    if missing:
        report["missing"] = [missing[0], missing[-1]]
        # lacuna que o fluxo de eventos nao recupera: reconstruir da autoridade
        for (key, et, eid) in frappe.db.sql(
                "SELECT name, entity_type, entity_id FROM `tabSynapse Estado` WHERE name != %s", estado.STREAM_KEY):
            if et == "Synapse":
                continue
            outbox.emit(et, eid, "state_snapshot", authority_snapshot(et, eid), origin_actor="SYSTEM", bump=False)
            report["resync_entities"] += 1
        _set(gap_resolvido_ate=max(missing))
    for r in frappe.db.sql(
            f"SELECT event_id, entity_type, entity_id FROM `tab{OUTBOX}` WHERE status='DEAD_LETTER' AND dlq_impact_resolved=0",
            as_dict=True):
        if r["entity_type"] and r["entity_id"]:
            outbox.emit(r["entity_type"], r["entity_id"], "state_snapshot",
                        authority_snapshot(r["entity_type"], r["entity_id"]), origin_actor="SYSTEM", bump=False)
            frappe.db.sql(f"UPDATE `tab{OUTBOX}` SET dlq_impact_resolved=1 WHERE name=%s", r["event_id"])
            report["dlq_validated"].append(r["event_id"])
        else:
            report["dlq_blocking"].append(r["event_id"])  # impacto indeterminavel: mutacoes permanecem bloqueadas
    advance_processed_watermark()
    report["pending_replay"] = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tab{OUTBOX}` WHERE status IN ('PENDING','DELIVERED','ACK')")[0][0]
    return report


def verify_consistency():
    s = get_state()
    wm, head = s["watermark_processed_seq"], stream_head()
    reasons = []
    missing = _missing_seqs(wm, head)
    if missing:
        reasons.append(f"lacuna nao resolvida: seq {missing[0]}..{missing[-1]}")
    pend = frappe.db.sql(
        f"SELECT COUNT(*) FROM `tab{OUTBOX}` WHERE status IN ('PENDING','DELIVERED','ACK') AND seq <= %s", head)[0][0]
    if pend:
        reasons.append(f"{pend} evento(s) ainda nao PROCESSED")
    dlq = frappe.db.sql(f"SELECT COUNT(*) FROM `tab{OUTBOX}` WHERE status='DEAD_LETTER' AND dlq_impact_resolved=0")[0][0]
    if dlq:
        reasons.append(f"{dlq} evento(s) em DLQ com impacto nao validado")
    if wm < head and not reasons:
        reasons.append(f"watermark {wm} < head {head}")
    return {"ok": not reasons, "reasons": reasons, "watermark": wm, "head": head}


def declare_consistent():
    """RECONCILING -> CONSISTENT_STATE (somente se comprovado) -> revalida HOLDs -> HEALTHY."""
    from synapse_core import execution
    cur = get_state()["estado_operacional"]
    if cur != "RECONCILING":
        return {"declared": False, "state": cur, "reason": "nao esta em RECONCILING"}
    v = verify_consistency()
    if not v["ok"]:
        return {"declared": False, "state": cur, **v}
    set_state("CONSISTENT_STATE", "reconciliacao comprovou consistencia", gap_from=0, gap_to=0)
    released = execution.release_holds()
    set_state("HEALTHY", "holds revalidados")
    return {"declared": True, "state": "HEALTHY", "holds": released, **v}


def heartbeat_tick():
    """Heartbeat proprio do Ritmo (scheduler): detecta ponte parada mesmo que a Harmonia esteja morta."""
    r = heartbeat()
    frappe.db.commit()
    return r
