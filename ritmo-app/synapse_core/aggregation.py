"""Agregacao com DUAS condicoes de flush: MAX_EVENTS ou MAX_WAIT (Ordem Final secao 14).

O buffer e o proprio Synapse Outbox (nao ha segunda fila): linhas ACK com gate_action='AGGREGATE'
que ainda nao foram PROCESSED. flush() devolve os ENVELOPES COMPLETOS (o mesmo formato do Outbox) e
NAO marca PROCESSED - so a Harmonia o faz depois de Lyra + execucao.

flushed_at funciona como LEASE de agg_flush_lease_seconds: se a Lyra/Harmonia falhar depois do flush,
o lease expira e os eventos voltam a ser elegiveis (continuam recuperaveis, nada e perdido nem duplicado
porque o operation_id da Lyra e derivado do event_id).
"""
import frappe
from frappe.utils import add_to_date, now, now_datetime, time_diff_in_seconds

from synapse_core import outbox
from synapse_core.config import cfg

OUTBOX = outbox.OUTBOX


def _lease_cutoff():
    return add_to_date(now_datetime(), seconds=-cfg("agg_flush_lease_seconds"))


def due_groups(now_dt=None):
    now_dt = now_dt or now_datetime()
    max_events, max_wait = cfg("agg_max_events"), cfg("agg_max_wait_seconds")
    groups = frappe.db.sql(
        f"""SELECT agg_key, COUNT(*) n, MIN(ack_at) oldest FROM `tab{OUTBOX}`
            WHERE status='ACK' AND gate_action='AGGREGATE' AND agg_key IS NOT NULL
              AND (flushed_at IS NULL OR flushed_at < %s)
            GROUP BY agg_key""", (_lease_cutoff(),), as_dict=True)
    due = []
    for g in groups:
        age = time_diff_in_seconds(now_dt, g["oldest"])
        if g["n"] >= max_events:
            due.append({"agg_key": g["agg_key"], "count": g["n"], "age_seconds": age, "reason": "MAX_EVENTS"})
        elif age >= max_wait:
            due.append({"agg_key": g["agg_key"], "count": g["n"], "age_seconds": age, "reason": "MAX_WAIT"})
    return due


def flush(agg_key):
    rows = frappe.db.sql(
        f"""SELECT * FROM `tab{OUTBOX}` WHERE status='ACK' AND gate_action='AGGREGATE' AND agg_key=%s
            AND (flushed_at IS NULL OR flushed_at < %s) ORDER BY seq FOR UPDATE""", (agg_key, _lease_cutoff()), as_dict=True)
    if rows:
        frappe.db.sql(f"UPDATE `tab{OUTBOX}` SET flushed_at=%s WHERE name IN %s", (now(), tuple(r["name"] for r in rows)))
    return {"agg_key": agg_key, "events": [outbox.envelope(r) for r in rows], "event_ids": [r["event_id"] for r in rows]}
