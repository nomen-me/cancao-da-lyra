"""Camada de execucao (autoridade) - Ordem Final secoes 5, 11, 12, 15, 16.

TODA mutacao autonoma passa por execute(): idempotencia -> TTL -> estado operacional
(HOLD) -> autorizacao/aprovacao -> optimistic lock (decision_state_version) -> execucao.
A Harpa (approve) reentra por AQUI: nunca executa direto no Ritmo.
Tudo ocorre na transacao da requisicao: ou o negocio + eventos + registro da
operacao confirmam juntos, ou nenhum.
"""
import json

import frappe
from frappe.utils import add_to_date, get_datetime, now, now_datetime

from synapse_core import autonomia, estado, outbox
from synapse_core.config import cfg, cfg_json

OP = "Synapse Operacao"
TERMINAL = ("EXECUTED", "CONCURRENCY_CONFLICT", "CONTEXT_CHANGED", "INTENT_EXPIRED", "UNAUTHORIZED", "FAILED", "REJECTED")
LINEAGE_KEYS = ("event_id", "root_event_id", "chain_id", "causal_depth")


# ------------------------------------------------------------------ handlers (acoes reais no Ritmo)
def _h_criar_venda(o):
    a = o["args"]
    if not a.get("customer") or not a.get("itens"):
        frappe.throw("criar_venda exige customer e itens")
    today = frappe.utils.nowdate()
    doc = frappe.get_doc({
        "doctype": "Sales Order", "customer": a["customer"], "company": a.get("company"),
        "transaction_date": today, "delivery_date": a.get("delivery_date") or today,
        "synapse_op_id": o["operation_id"],
        "items": [{"item_code": i["produto_id"], "qty": i["quantidade"], "rate": i["preco_unitario"],
                   "warehouse": i.get("warehouse"), "delivery_date": a.get("delivery_date") or today}
                  for i in a["itens"]],
    })
    doc.insert()
    return {"doctype": "Sales Order", "name": doc.name}


def _h_confirmar_venda(o):
    doc = frappe.get_doc("Sales Order", o["args"]["id_pedido"])
    doc.submit()
    return {"doctype": "Sales Order", "name": doc.name, "docstatus": doc.docstatus}


def _h_cancelar_venda(o):
    a = o["args"]
    if not a.get("motivo"):
        frappe.throw("cancelar_venda exige motivo (auditoria)")
    doc = frappe.get_doc("Sales Order", a["id_pedido"])
    doc.cancel()
    doc.add_comment("Comment", f"Cancelado via Synapse ({o['operation_id']}): {a['motivo']}")
    return {"doctype": "Sales Order", "name": doc.name, "docstatus": doc.docstatus}


# action -> (handler, doctype, ptype)
HANDLERS = {
    "criar_venda": (_h_criar_venda, "Sales Order", "create"),
    "confirmar_venda": (_h_confirmar_venda, "Sales Order", "submit"),
    "cancelar_venda": (_h_cancelar_venda, "Sales Order", "cancel"),
}


# ------------------------------------------------------------------ util
def _sql_set(op_id, **f):
    if not f:
        return
    for k in ("payload", "result"):
        if k in f and not isinstance(f[k], str) and f[k] is not None:
            f[k] = frappe.as_json(f[k])
    sets = ", ".join(f"`{k}`=%s" for k in f)
    frappe.db.sql(f"UPDATE `tab{OP}` SET {sets}, modified=%s WHERE name=%s", (*f.values(), now(), op_id))


def _load(op_id, for_update=True):
    r = frappe.db.sql(f"SELECT * FROM `tab{OP}` WHERE name=%s" + (" FOR UPDATE" if for_update else ""), op_id, as_dict=True)
    return r[0] if r else None


