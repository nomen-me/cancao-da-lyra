import json,urllib.request,time,sys
B="http://127.0.0.1:3055"
env=dict(l.strip().split('=',1) for l in open('/root/.semaphore_synapse/integracao_n8n.env') if '=' in l)
tok=[l.split('=',1)[1].strip() for l in open('/root/.semaphore_synapse/credenciais.env') if l.startswith('SEMAPHORE_API_TOKEN=')][0]
def api(p):
    return json.loads(urllib.request.urlopen(urllib.request.Request(B+p,headers={"Authorization":"Bearer "+tok})).read())
def trigger(path,hdr,body):
    r=urllib.request.Request(B+path,data=json.dumps(body).encode(),method="POST",headers={"Content-Type":"application/json",**hdr})
    try: x=urllib.request.urlopen(r); return x.status
    except urllib.error.HTTPError as e: return e.code
P=api("/api/projects")[0]["id"]
def tasks(): return api(f"/api/project/{P}/tasks")
def espera(n0):
    for _ in range(60):
        ts=tasks()
        if len(ts)>n0 and ts[0]["status"] in ("success","error"): return ts[0]
        time.sleep(1)
    return None
corpo={"vps_ip":"203.0.113.10","dominio":"lojademo.com.br","nome_loja":"Loja Demo"}
H=env["SEMAPHORE_INTEGRACAO_HEADER"]; V=env["SEMAPHORE_INTEGRACAO_VALIDAR_PATH"]
n0=len(tasks()); print("token ERRADO ->",trigger(V,{H:"errado"},corpo),"| tasks criadas:",len(tasks())-n0)
n0=len(tasks()); print("token CERTO  ->",trigger(V,{H:env["SEMAPHORE_INTEGRACAO_TOKEN"]},corpo))
t=espera(n0); print("task:",t and (t["id"],t["status"],t["template_id"]))
for l in api(f"/api/project/{P}/tasks/{t['id']}/output"):
    o=l.get("output","")
    if "INTEGRACAO_OK" in o: print("  >",o.replace("\x1b[0;32m","").replace("\x1b[0m","").strip())
print("--- contrato violado (dominio invalido) deve FALHAR no playbook, nao provisionar:")
n0=len(tasks()); trigger(V,{H:env["SEMAPHORE_INTEGRACAO_TOKEN"]},{**corpo,"dominio":"semponto"})
t=espera(n0); print("task:",t and (t["id"],t["status"]))
