import json,urllib.request,time,re
W="http://127.0.0.1:5678/webhook/synapse-provisionar-cliente"
TOK="token-de-provisionamento-teste-123"
tok=[l.split('=',1)[1].strip() for l in open('/root/.semaphore_synapse/credenciais.env') if l.startswith('SEMAPHORE_API_TOKEN=')][0]
def sem(p): return json.loads(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:3055"+p,headers={"Authorization":"Bearer "+tok})).read())
def post(h,b):
    r=urllib.request.Request(W,data=json.dumps(b).encode(),method="POST",headers={"Content-Type":"application/json",**h})
    try: x=urllib.request.urlopen(r,timeout=60); return x.status,x.read().decode()[:160]
    except urllib.error.HTTPError as e: return e.code,e.read().decode()[:160]
ok={"operacao":"validar","vps_ip":"203.0.113.10","dominio":"lojademo.com.br","nome_loja":"Loja Demo"}
n0=len(sem("/api/project/1/tasks"))
print("1) sem token na Harmonia      ->",post({},ok))
print("2) token errado               ->",post({"X-Synapse-Provision-Token":"x"},ok))
print("3) dominio invalido           ->",post({"X-Synapse-Provision-Token":TOK},{**ok,"dominio":"semponto"}))
print("4) operacao invalida          ->",post({"X-Synapse-Provision-Token":TOK},{**ok,"operacao":"apagar"}))
print("   tasks criadas ate aqui:",len(sem("/api/project/1/tasks"))-n0,"(esperado 0)")
print("5) contrato valido (validar)  ->",post({"X-Synapse-Provision-Token":TOK},ok))
for _ in range(60):
    ts=sem("/api/project/1/tasks")
    if len(ts)>n0 and ts[0]["status"] in ("success","error"): break
    time.sleep(1)
t=ts[0]; print("   task no Semaphore:",t["id"],t["status"],"template",t["template_id"],"(4 = Validar Integracao)")
for l in sem(f"/api/project/1/tasks/{t['id']}/output"):
    o=re.sub(r"\x1b\[[0-9;]*m","",l.get("output","")).strip()
    if "INTEGRACAO_OK" in o: print("   >",o)