def normalize(op):
    """Validade da intencao (item 1 da revalidacao)."""
    op = dict(op or {})
    for k in ("operation_id", "action"):
        if not op.get(k):
            frappe.throw(f"operacao invalida: {k} obrigatorio", frappe.ValidationError)
    basis = op.get("basis") or []
    if not basis and op.get("entity_type") and op.get("entity_id") not in (None, ""):
        basis = [{"entity_type": op["entity_type"], "entity_id": op["entity_id"],
                  "decision_state_version": op.get("decision_state_version")}]
    if not basis:
        frappe.throw("operacao invalida: decision_state_version/entidade obrigatorios", frappe.ValidationError)
    for b in basis:
        if b.get("decision_state_version") is None or not b.get("entity_type") or b.get("entity_id") in (None, ""):
            frappe.throw("operacao invalida: cada base exige entity_type, entity_id e decision_state_version", frappe.ValidationError)
    op["basis"] = sorted(basis, key=lambda b: (b["entity_type"], str(b["entity_id"])))
    lin = op.get("lineage")
    if lin and any(k not in lin for k in LINEAGE_KEYS):
        frappe.throw("operacao invalida: lineage incompleta", frappe.ValidationError)
    op.setdefault("args", {})
    op["origin_actor"] = op.get("origin_actor") or "LYRA"
    created = get_datetime(op["intent_created_at"]) if op.get("intent_created_at") else now_datetime()
    default_ttl = cfg("aprovacao_ttl_segundos") if op["action"] in cfg_json("acoes_exigem_aprovacao") else cfg("hold_ttl_padrao_segundos")
    ttl = int(op.get("intent_ttl") or default_ttl)
    op["intent_created_at"], op["intent_ttl"] = created, ttl
    op["intent_expires_at"] = add_to_date(created, seconds=ttl, as_datetime=True)
    return op


def _op_from_row(r):
    p = json.loads(r["payload"]) if r.get("payload") else {}
    p.update(operation_id=r["operation_id"], action=r["action"], origin_actor=r["origin_actor"],
             intent_created_at=r["intent_created_at"], intent_ttl=r["intent_ttl"],
             intent_expires_at=r["intent_expires_at"])
    return p


def _emit(o, event_type, payload, actor="SYSTEM"):
    b = o["basis"][0]
    # bump=False: eventos do CICLO DE VIDA da operacao (held/expired/conflict/approval...) nao alteram o
    # estado de negocio; se incrementassem state_version invalidariam a propria decisao. Mudancas reais de
    # negocio geram seus eventos (e versoes) pelos hooks do Outbox.
    outbox.emit(b["entity_type"], b["entity_id"], event_type, payload, origin_actor=actor,
                origin_operation_id=o["operation_id"], parent=o.get("lineage"), bump=False)


def _finish(o, status, result=None, error=None, event=None, event_payload=None):
    f = {"status": status}
    if result is not None:
        f["result"] = result
    if error:
        f["error"] = str(error)[:1000]
    if status == "EXECUTED":
        f["executed_at"] = now()
    _sql_set(o["operation_id"], **f)
    if event:
        _emit(o, event, {"operation_id": o["operation_id"], "action": o["action"],
                         "conversa_id": (o.get("args") or {}).get("conversa_id"), **(event_payload or {})})
    return {"status": status.lower(), "operation_id": o["operation_id"], **(result or {}), **({"error": error} if error else {})}


def _current_context(o):
    ctx = []
    for b in o["basis"]:
        ctx.append({"entity_type": b["entity_type"], "entity_id": str(b["entity_id"]),
                    "expected_state_version": b["decision_state_version"],
                    "current_state_version": estado.current(b["entity_type"], b["entity_id"]),
                    "snapshot": autonomia.authority_snapshot(b["entity_type"], b["entity_id"])})
    return ctx


