#!/bin/bash
W=/home/claude/work; E=$W/e2e; cd $E
setsid nohup python3 tls_proxy.py 5690 5678 > /dev/null 2>&1 < /dev/null &
(
  cd $W/n8n_real; . ./n8n_env.sh; export PATH=$W/node24/node_modules/node/bin:$PATH
  export NODE_TLS_REJECT_UNAUTHORIZED=0
  export ERPNEXT_DOMAIN=localhost:8443 HARMONIA_DOMAIN=localhost:5690 CHATWOOT_DOMAIN=localhost:5680 CHATWOOT_ACCOUNT_ID=1 CHATWOOT_API_TOKEN=token-chatwoot-teste
  export ERPNEXT_API_KEY=503ee1ae7d3efc6 ERPNEXT_API_SECRET=95704671da1ea39
  export LYRA_CENTRAL_URL=http://127.0.0.1:18080 LYRA_API_KEY=chave-teste LYRA_TENANT_ID=tenant-teste
  setsid nohup node node_modules/n8n/bin/n8n start > $E/n8n.log 2>&1 < /dev/null &
)
echo iniciado
