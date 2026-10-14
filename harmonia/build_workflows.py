"""Gera os workflows n8n da Cancao. A logica JS vem de cancao_logic.js (mesma fonte dos testes)."""
import json, re
LOGIC = open("cancao_logic.js").read()
LOGIC_INLINE = LOGIC.replace("module.exports = { validate, gate, aggKey, GATE_POLICY, SUPPORTED_SCHEMAS, REQUIRED };", "")

AUTH = {"name": "Authorization", "value": "=token {{$env.ERPNEXT_API_KEY}}:{{$env.ERPNEXT_API_SECRET}}"}
CT = {"name": "Content-Type", "value": "application/json"}
LYRA_H = [CT, {"name": "Authorization", "value": "=Bearer {{$env.LYRA_API_KEY}}"}, {"name": "X-Tenant-ID", "value": "={{$env.LYRA_TENANT_ID}}"}]

_i = [0]
def node(name, typ, ver, params, x, y, **kw):
    _i[0] += 1
    n = {"parameters": params, "id": f"n{_i[0]:02d}", "name": name, "type": f"n8n-nodes-base.{typ}", "typeVersion": ver, "position": [x, y]}
    n.update(kw); return n

def ritmo(name, method, body_expr, x, y, **kw):
    return node(name, "httpRequest", 4, {
        "url": f"=https://{{{{$env.ERPNEXT_DOMAIN}}}}/api/method/synapse_core.api.{method}", "method": "POST",
        "sendHeaders": True, "headerParameters": {"parameters": [AUTH, CT]},
        "sendBody": True, "specifyBody": "json", "jsonBody": body_expr, "options": {}}, x, y, **kw)

def lyra(name, body_expr, x, y):
    return node(name, "httpRequest", 4, {
        "url": "={{$env.LYRA_CENTRAL_URL}}/v1/decide", "method": "POST", "sendHeaders": True,
        "headerParameters": {"parameters": LYRA_H}, "sendBody": True, "specifyBody": "json", "jsonBody": body_expr,
        "options": {"timeout": 60000}}, x, y)

def code(name, js, x, y):
    return node(name, "code", 2, {"jsCode": js}, x, y)

def cond(name, left, x, y):
    return node(name, "if", 1, {"conditions": {"boolean": [{"value1": left, "value2": True}]}}, x, y)

def respond(name, status, x, y):
    return node(name, "respondToWebhook", 1, {"respondWith": "json", "responseBody": "={{$json}}", "options": {"responseCode": status}}, x, y)

def conn(m, a, b, idx=0):
    m.setdefault(a, {"main": []})
    while len(m[a]["main"]) <= idx: m[a]["main"].append([])
    m[a]["main"][idx].append({"node": b, "type": "main", "index": 0})

