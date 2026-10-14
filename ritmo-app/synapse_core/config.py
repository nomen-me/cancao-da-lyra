"""Leitura da configuracao do Synapse (Single 'Synapse Autonomia').

Fonte unica dos valores: os defaults declarados no proprio DocType JSON. Nada de
numeros magicos espalhados pelo codigo. Leitura direta em tabSingles, sem o
value_cache do Frappe (processos longos - scheduler/worker - nao podem enxergar
configuracao velha).
"""
import json

import frappe

DT = "Synapse Autonomia"


def cfg(key):
    row = frappe.db.sql("SELECT value FROM `tabSingles` WHERE doctype=%s AND field=%s", (DT, key))
    df = frappe.get_meta(DT).get_field(key)
    if df is None:
        frappe.throw(f"Synapse: configuracao desconhecida: {key}")
    raw = row[0][0] if row else None
    if raw is None or raw == "":
        raw = df.default
    if df.fieldtype == "Int":
        return int(raw) if raw not in (None, "") else 0
    return raw


def cfg_json(key):
    raw = cfg(key)
    return json.loads(raw) if raw else []


def set_cfg(key, value):
    """Uso administrativo/testes: grava o valor sem cache."""
    frappe.db.set_single_value(DT, key, value)
    frappe.db.value_cache.pop(DT, None)
