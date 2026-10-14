#!/usr/bin/env bash
# Testes A-J da Ordem Final contra a infraestrutura REAL (Ritmo + Harmonia + Lyra Central + Harpa).
# Uso:  source ./vps.env && ./run_vps_tests.sh prepare | A | B | ... | J | all
# Cada teste grava evidence/<T>.log e imprime "RESULT <T>: PASS|FAIL". NENHUM mock: fatos vem do banco/API reais.
set -uo pipefail
: "${ERPNEXT_DOMAIN:?}"; : "${ERP_API_KEY:?}"; : "${ERP_API_SECRET:?}"; : "${DOMINIO_ERP:?}"; : "${HARMONIA_DOMAIN:?}"
# *_DOMAIN = so o host (ex.: ritmo.exemplo.com); o consumidor prefixa https://. *_URL permite override (testes locais).
ERPNEXT_URL="${ERPNEXT_URL:-https://${ERPNEXT_DOMAIN}}"; HARMONIA_URL="${HARMONIA_URL:-https://${HARMONIA_DOMAIN}}"
N8N_CT="${N8N_CT:-n8n}"; RITMO_CT="${RITMO_CT:-ritmo-backend-1}"
CUST="${TEST_CUSTOMER:-CLI-CANCAO}"; ITEM="${TEST_ITEM:-ITEM-CANCAO}"; COMPANY="${COMPANY:?defina COMPANY}"
APPROVER_TOKEN="${APPROVER_TOKEN:-}"   # "chave:segredo" de um usuario com o papel aprovador (Sales Manager)
EV="$(dirname "$0")/evidence"; mkdir -p "$EV"; AUTH="Authorization: token ${ERP_API_KEY}:${ERP_API_SECRET}"
# Ganchos p/ rodar contra um Ritmo local (sem Docker): SQL_LOCAL="mysql ... -N -e" e EXTRA_H="Host: site"
XH=(); [ -n "${EXTRA_H:-}" ] && XH=(-H "$EXTRA_H")
api()  { curl -sS -m 60 "${XH[@]}" -H "$AUTH" -H 'Content-Type: application/json' -X POST "$ERPNEXT_URL/api/method/synapse_core.api.$1" -d "${2:-{\}}"; }
rest() { curl -sS -m 60 "${XH[@]}" -H "$AUTH" -H 'Content-Type: application/json' -X "$1" "$ERPNEXT_URL/api/resource/$2" ${3:+-d "$3"}; }
sql()  { if [ -n "${SQL_LOCAL:-}" ]; then $SQL_LOCAL "$1" 2>/dev/null; else docker exec -u frappe "$RITMO_CT" bench --site "$DOMINIO_ERP" mariadb -N -e "$1" 2>/dev/null; fi; }
setcfg(){ curl -sS -m 30 "${XH[@]}" -H "$AUTH" -H 'Content-Type: application/json' -X POST "$ERPNEXT_URL/api/method/frappe.client.set_value" -d "{\"doctype\":\"Synapse Autonomia\",\"name\":\"Synapse Autonomia\",\"fieldname\":\"$1\",\"value\":\"$2\"}" >/dev/null; }
state(){ api state | jq -r '.message.estado_operacional'; }
ver()  { sql "SELECT COALESCE((SELECT versao FROM \`tabSynapse Estado\` WHERE name='$1::$2'),0)"; }
wait_for(){ local t=$1; shift; for _ in $(seq 1 "$t"); do eval "$@" && return 0; sleep 5; done; return 1; }
uid()  { echo "vps-$1-$(date +%s%N)"; }
op_json() { # op_json <op_id> <ttl|""> <item_version>
  jq -nc --arg id "$1" --arg ttl "$2" --arg c "$CUST" --arg co "$COMPANY" --arg it "$ITEM" --argjson v "$3" \
   '{operation_id:$id,action:"criar_venda",origin_actor:"LYRA",args:{customer:$c,company:$co,itens:[{produto_id:$it,quantidade:1,preco_unitario:10}]},basis:[{entity_type:"Item",entity_id:$it,decision_state_version:$v}]} + (if $ttl=="" then {} else {intent_ttl:($ttl|tonumber)} end)'; }