# ------------------------------------------------------------ 1. Ingestao
N = []; C = {}
N += [
 node("Webhook — Evento do Outbox (Ritmo)", "webhook", 1, {"httpMethod": "POST", "path": "synapse-evento", "responseMode": "responseNode", "options": {}}, 200, 300),
 code("Valida e Classifica (gate determinístico)", LOGIC_INLINE + """
const env = $json.body;
const v = validate(env);
if (!v.ok) return [{ json: { ok: false, reason: v.reason, event_id: env && env.event_id, schema_version: env && env.schema_version } }];
return [{ json: { ok: true, env, gate: gate(env), event_id: env.event_id } }];""", 440, 300),
 cond("Envelope Válido?", "={{$json.ok}}", 680, 300),
 ritmo("Ritmo — Registra Falha (retry → DLQ)", "outbox_failed", '={{JSON.stringify({event_id: $json.event_id, reason: $json.reason, schema_version: $json.schema_version ? String($json.schema_version) : null})}}', 920, 500),
 respond("Responde 422 (será reentregue até a DLQ)", 422, 1160, 500),
 ritmo("Ritmo — ACK", "outbox_ack", '={{JSON.stringify({event_id: $json.event_id})}}', 920, 200),
 code("Interpreta ACK (dedup)", """
const prev = $node['Valida e Classifica (gate determinístico)'].json;
const ack = $input.first().json.message || {};
// duplicado so e descartado se ja concluido; ACK-sem-PROCESSED (ex.: timeout da Lyra) e REPROCESSADO (seguro: operation_id deterministico)
return [{ json: { ...prev, duplicate: !!ack.duplicate && ['PROCESSED','DEAD_LETTER'].includes(ack.status), ack } }];""", 1160, 200),
 cond("Duplicado?", "={{$json.duplicate}}", 1400, 200),
 respond("Responde 200 (duplicado ignorado)", 200, 1640, 100),
 cond("Predecessor pendente?", "={{($json.ack.predecessor_pending || []).length > 0}}", 1640, 200),
 respond("Responde 202 (aguarda predecessor; segue ACK, sera reentregue)", 202, 1880, 100),
 ritmo("Ritmo — Registra Decisão do Gate", "gate_mark", '={{JSON.stringify({event_id: $json.event_id, gate_action: $json.gate.action, agg_key: $json.gate.agg_key})}}', 1640, 300),
 code("Contexto do Evento (pós-gate)", """
// Nós HTTP substituem $json pela resposta do Ritmo: reanexa envelope + decisão do gate para os próximos passos.
return [{ json: { ...$node['Interpreta ACK (dedup)'].json } }];""", 1760, 300),
 cond("Vai para a Lyra?", "={{$json.gate.action === 'FORWARD'}}", 1880, 300),
 lyra("Lyra Central — Decide", "={{JSON.stringify({event: $json.env, trigger: 'event'})}}", 2120, 200),
 code("Monta Operações", """
const prev = $node['Interpreta ACK (dedup)'].json;
const ops = ($input.first().json.operations || []);
if (!ops.length) return [{ json: { ...prev, sem_operacao: true } }];
return ops.map(o => ({ json: { ...prev, operation: o, sem_operacao: false } }));""", 2360, 200),
 cond("Tem Operação?", "={{$json.sem_operacao === false}}", 2600, 200),
 ritmo("Ritmo — Executa (autoridade)", "execute", '={{JSON.stringify({operation: $json.operation})}}', 2840, 100),
 cond("Agregar (aguarda flush)?", "={{$json.gate.action === 'AGGREGATE'}}", 2120, 450),
 ritmo("Ritmo — PROCESSED", "outbox_processed", '={{JSON.stringify({event_ids: [$node["Interpreta ACK (dedup)"].json.event_id]})}}', 3080, 300),
 respond("Responde 200", 200, 3320, 300),
 node("Harmonia — Aciona flush imediato (MAX_EVENTS)", "httpRequest", 4, {"url": "=https://{{$env.HARMONIA_DOMAIN}}/webhook/synapse-flush-agora", "method": "POST", "sendHeaders": True, "headerParameters": {"parameters": [CT]}, "sendBody": True, "specifyBody": "json", "jsonBody": "={{JSON.stringify({agg_key: $json.gate.agg_key})}}", "options": {"timeout": 5000}}, 2360, 450, continueOnFail=True),
 respond("Responde 200 (agregado)", 200, 2600, 450),
 code("Extrai Notificações (Harpa)", """
const ns = ($input.first().json.notifications || []).filter(n => n.conversa_id);
if (!ns.length) return [{ json: { sem_notificacao: true } }];
return ns.map(n => ({ json: n }));""", 2360, 650),
 cond("Há notificação?", "={{$json.sem_notificacao !== true}}", 2600, 650),
 node("Harpa — Envia Notificação (reconfirmação / aprovação)", "httpRequest", 4, {
   "url": "=https://{{$env.CHATWOOT_DOMAIN}}/api/v1/accounts/{{$env.CHATWOOT_ACCOUNT_ID}}/conversations/{{$json.conversa_id}}/messages",
   "method": "POST", "sendHeaders": True,
   "headerParameters": {"parameters": [{"name": "api_access_token", "value": "={{$env.CHATWOOT_API_TOKEN}}"}, CT]},
   "sendBody": True, "specifyBody": "json",
   "jsonBody": "={{JSON.stringify({content: $json.text, message_type: 'outgoing', private: !!$json.private})}}", "options": {}},
   2840, 650, retryOnFail=True, maxTries=3),
]
for a, b, i in [
 ("Webhook — Evento do Outbox (Ritmo)", "Valida e Classifica (gate determinístico)", 0),
 ("Valida e Classifica (gate determinístico)", "Envelope Válido?", 0),
 ("Envelope Válido?", "Ritmo — ACK", 0), ("Envelope Válido?", "Ritmo — Registra Falha (retry → DLQ)", 1),
 ("Ritmo — Registra Falha (retry → DLQ)", "Responde 422 (será reentregue até a DLQ)", 0),
 ("Ritmo — ACK", "Interpreta ACK (dedup)", 0), ("Interpreta ACK (dedup)", "Duplicado?", 0),
 ("Duplicado?", "Responde 200 (duplicado ignorado)", 0), ("Duplicado?", "Predecessor pendente?", 1),
 ("Predecessor pendente?", "Responde 202 (aguarda predecessor; segue ACK, sera reentregue)", 0), ("Predecessor pendente?", "Ritmo — Registra Decisão do Gate", 1),
 ("Ritmo — Registra Decisão do Gate", "Contexto do Evento (pós-gate)", 0), ("Contexto do Evento (pós-gate)", "Vai para a Lyra?", 0),
 ("Vai para a Lyra?", "Lyra Central — Decide", 0), ("Vai para a Lyra?", "Agregar (aguarda flush)?", 1),
 ("Lyra Central — Decide", "Monta Operações", 0), ("Monta Operações", "Tem Operação?", 0),
 ("Lyra Central — Decide", "Extrai Notificações (Harpa)", 0), ("Extrai Notificações (Harpa)", "Há notificação?", 0),
 ("Há notificação?", "Harpa — Envia Notificação (reconfirmação / aprovação)", 0),
 ("Tem Operação?", "Ritmo — Executa (autoridade)", 0), ("Tem Operação?", "Ritmo — PROCESSED", 1),
 ("Ritmo — Executa (autoridade)", "Ritmo — PROCESSED", 0),
 ("Agregar (aguarda flush)?", "Harmonia — Aciona flush imediato (MAX_EVENTS)", 0), ("Harmonia — Aciona flush imediato (MAX_EVENTS)", "Responde 200 (agregado)", 0), ("Agregar (aguarda flush)?", "Ritmo — PROCESSED", 1),
 ("Ritmo — PROCESSED", "Responde 200", 0)]:
    conn(C, a, b, i)
