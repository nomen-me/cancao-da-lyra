#!/bin/bash
W=/home/claude/work; E=$W/e2e; cd $E
$W/env_up.sh >/dev/null 2>&1
mkdir -p $W/lyra/lyra-central-api/data
node -e "const c=require('crypto');require('fs').writeFileSync('$W/lyra/lyra-central-api/data/tenants.dev.json',JSON.stringify({'tenant-teste':c.createHash('sha256').update('chave-teste').digest('hex')}))"
alive(){ python3 $E/killproc.py "$1" >/dev/null 2>&1; }
curl -s -m2 http://127.0.0.1:8011/api/method/ping | grep -q pong || su frappe -c "cd $W/frappe-bench && (setsid nohup bench --site synapse.test serve --port 8011 > /tmp/ritmo_serve.log 2>&1 < /dev/null &)"
curl -s -m2 -o /dev/null http://127.0.0.1:18080/ || (cd $W/lyra/lyra-central-api && GEMINI_MODEL=gemini-teste GEMINI_API_KEY=chave-invalida-de-teste ADMIN_API_KEY=admin-teste REDIS_URL=redis://127.0.0.1:13000 PORT=18080 LOG_LEVEL=info setsid nohup node src/index.js > /tmp/lyra_e2e.log 2>&1 < /dev/null &)
curl -sk -m2 https://localhost:8443/api/method/ping | grep -q pong || (setsid nohup python3 tls_proxy.py 8443 8011 > /dev/null 2>&1 < /dev/null &)
curl -sk -m2 -o /dev/null https://localhost:5680/ || { (setsid nohup python3 chatwoot_recorder.py > /dev/null 2>&1 < /dev/null &); (setsid nohup python3 tls_proxy.py 5680 5681 > /dev/null 2>&1 < /dev/null &); }
curl -s -m2 http://127.0.0.1:5678/healthz | grep -q ok || ./start_rest.sh >/dev/null
for i in $(seq 1 40); do curl -s -m2 http://127.0.0.1:5678/healthz | grep -q ok && curl -s -m2 http://127.0.0.1:8011/api/method/ping | grep -q pong && break; sleep 3; done
echo "pilha: n8n=$(curl -s -m3 http://127.0.0.1:5678/healthz) ritmo=$(curl -s -m3 http://127.0.0.1:8011/api/method/ping) tls-ritmo=$(curl -sk -m3 https://localhost:8443/api/method/ping) tls-n8n=$(curl -sk -m3 https://localhost:5690/healthz) lyra=$(curl -s -m3 -o /dev/null -w %{http_code} -X POST http://127.0.0.1:18080/v1/decide)"
