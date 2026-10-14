"""Gera o workflow n8n 'Synapse - Provisionar Cliente (Semaphore)'. Logica JS vem de provisionar_logic.js."""
import json, os
LOGIC = open("provisionar_logic.js").read().replace("module.exports = { autenticar, validarContrato, OPERACOES };", "")
CT = {"name": "Content-Type", "value": "application/json"}
def node(i, name, typ, ver, params, x, y, **kw):
    n = {"parameters": params, "id": f"p{i:02d}", "name": name, "type": f"n8n-nodes-base.{typ}", "typeVersion": ver, "position": [x, y]}; n.update(kw); return n
N = [
 node(1, "Webhook — Provisionar/Validar Cliente", "webhook", 1, {"httpMethod": "POST", "path": "synapse-provisionar-cliente", "responseMode": "responseNode", "options": {}}, 200, 300),
 node(2, "Autentica e Valida o Contrato", "code", 2, {"jsCode": LOGIC + """
const esperado = $env.SYNAPSE_PROVISION_TOKEN;
const a = autenticar($json.headers, esperado);
if (!a.ok) return [{ json: { ok: false, status: a.status, reason: a.reason } }];
const v = validarContrato($json.body);
if (!v.ok) return [{ json: { ok: false, status: v.status, reason: v.reason } }];
const path = v.operacao === 'provisionar' ? $env.SEMAPHORE_INTEGRACAO_PROVISIONAR_PATH : $env.SEMAPHORE_INTEGRACAO_VALIDAR_PATH;
if (!path || !$env.SEMAPHORE_INTEGRACAO_TOKEN || !$env.SEMAPHORE_INTERNAL_URL) return [{ json: { ok: false, status: 500, reason: 'integracao Semaphore nao configurada no Harmonia' } }];
return [{ json: { ok: true, operacao: v.operacao, payload: v.payload, url: $env.SEMAPHORE_INTERNAL_URL + path, header: $env.SEMAPHORE_INTEGRACAO_HEADER || 'X-Synapse-Token' } }];"""}, 440, 300),
 node(3, "Contrato OK?", "if", 1, {"conditions": {"boolean": [{"value1": "={{$json.ok}}", "value2": True}]}}, 680, 300),
 node(4, "Semaphore — Dispara Integration (API oficial)", "httpRequest", 4, {
   "url": "={{$json.url}}", "method": "POST", "sendHeaders": True,
   "specifyHeaders": "json", "jsonHeaders": "={{JSON.stringify({'Content-Type':'application/json', [$json.header]: $env.SEMAPHORE_INTEGRACAO_TOKEN})}}",
   "sendBody": True, "specifyBody": "json", "jsonBody": "={{JSON.stringify($json.payload)}}",
   "options": {"timeout": 30000, "response": {"response": {"neverError": True, "fullResponse": True}}}}, 920, 200),
 node(5, "Resposta 202 (task enfileirada no Semaphore)", "code", 2, {"jsCode": """
const prev = $node['Autentica e Valida o Contrato'].json;
const st = $input.first().json.statusCode;
if (st < 200 || st >= 300) throw new Error('Semaphore recusou a Integration (HTTP ' + st + ')');
return [{ json: { accepted: true, operacao: prev.operacao, dominio: prev.payload.dominio, vps_ip: prev.payload.vps_ip } }];"""}, 1160, 200),
 node(6, "Responde 202", "respondToWebhook", 1, {"respondWith": "json", "responseBody": "={{$json}}", "options": {"responseCode": 202}}, 1400, 200),
 node(7, "Status de erro (401/400/500)", "code", 2, {"jsCode": "return [{ json: { accepted: false, status: $json.status, reason: $json.reason } }];"}, 920, 420),
 node(8, "Erro é autenticação?", "if", 1, {"conditions": {"number": [{"value1": "={{$json.status}}", "operation": "equal", "value2": 401}]}}, 1160, 420),
 node(9, "Responde 401", "respondToWebhook", 1, {"respondWith": "json", "responseBody": "={{$json}}", "options": {"responseCode": 401}}, 1400, 340),
 node(10, "Responde 400", "respondToWebhook", 1, {"respondWith": "json", "responseBody": "={{$json}}", "options": {"responseCode": 400}}, 1400, 500),
]
def link(a, b, i=0): C.setdefault(a, {"main": []}); [C[a]["main"].append([]) for _ in range(i + 1 - len(C[a]["main"]))]; C[a]["main"][i].append({"node": b, "type": "main", "index": 0})
C = {}
for a, b, i in [("Webhook — Provisionar/Validar Cliente", "Autentica e Valida o Contrato", 0), ("Autentica e Valida o Contrato", "Contrato OK?", 0),
                ("Contrato OK?", "Semaphore — Dispara Integration (API oficial)", 0), ("Contrato OK?", "Status de erro (401/400/500)", 1),
                ("Semaphore — Dispara Integration (API oficial)", "Resposta 202 (task enfileirada no Semaphore)", 0), ("Resposta 202 (task enfileirada no Semaphore)", "Responde 202", 0),
                ("Status de erro (401/400/500)", "Erro é autenticação?", 0), ("Erro é autenticação?", "Responde 401", 0), ("Erro é autenticação?", "Responde 400", 1)]:
    link(a, b, i)
wf = {"name": "Synapse — Provisionar Cliente (Semaphore)", "nodes": N, "connections": C, "settings": {"executionOrder": "v1"}}
names = [n["name"] for n in N]; assert len(names) == len(set(names))
for a, v in C.items():
    assert a in names
    for outs in v["main"]:
        for c in outs: assert c["node"] in names
os.makedirs("workflows", exist_ok=True); json.dump(wf, open("workflows/synapse_provisionar_cliente.json", "w"), indent=1, ensure_ascii=False)
print("synapse_provisionar_cliente.json", len(names), "nos ok")
