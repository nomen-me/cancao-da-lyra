import json,urllib.request,glob,os
B="http://127.0.0.1:5678/api/v1"; key=open('/tmp/n8n_api_key.txt').read().strip()
def call(m,p,body=None):
    r=urllib.request.Request(B+p,data=(json.dumps(body).encode() if body is not None else None),method=m,headers={"X-N8N-API-KEY":key,"Content-Type":"application/json"})
    try: x=urllib.request.urlopen(r,timeout=90); d=x.read(); return x.status,(json.loads(d) if d else None)
    except urllib.error.HTTPError as e: return e.code,e.read().decode()[:400]
for w in call("GET","/workflows")[1]["data"]:
    call("POST",f"/workflows/{w['id']}/deactivate"); call("DELETE",f"/workflows/{w['id']}")
dirs=['/home/claude/work/json/x/Json','/home/claude/work/json_corrigido','/home/claude/work/synapse-impl/harmonia/workflows']
files={}
for d in dirs:
    for f in glob.glob(d+'/*.json'): files[os.path.basename(f)]=f     # corrigidos/novos sobrepoem
res=[]
for name,f in sorted(files.items()):
    wf=json.load(open(f))
    if 'nodes' not in wf: continue
    wf.setdefault("settings",{}); s,c=call("POST","/workflows",{k:wf[k] for k in ("name","nodes","connections","settings")})
    if s>=300: res.append((name,"CRIAR",s,str(c)[:160])); continue
    s2,a=call("POST",f"/workflows/{c['id']}/activate")
    res.append((name,"ATIVO" if s2==200 and a.get("active") else "ATIVAR-FALHOU",s2,"" if s2==200 else str(a)[:200]))
for r in res: print(r[0].ljust(36),r[1],r[2],r[3])
print("total",len(res),"ativos",sum(1 for r in res if r[1]=="ATIVO"))