# ------------------------------------------------------------------ entrada
def execute(op):
    """Ponto unico de execucao de mutacao autonoma."""
    o = normalize(op)
    # 5. idempotencia: unicidade ENFORCADA pelo indice unico (name = operation_id)
    try:
        now_s = now()
        frappe.db.sql(
            f"""INSERT INTO `tab{OP}` (name, creation, modified, modified_by, owner, docstatus, idx, operation_id, action, status,
                 entity_type, entity_id, decision_state_version, origin_actor, requested_by, intent_created_at, intent_ttl,
                 intent_expires_at, payload)
                VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,'RECEIVED',%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (o["operation_id"], now_s, now_s, frappe.session.user, frappe.session.user, o["operation_id"], o["action"],
             o["basis"][0]["entity_type"], str(o["basis"][0]["entity_id"]), o["basis"][0]["decision_state_version"],
             o["origin_actor"], frappe.session.user, o["intent_created_at"], o["intent_ttl"], o["intent_expires_at"],
             frappe.as_json({k: o[k] for k in ("args", "basis", "lineage") if k in o})))
    except Exception as e:
        if not frappe.db.is_duplicate_entry(e):
            raise
        # Duplicata: quem inseriu antes ja COMMITOU (insert + resultado final estao na mesma transacao).
        # Libera o lock compartilhado do indice duplicado ANTES de reler; senao N requisicoes identicas
        # seguram S e todas pedem X na mesma linha -> deadlock 1213 (reproduzido em teste real).
        frappe.db.rollback()
        existing = _load(o["operation_id"], for_update=False)
        return _replay(existing)
    return _run(o, mode="new")


def _replay(r):
    res = json.loads(r["result"]) if r.get("result") else {}
    return {"status": r["status"].lower(), "operation_id": r["operation_id"], "replayed": True, **res}


def _run(o, mode):
    row = _load(o["operation_id"])
    # 2. validade temporal - NENHUMA revalidacao de estoque/versao ressuscita intencao expirada
    if now_datetime() > get_datetime(o["intent_expires_at"]):
        return _finish(o, "INTENT_EXPIRED", event="intent_expired",
                       event_payload={"intent_expires_at": str(o["intent_expires_at"]), "mode": mode,
                                      "acao_requerida": "reconfirmar intencao do cliente"})
    # 3. estado operacional atual
    if autonomia.state_for_execution() == "RECONCILING":
        _sql_set(o["operation_id"], status="HOLD", hold_created_at=row["hold_created_at"] or now())
        _emit(o, "operation_held", {"operation_id": o["operation_id"], "action": o["action"]})
        return {"status": "hold", "operation_id": o["operation_id"], "intent_expires_at": str(o["intent_expires_at"])}
    # 6. autorizacao / aprovacao humana
    if o["action"] not in cfg_json("acoes_autonomas_permitidas") or o["action"] not in HANDLERS:
        return _finish(o, "UNAUTHORIZED", error=f"acao nao permitida: {o['action']}", event="operation_unauthorized")
    approved = bool(row.get("approved_by"))
    if o["action"] in cfg_json("acoes_exigem_aprovacao") and o["origin_actor"] != "HUMAN" and not approved:
        _sql_set(o["operation_id"], status="AWAITING_APPROVAL", approval_required=1)
        _emit(o, "approval_requested", {"operation_id": o["operation_id"], "action": o["action"], "args": o["args"],
                                        "conversa_id": (o.get("args") or {}).get("conversa_id")})
        return {"status": "awaiting_approval", "operation_id": o["operation_id"], "intent_expires_at": str(o["intent_expires_at"])}
    handler, doctype, ptype = HANDLERS[o["action"]]
    if not frappe.has_permission(doctype, ptype):
        return _finish(o, "UNAUTHORIZED", error=f"sem permissao {ptype} em {doctype}", event="operation_unauthorized")
    # 4. optimistic lock: ordem de locks stream -> entidades (ordenadas)
    estado.lock_stream()
    for b in o["basis"]:
        cur = estado.current(b["entity_type"], b["entity_id"], for_update=True)
        if cur != int(b["decision_state_version"]):
            changed = mode == "approve" or approved
            status = "CONTEXT_CHANGED" if changed else "CONCURRENCY_CONFLICT"
            ctx = _current_context(o)
            return _finish(o, status, result={"current_context": ctx}, event=status.lower(),
                           event_payload={"expected": b["decision_state_version"], "actual": cur, "current_context": ctx})
    # 7. execucao (savepoint: falha no meio nao deixa negocio parcial; o FAILED persiste)
    frappe.db.savepoint("synapse_op")
    frappe.flags.synapse_ctx = {"origin_actor": o["origin_actor"], "origin_operation_id": o["operation_id"],
                                "parent": o.get("lineage")}
    try:
        result = handler(o)
    except Exception as e:
        frappe.db.rollback(save_point="synapse_op")
        frappe.flags.synapse_ctx = None
        return _finish(o, "FAILED", error=f"{type(e).__name__}: {e}", event="operation_failed")
    frappe.flags.synapse_ctx = None
    return _finish(o, "EXECUTED", result={"result": result})


# ------------------------------------------------------------------ HOLD / Harpa
def release_holds():
    """Chamado SO por autonomia.declare_consistent(). Cada HOLD passa de novo por TODO o pipeline."""
    out = []
    ids = [r[0] for r in frappe.db.sql(f"SELECT name FROM `tab{OP}` WHERE status='HOLD' ORDER BY hold_created_at, creation FOR UPDATE")]
    for oid in ids:
        r = _load(oid)
        res = _run(_op_from_row(r), mode="release")
        out.append({"operation_id": oid, "status": res["status"]})
    return out


def require_approver():
    """Autorizacao unica para approve() E reject(): identidade real do Frappe (sessao ou API key do operador)."""
    role = cfg("papel_aprovador")
    roles = frappe.get_roles()
    if frappe.session.user == "Guest" or (role not in roles and "System Manager" not in roles):
        frappe.throw(f"apenas o papel {role} pode aprovar ou rejeitar", frappe.PermissionError)


def approve(operation_id):
    """Aprovacao humana (Harpa). Reentra na camada de execucao; nunca executa direto."""
    require_approver()
    r = _load(operation_id)
    if not r:
        frappe.throw(f"operacao inexistente: {operation_id}", frappe.DoesNotExistError)
    if r["status"] != "AWAITING_APPROVAL":
        return {"status": r["status"].lower(), "operation_id": operation_id, "note": "operacao nao aguarda aprovacao", "replayed": True}
    _sql_set(operation_id, approved_by=frappe.session.user, approved_at=now())
    return _run(_op_from_row(_load(operation_id)), mode="approve")


def reject(operation_id, reason=None):
    require_approver()          # sem permissao => recusa e a operacao permanece intacta
    r = _load(operation_id)
    if not r or r["status"] != "AWAITING_APPROVAL":
        return {"status": (r or {}).get("status", "missing").lower(), "operation_id": operation_id}
    o = _op_from_row(r)
    _sql_set(operation_id, approved_by=frappe.session.user, approved_at=now())   # quem decidiu (auditoria)
    return _finish(o, "REJECTED", error=reason or "rejeitada pelo operador", event="approval_rejected")


def pending_approvals():
    """Fila de aprovacao do Painel: as PROPRIAS operacoes do Ritmo (nao ha copia em Harmonia/Harpa)."""
    require_approver()
    rows = frappe.db.sql(
        f"""SELECT operation_id, action, entity_type, entity_id, decision_state_version, intent_created_at, intent_expires_at,
                   payload, requested_by FROM `tab{OP}` WHERE status='AWAITING_APPROVAL' ORDER BY intent_created_at""", as_dict=True)
    now_ = now_datetime()
    out = []
    for r in rows:
        p = json.loads(r["payload"]) if r.get("payload") else {}
        out.append({"operation_id": r["operation_id"], "action": r["action"], "args": p.get("args", {}),
                    "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                    "decision_state_version": r["decision_state_version"],
                    "current_state_version": estado.current(r["entity_type"], r["entity_id"]),
                    "intent_expires_at": str(r["intent_expires_at"]),
                    "expired": now_ > get_datetime(r["intent_expires_at"])})
    return out