touch_item() { rest PUT "Item/$ITEM" "{\"description\":\"vps-test $(date +%s%N)\"}" >/dev/null; }
so_count() { sql "SELECT COUNT(*) FROM \`tabSales Order\` WHERE synapse_op_id='$1'"; }
verdict() { local t=$1 ok=$2; echo "RESULT $t: $([ "$ok" = 1 ] && echo PASS || echo FAIL)" | tee -a "$EV/$t.log"; }
log() { echo "[$(date -Is)] $*" | tee -a "$EV/$T.log"; }
reset_cfg() { setcfg heartbeat_max_lag_segundos 120; setcfg agg_max_events 10; setcfg agg_max_wait_seconds 120; setcfg causal_depth_limit 20; setcfg outbox_retention_days 7; }

prepare() {
  rest POST Customer "{\"customer_name\":\"$CUST\",\"customer_group\":\"Commercial\",\"territory\":\"Rest Of The World\"}" | head -c 200; echo
  rest POST Item "{\"item_code\":\"$ITEM\",\"item_name\":\"$ITEM\",\"item_group\":\"Products\",\"stock_uom\":\"Nos\",\"is_stock_item\":0}" | head -c 200; echo
  api heartbeat | jq -c .message
  echo "Workflows Canção ativos?"; curl -sS -o /dev/null -w "%{http_code}\n" -X POST "$HARMONIA_URL/webhook/synapse-evento" -H 'Content-Type: application/json' -d '{}'
}

# ---------- A: reconciliacao ----------
tA() { T=A; : > "$EV/A.log"; ok=1; reset_cfg; setcfg heartbeat_max_lag_segundos 20
  log "1) interrompe a ponte (Harmonia)"; docker stop "$N8N_CT" >/dev/null
  for _ in 1 2 3; do touch_item; done; sleep 40
  api heartbeat | tee -a "$EV/A.log" | jq -c .message; [ "$(state)" = RECONCILING ] || { log "FALHA: nao entrou em RECONCILING"; ok=0; }
  log "2) leitura informativa continua e marca possivel desatualizacao"
  api entity_state "{\"entity_type\":\"Item\",\"entity_id\":\"$ITEM\"}" | tee -a "$EV/A.log" | jq -e '.message.possibly_stale==true' >/dev/null || { log "FALHA leitura"; ok=0; }
  log "3) mutacao critica entra em HOLD"; OP=$(uid A)
  R=$(api execute "{\"operation\":$(op_json "$OP" 900 "$(ver Item "$ITEM")" | jq -Rs .)}"); echo "$R" >> "$EV/A.log"
  [ "$(echo "$R" | jq -r .message.status)" = hold ] && [ "$(so_count "$OP")" = 0 ] || { log "FALHA: nao ficou em HOLD"; ok=0; }
  log "4) restaura a ponte; a Harmonia reconcilia e SO ENTAO libera"; docker start "$N8N_CT" >/dev/null
  wait_for 72 '[ "$(state)" = HEALTHY ]' || { log "FALHA: nao voltou a HEALTHY em 6 min"; ok=0; }
  sql "SELECT seq, payload FROM \`tabSynapse Outbox\` WHERE event_type='synapse.state_changed' ORDER BY seq DESC LIMIT 6" | tee -a "$EV/A.log"
  grep -q CONSISTENT_STATE "$EV/A.log" || { log "FALHA: sem evento CONSISTENT_STATE"; ok=0; }
  [ "$(so_count "$OP")" = 1 ] || { log "FALHA: HOLD nao executou apos consistencia"; ok=0; }
  reset_cfg; verdict A $ok; }

