"""Contadores de versao (Synapse Estado).

- STREAM_KEY: sequencia global do Outbox. Incrementada NA MESMA TRANSACAO do
  negocio, sob lock de linha (InnoDB), portanto:
    * sem buracos (rollback desfaz o incremento junto com o negocio);
    * ordem de commit == ordem de seq (a linha fica travada ate o commit).
  Custo assumido: escritas que geram evento serializam nessa linha ate o commit.
- '<entity_type>::<entity_id>': state_version por entidade (optimistic lock).
"""
import frappe

STREAM_KEY = "__stream__"


def key_of(entity_type, entity_id):
    return f"{entity_type}::{entity_id}"


def _bump_key(key, entity_type=None, entity_id=None):
    now = frappe.utils.now()
    user = frappe.session.user or "Administrator"
    frappe.db.sql(
        """INSERT INTO `tabSynapse Estado`
             (name, creation, modified, modified_by, owner, docstatus, idx, chave, entity_type, entity_id, versao, versao_ack)
           VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,%s,1,0)
           ON DUPLICATE KEY UPDATE versao = versao + 1, modified = VALUES(modified)""",
        (key, now, now, user, user, key, entity_type, entity_id),
    )
    return int(frappe.db.sql("SELECT versao FROM `tabSynapse Estado` WHERE name=%s", key)[0][0])


def bump_stream():
    return _bump_key(STREAM_KEY, "__stream__", "")


def bump(entity_type, entity_id):
    return _bump_key(key_of(entity_type, entity_id), entity_type, str(entity_id))


def current(entity_type, entity_id, for_update=False):
    sql = "SELECT versao FROM `tabSynapse Estado` WHERE name=%s" + (" FOR UPDATE" if for_update else "")
    rows = frappe.db.sql(sql, key_of(entity_type, entity_id))
    return int(rows[0][0]) if rows else 0


def lock_stream():
    """Ordem global de locks: stream -> entidades (evita deadlock entre execucao e hooks)."""
    frappe.db.sql(
        """INSERT INTO `tabSynapse Estado`
             (name, creation, modified, modified_by, owner, docstatus, idx, chave, entity_type, entity_id, versao, versao_ack)
           VALUES (%s,%s,%s,'Administrator','Administrator',0,0,%s,'__stream__','',0,0)
           ON DUPLICATE KEY UPDATE modified = modified""",
        (STREAM_KEY, frappe.utils.now(), frappe.utils.now(), STREAM_KEY),
    )
    frappe.db.sql("SELECT versao FROM `tabSynapse Estado` WHERE name=%s FOR UPDATE", STREAM_KEY)