wf1 = {"name": "Synapse — Canção — Ingestão de Eventos (Harmonia)", "nodes": N, "connections": C, "settings": {"executionOrder": "v1"}}

# ------------------------------------------------------------ 2. Flush de agregacao + heartbeat + reconciliacao
_i[0] = 100; N = []; C = {}
N += [
 node("Agendador — a cada 1 min (flush)", "scheduleTrigger", 1, {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}}, 200, 200),
 ritmo("Ritmo — Grupos vencidos (MAX_EVENTS ou MAX_WAIT)", "aggregation_due", "={{JSON.stringify({})}}", 440, 200),
 code("Um item por grupo", """
const due = ($input.first().json.message || []);
if (!due.length) return [{ json: { vazio: true } }];
return due.map(g => ({ json: g }));""", 680, 200),
 cond("Há grupo?", "={{$json.vazio !== true}}", 900, 200),
 ritmo("Ritmo — Flush do grupo", "aggregation_flush", '={{JSON.stringify({agg_key: $json.agg_key})}}', 1120, 100),
 lyra("Lyra Central — Decide (lote agregado)", "={{JSON.stringify({trigger: 'aggregate', events: $json.message.events})}}", 1360, 100),
 code("Monta Operações do Lote", """
const g = $node['Um item por grupo'].json;
const flush = $node['Ritmo — Flush do grupo'].json.message || {};
const ops = ($input.first().json.operations || []);
if (!ops.length) return [{ json: { event_ids: flush.event_ids, sem_operacao: true } }];
return ops.map(o => ({ json: { event_ids: flush.event_ids, operation: o, sem_operacao: false } }));""", 1600, 100),
 cond("Lote tem operação?", "={{$json.sem_operacao === false}}", 1840, 100),
 ritmo("Ritmo — Executa (lote)", "execute", '={{JSON.stringify({operation: $json.operation})}}', 2080, 0),
 ritmo("Ritmo — PROCESSED (lote)", "outbox_processed", '={{JSON.stringify({event_ids: $node["Monta Operações do Lote"].json.event_ids})}}', 2320, 100),
 node("Webhook — Flush imediato (chamado pela ingestão)", "webhook", 1, {"httpMethod": "POST", "path": "synapse-flush-agora", "responseMode": "onReceived", "options": {}}, 200, 0),
 node("Agendador — a cada 1 min (heartbeat)", "scheduleTrigger", 1, {"rule": {"interval": [{"field": "minutes", "minutesInterval": 1}]}}, 200, 600),
 ritmo("Ritmo — Heartbeat (watermark/lacunas/atraso)", "heartbeat", "={{JSON.stringify({})}}", 440, 600),
 cond("Estado é RECONCILING?", "={{$json.message.state === 'RECONCILING'}}", 680, 600),
 ritmo("Ritmo — Reconcilia (replay + autoridade)", "reconcile", "={{JSON.stringify({})}}", 920, 500),
 ritmo("Ritmo — Reenvio de pendentes (replay)", "outbox_replay", "={{JSON.stringify({from_seq: 0})}}", 1160, 500),
 code("Reentrega ao webhook de ingestão", """
const evs = $input.first().json.message || [];
if (!evs.length) return [{ json: { replay: 0 } }];
return evs.map(e => ({ json: e }));""", 1400, 500),
 node("Harmonia — Reingesta evento", "httpRequest", 4, {"url": "=https://{{$env.HARMONIA_DOMAIN}}/webhook/synapse-evento", "method": "POST", "sendHeaders": True, "headerParameters": {"parameters": [CT]}, "sendBody": True, "specifyBody": "json", "jsonBody": "={{JSON.stringify($json)}}", "options": {}}, 1640, 500, continueOnFail=True),
 ritmo("Ritmo — Declara CONSISTENT_STATE (só se comprovado)", "declare_consistent", "={{JSON.stringify({})}}", 1880, 500),
]
for a, b, i in [
 ("Agendador — a cada 1 min (flush)", "Ritmo — Grupos vencidos (MAX_EVENTS ou MAX_WAIT)", 0),
 ("Webhook — Flush imediato (chamado pela ingestão)", "Ritmo — Grupos vencidos (MAX_EVENTS ou MAX_WAIT)", 0),
 ("Ritmo — Grupos vencidos (MAX_EVENTS ou MAX_WAIT)", "Um item por grupo", 0), ("Um item por grupo", "Há grupo?", 0),
 ("Há grupo?", "Ritmo — Flush do grupo", 0), ("Ritmo — Flush do grupo", "Lyra Central — Decide (lote agregado)", 0),
 ("Lyra Central — Decide (lote agregado)", "Monta Operações do Lote", 0), ("Monta Operações do Lote", "Lote tem operação?", 0),
 ("Lote tem operação?", "Ritmo — Executa (lote)", 0), ("Lote tem operação?", "Ritmo — PROCESSED (lote)", 1),
 ("Ritmo — Executa (lote)", "Ritmo — PROCESSED (lote)", 0),
 ("Agendador — a cada 1 min (heartbeat)", "Ritmo — Heartbeat (watermark/lacunas/atraso)", 0),
 ("Ritmo — Heartbeat (watermark/lacunas/atraso)", "Estado é RECONCILING?", 0),
 ("Estado é RECONCILING?", "Ritmo — Reconcilia (replay + autoridade)", 0),
 ("Ritmo — Reconcilia (replay + autoridade)", "Ritmo — Reenvio de pendentes (replay)", 0),
 ("Ritmo — Reenvio de pendentes (replay)", "Reentrega ao webhook de ingestão", 0),
 ("Reentrega ao webhook de ingestão", "Harmonia — Reingesta evento", 0),
 ("Harmonia — Reingesta evento", "Ritmo — Declara CONSISTENT_STATE (só se comprovado)", 0)]:
    conn(C, a, b, i)