# ---------- B: HOLD expirado ----------
tB() { T=B; : > "$EV/B.log"; ok=1; reset_cfg; setcfg heartbeat_max_lag_segundos 20
  docker stop "$N8N_CT" >/dev/null; touch_item; sleep 40; api heartbeat >/dev/null
  OP=$(uid B); V=$(ver Item "$ITEM")
  api execute "{\"operation\":$(op_json "$OP" 20 "$V" | jq -Rs .)}" | tee -a "$EV/B.log" | jq -c .message
  sleep 30; log "TTL (20s) ultrapassado; restaurando"; docker start "$N8N_CT" >/dev/null
  wait_for 72 '[ "$(state)" = HEALTHY ]' || { log "FALHA: nao voltou a HEALTHY"; ok=0; }
  [ "$(so_count "$OP")" = 0 ] || { log "FALHA: executou intencao expirada"; ok=0; }
  [ "$(sql "SELECT status FROM \`tabSynapse Operacao\` WHERE name='$OP'")" = INTENT_EXPIRED ] || { log "FALHA: status != INTENT_EXPIRED"; ok=0; }
  log "versao ainda valida? decision=$V atual=$(ver Item "$ITEM") (versao valida NAO ressuscita intencao)"
  sql "SELECT event_id,status,event_type FROM \`tabSynapse Outbox\` WHERE origin_operation_id='$OP'" | tee -a "$EV/B.log"
  log "reconfirmacao: confira no Chatwoot/Harpa a mensagem 'reconfirm_intent' (requer conversa_id nos args da operacao real)"
  reset_cfg; verdict B $ok; }

# ---------- C: concurrency conflict ----------
tC() { T=C; : > "$EV/C.log"; ok=1; reset_cfg; touch_item; V=$(ver Item "$ITEM"); touch_item
  log "decisao em v=$V; estado atual=$(ver Item "$ITEM")"; OP=$(uid C)
  R=$(api execute "{\"operation\":$(op_json "$OP" "" "$V" | jq -Rs .)}"); echo "$R" | tee -a "$EV/C.log" | jq -c .message
  [ "$(echo "$R" | jq -r .message.status)" = concurrency_conflict ] || { log "FALHA: nao rejeitou"; ok=0; }
  [ "$(so_count "$OP")" = 0 ] || { log "FALHA: executou"; ok=0; }
  [ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE origin_operation_id='$OP' AND event_type='concurrency_conflict'")" = 1 ] || { log "FALHA: sem evento"; ok=0; }
  log "Harmonia deve ingerir o evento e a Lyra recalcular (evento vira PROCESSED):"
  wait_for 36 '[ "$(sql "SELECT status FROM \`tabSynapse Outbox\` WHERE origin_operation_id='"'$OP'"' AND event_type='"'concurrency_conflict'"'")" = PROCESSED ]' || { log "FALHA: conflito nao processado pela Harmonia/Lyra"; ok=0; }
  verdict C $ok; }

# ---------- D: outbox transacional (HTTP real) ----------
tD() { T=D; : > "$EV/D.log"; ok=1; D1="VPS-D1-$$"; D2="VPS-D2-$$"
  mk() { echo "{\"doctype\":\"Item\",\"item_code\":\"$1\",\"item_name\":\"x\",\"item_group\":\"Products\",\"stock_uom\":\"$2\"}"; }
  BAD="[$(mk $D1 Nos),$(mk $D2 UOM-INEXISTENTE)]"
  curl -sS -o /dev/null -w "insert_many invalido: http=%{http_code}\n" "${XH[@]}" -H "$AUTH" -H 'Content-Type: application/json' -X POST "$ERPNEXT_URL/api/method/frappe.client.insert_many" -d "{\"docs\":$(echo "$BAD" | jq -Rs .)}" | tee -a "$EV/D.log"
  n=$(sql "SELECT (SELECT COUNT(*) FROM tabItem WHERE name='$D1'),(SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE entity_id='$D1')"); log "apos falha: item/evento = $n"; [ "$n" = "0	0" ] || ok=0
  GOOD="[$(mk $D1 Nos),$(mk $D2 Nos)]"
  curl -sS -o /dev/null -w "insert_many valido: http=%{http_code}\n" "${XH[@]}" -H "$AUTH" -H 'Content-Type: application/json' -X POST "$ERPNEXT_URL/api/method/frappe.client.insert_many" -d "{\"docs\":$(echo "$GOOD" | jq -Rs .)}" | tee -a "$EV/D.log"
  n=$(sql "SELECT (SELECT COUNT(*) FROM tabItem WHERE name='$D1'),(SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE entity_id='$D1')"); log "apos sucesso: item/evento = $n"; [ "$n" = "1	1" ] || ok=0
  sql "SELECT seq,event_id,status,event_type FROM \`tabSynapse Outbox\` WHERE entity_id IN ('$D1','$D2') ORDER BY seq" | tee -a "$EV/D.log"
  verdict D $ok; }

# ---------- E: poison event ----------
tE() { T=E; : > "$EV/E.log"; ok=1; reset_cfg
  docker stop "$N8N_CT" >/dev/null; touch_item
  P=$(sql "SELECT event_id FROM \`tabSynapse Outbox\` WHERE status='PENDING' ORDER BY seq DESC LIMIT 1"); log "evento veneno: $P"
  sql "UPDATE \`tabSynapse Outbox\` SET payload='{corrompido' WHERE name='$P'"; docker start "$N8N_CT" >/dev/null
  wait_for 90 '[ "$(sql "SELECT status FROM \`tabSynapse Outbox\` WHERE name='"'$P'"'")" = DEAD_LETTER ]' || { log "FALHA: nao chegou a DLQ em 7,5 min"; ok=0; }
  sql "SELECT retry_count,first_failure_at,last_failure_at,failure_reason,schema_version,dead_lettered_at FROM \`tabSynapse Outbox\` WHERE name='$P'" | tee -a "$EV/E.log"
  [ "$(sql "SELECT retry_count FROM \`tabSynapse Outbox\` WHERE name='$P'")" = 3 ] || { log "FALHA: retry_count != 3"; ok=0; }
  log "estado apos DLQ: $(state) (esperado RECONCILING, nunca HEALTHY direto)"
  touch_item; wait_for 24 '[ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE event_type=\"item.updated\" AND status IN (\"PROCESSED\") AND seq>(SELECT seq FROM \`tabSynapse Outbox\` WHERE name=\"'$P'\")")" -ge 1 ]' || log "AVISO: evento posterior ainda nao PROCESSED (pode estar em agregacao)"
  wait_for 72 '[ "$(state)" = HEALTHY ]' || { log "FALHA: reconciliacao do impacto nao concluiu"; ok=0; }
  sql "SELECT dlq_impact_resolved FROM \`tabSynapse Outbox\` WHERE name='$P'" | tee -a "$EV/E.log"
  verdict E $ok; }

# ---------- F: purge ----------
tF() { T=F; : > "$EV/F.log"; ok=1
  for _ in 1 2 3; do touch_item; done; wait_for 36 '[ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE status=\"PROCESSED\"")" -ge 3 ]'
  api heartbeat >/dev/null; api reconcile >/dev/null
  ELIG=$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE status='PROCESSED'"); sql "UPDATE \`tabSynapse Outbox\` SET processed_at=DATE_SUB(processed_at, INTERVAL 30 DAY) WHERE status='PROCESSED' AND seq<=(SELECT MAX(seq) FROM (SELECT seq FROM \`tabSynapse Outbox\` WHERE status='PROCESSED' ORDER BY seq LIMIT 2) t)"
  NAO=$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE status<>'PROCESSED'"); log "PROCESSED=$ELIG  nao-processados=$NAO"
  api purge | tee -a "$EV/F.log" | jq -c .message
  NAO2=$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE status<>'PROCESSED'"); [ "$NAO" = "$NAO2" ] || { log "FALHA: purge tocou registros nao-PROCESSED"; ok=0; }
  [ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE status='PROCESSED' AND processed_at < DATE_SUB(NOW(), INTERVAL 7 DAY)")" = 0 ] || { log "FALHA: sobrou PROCESSED antigo elegivel"; ok=0; }
  verdict F $ok; }

# ---------- G: Harpa obsoleta ----------
tG() { T=G; : > "$EV/G.log"; ok=1; [ -n "$APPROVER_TOKEN" ] || { log "defina APPROVER_TOKEN"; verdict G 0; return; }
  OP=$(uid G); R=$(api execute "{\"operation\":$(op_json "$OP" "" "$(ver Item "$ITEM")" | jq -Rs .)}"); SO=$(echo "$R" | jq -r '.message.result.name')
  CO="$OP-conf"; api execute "{\"operation\":$(jq -nc --arg id "$CO" --arg so "$SO" --argjson v "$(ver 'Sales Order' "$SO")" '{operation_id:$id,action:"confirmar_venda",origin_actor:"LYRA",args:{id_pedido:$so},basis:[{entity_type:"Sales Order",entity_id:$so,decision_state_version:$v}]}' | jq -Rs .)}" | jq -c .message | tee -a "$EV/G.log"
  CX="$OP-cancel"; V=$(ver 'Sales Order' "$SO")
  api execute "{\"operation\":$(jq -nc --arg id "$CX" --arg so "$SO" --argjson v "$V" '{operation_id:$id,action:"cancelar_venda",origin_actor:"LYRA",args:{id_pedido:$so,motivo:"teste G"},basis:[{entity_type:"Sales Order",entity_id:$so,decision_state_version:$v}]}' | jq -Rs .)}" | tee -a "$EV/G.log" | jq -c .message
  log "altera o estado do pedido ANTES da aprovacao"; rest PUT "Sales Order/$SO" "{\"delivery_date\":\"$(date -d '+20 days' +%F)\"}" >/dev/null
  log "aprova pela Harpa (webhook real da Harmonia)"
  curl -sS -m 60 -H 'Content-Type: application/json' -H "X-Approver-Token: $APPROVER_TOKEN" -X POST "$HARMONIA_URL/webhook/synapse-aprovar" -d "{\"operation_id\":\"$CX\",\"decision\":\"approve\"}" | tee -a "$EV/G.log" | jq -c .
  [ "$(sql "SELECT status FROM \`tabSynapse Operacao\` WHERE name='$CX'")" = CONTEXT_CHANGED ] || { log "FALHA: nao rejeitou por contexto alterado"; ok=0; }
  [ "$(sql "SELECT docstatus FROM \`tabSales Order\` WHERE name='$SO'")" = 1 ] || { log "FALHA: pedido foi cancelado"; ok=0; }
  verdict G $ok; }

# ---------- H: profundidade causal ----------
tH() { T=H; : > "$EV/H.log"; ok=1; setcfg causal_depth_limit 3
  PARENT=""; for i in 1 2 3 4 5 6 7; do
    LIN=$(sql "SELECT JSON_OBJECT('event_id',event_id,'root_event_id',root_event_id,'chain_id',chain_id,'causal_depth',causal_depth) FROM \`tabSynapse Outbox\` ORDER BY seq DESC LIMIT 1")
    OP=$(uid H$i); jq -nc --argjson l "${LIN:-null}" --arg id "$OP" --arg c "$CUST" --arg co "$COMPANY" --arg it "$ITEM" --argjson v "$(ver Item "$ITEM")" '{operation_id:$id,action:"criar_venda",origin_actor:"LYRA",args:{customer:$c,company:$co,itens:[{produto_id:$it,quantidade:1,preco_unitario:10}]},basis:[{entity_type:"Item",entity_id:$it,decision_state_version:$v}]} + (if $l then {lineage:$l} else {} end)' > /tmp/h_op.json
    api execute "{\"operation\":$(jq -Rs . < /tmp/h_op.json)}" | jq -c .message.status >> "$EV/H.log"; done
  sql "SELECT causal_depth,chain_id,previous_chain_id,LENGTH(causal_summary),LENGTH(payload) FROM \`tabSynapse Outbox\` WHERE entity_type='Sales Order' AND origin_actor='LYRA' ORDER BY seq" | tee -a "$EV/H.log"
  [ "$(sql "SELECT MAX(causal_depth) FROM \`tabSynapse Outbox\` WHERE origin_actor='LYRA' AND origin_operation_id LIKE 'vps-H%'")" -le 3 ] || { log "FALHA: profundidade > limite"; ok=0; }
  [ "$(sql "SELECT COUNT(DISTINCT chain_id) FROM \`tabSynapse Outbox\` WHERE origin_operation_id LIKE 'vps-H%'")" -ge 2 ] || { log "FALHA: cadeia nao rolou"; ok=0; }
  [ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE previous_chain_id IS NOT NULL AND causal_summary IS NOT NULL AND origin_operation_id LIKE 'vps-H%'")" -ge 1 ] || { log "FALHA: sem resumo/referencia a cadeia anterior"; ok=0; }
  reset_cfg; verdict H $ok; }

# ---------- I: agregacao (Harmonia real) ----------
tI() { T=I; : > "$EV/I.log"; ok=1; reset_cfg; setcfg agg_max_wait_seconds 60; setcfg agg_max_events 10
  log "I.1: 3 eventos (< MAX_EVENTS) -> flush por MAX_WAIT"; T0=$(date +%s)
  for _ in 1 2 3; do touch_item; sleep 1; done
  wait_for 40 '[ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE gate_action=\"AGGREGATE\" AND flushed_at IS NOT NULL AND creation>FROM_UNIXTIME('$T0')")" -ge 3 ]' || { log "FALHA: nao fez flush por MAX_WAIT"; ok=0; }
  log "decorrido ate flush: $(( $(date +%s) - T0 ))s (esperado >= 60s)"; [ $(( $(date +%s) - T0 )) -ge 55 ] || { log "FALHA: flush antes do MAX_WAIT"; ok=0; }
  log "I.2: 10 eventos em poucos segundos -> flush imediato (nao espera MAX_WAIT)"; setcfg agg_max_wait_seconds 600; T1=$(date +%s)
  for _ in $(seq 1 10); do touch_item; done
  wait_for 12 '[ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Outbox\` WHERE gate_action=\"AGGREGATE\" AND flushed_at IS NOT NULL AND creation>FROM_UNIXTIME('$T1')")" -ge 10 ]' || { log "FALHA: nao fez flush imediato"; ok=0; }
  log "decorrido: $(( $(date +%s) - T1 ))s (esperado << 600s)"
  sql "SELECT agg_key,COUNT(*),MIN(ack_at),MAX(flushed_at) FROM \`tabSynapse Outbox\` WHERE gate_action='AGGREGATE' AND creation>FROM_UNIXTIME($T0) GROUP BY agg_key" | tee -a "$EV/I.log"
  log "I.3 reinicio: docker restart $N8N_CT com eventos agregados pendentes nao os perde (estado esta no banco)"
  reset_cfg; verdict I $ok; }

# ---------- J: idempotencia ----------
tJ() { T=J; : > "$EV/J.log"; ok=1; OP=$(uid J); J=$(op_json "$OP" "" "$(ver Item "$ITEM")")
  for i in 1 2 3 4 5 6; do api execute "{\"operation\":$(echo "$J" | jq -Rs .)}" | jq -c '.message // {erro:"sem message"}' >> "$EV/J.log" & done; wait
  cat "$EV/J.log"; [ "$(grep -c '"status":"executed"' "$EV/J.log")" = 6 ] || { log "FALHA: alguma chamada nao devolveu executed/replayed (erro ao chamador)"; ok=0; }
  log "SO com synapse_op_id=$OP: $(so_count "$OP")"; [ "$(so_count "$OP")" = 1 ] || { log "FALHA: != 1 mutacao"; ok=0; }
  [ "$(sql "SELECT COUNT(*) FROM \`tabSynapse Operacao\` WHERE name='$OP'")" = 1 ] || ok=0
  verdict J $ok; }

case "${1:-}" in prepare) prepare;; A|B|C|D|E|F|G|H|I|J) "t$1";; all) for x in A B C D E F G H I J; do "t$x"; done; grep -h "^RESULT" "$EV"/*.log;; *) echo "uso: $0 prepare|A..J|all";; esac
