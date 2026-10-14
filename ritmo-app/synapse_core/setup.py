"""after_install / after_migrate: papel, custom fields de idempotencia, defaults."""
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

ROLE = "Synapse Automacao"


def ensure():
    if not frappe.db.exists("Role", ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": ROLE, "desk_access": 0}).insert(ignore_permissions=True)
    # Mesma definicao do instalador (synapse_op_id unico); idempotente, nao duplica.
    create_custom_fields({
        "Sales Order": [dict(fieldname="synapse_op_id", label="Synapse - ID da operacao (idempotencia)", fieldtype="Data",
                             insert_after="customer", unique=1, read_only=1)],
    }, ignore_validate=True)
    frappe.db.set_single_value("Synapse Autonomia", "estado_operacional",
                               frappe.db.get_single_value("Synapse Autonomia", "estado_operacional") or "HEALTHY")


def after_install():
    ensure()


def after_migrate():
    ensure()