wf2 = {"name": "Synapse — Canção — Flush, Heartbeat e Reconciliação (Harmonia)", "nodes": N, "connections": C, "settings": {"executionOrder": "v1"}}

# ------------------------------------------------------------ 3. Harpa: aprovacao humana
_i[0] = 200; N = []; C = {}
N += [
 node("Webhook — Aprovação Humana (Harpa/Painel)", "webhook", 1, {"httpMethod": "POST", "path": "synapse-aprovar", "responseMode": "responseNode", "options": {}}, 200, 200),
 code("Valida pedido e credencial do aprovador", """
const b = $json.body || {};
const cred = $json.headers && $json.headers['x-approver-token'];   // "chave:segredo" do PROPRIO usuario aprovador no Ritmo
if (!b.operation_id || !['approve','reject'].includes(b.decision)) throw new Error('operation_id e decision (approve|reject) sao obrigatorios');
if (!cred) throw new Error('X-Approver-Token ausente: a aprovacao exige a identidade do operador humano');
return [{ json: { operation_id: b.operation_id, decision: b.decision, reason: b.reason || null, cred } }];""", 440, 200),
 node("Ritmo — Aprova/Rejeita (revalida na camada de execução)", "httpRequest", 4, {
   "url": "=https://{{$env.ERPNEXT_DOMAIN}}/api/method/synapse_core.api.{{$json.decision}}", "method": "POST", "sendHeaders": True,
   "headerParameters": {"parameters": [{"name": "Authorization", "value": "=token {{$json.cred}}"}, CT]},
   "sendBody": True, "specifyBody": "json", "jsonBody": "={{JSON.stringify({operation_id: $json.operation_id, reason: $json.reason})}}", "options": {}}, 680, 200, continueOnFail=True),
 code("Formata resposta ao operador", """
const r = $input.first().json;
const m = r.message || {};
const s = m.status;
const msg = s === 'context_changed' ? 'Aprovação obsoleta: o contexto mudou. Nada foi executado. Revise e solicite nova decisão.'
  : s === 'intent_expired' ? 'Aprovação expirada (TTL). Nada foi executado.'
  : s === 'executed' ? 'Aprovado e executado.' : (s === 'rejected' ? 'Rejeitada.' : 'Estado: ' + s);
return [{ json: { status: s, mensagem: msg, current_context: m.current_context || null, erro: r.exc ? 'permissao ou erro no Ritmo' : null } }];""", 920, 200),
 respond("Responde ao operador", 200, 1160, 200),
]
for a, b in [("Webhook — Aprovação Humana (Harpa/Painel)", "Valida pedido e credencial do aprovador"),
             ("Valida pedido e credencial do aprovador", "Ritmo — Aprova/Rejeita (revalida na camada de execução)"),
             ("Ritmo — Aprova/Rejeita (revalida na camada de execução)", "Formata resposta ao operador"),
             ("Formata resposta ao operador", "Responde ao operador")]:
    conn(C, a, b)
wf3 = {"name": "Synapse — Canção — Aprovação Humana (Harpa)", "nodes": N, "connections": C, "settings": {"executionOrder": "v1"}}

import os
os.makedirs("workflows", exist_ok=True)
for fn, wf in [("cancao_ingestao.json", wf1), ("cancao_flush_heartbeat.json", wf2), ("cancao_aprovacao_harpa.json", wf3)]:
    json.dump(wf, open("workflows/" + fn, "w"), indent=1, ensure_ascii=False)
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "nomes duplicados em " + fn
    for a, v in wf["connections"].items():
        assert a in names, a
        for outs in v["main"]:
            for c in outs: assert c["node"] in names, c
    print(fn, len(names), "nos ok")
