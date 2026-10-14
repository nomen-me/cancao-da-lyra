#!/usr/bin/env python3
"""Corrige o bundle semaphore.zip (nomen-me/semaphore @ pin) sem reescrever o que ja funciona.
Uso: patch_semaphore_bundle.py <pasta_bundle_extraida> <provisionar_cliente_synapse2.sh>
Toda ancora e verificada: se faltar, ABORTA (nunca gera bundle pela metade)."""
import os, re, shutil, sys

bundle, provisionador = sys.argv[1], sys.argv[2]
sh_path = os.path.join(bundle, 'configurar_semaphore_synapse.sh')
s = open(sh_path, encoding='utf-8').read()

def once(old, new, what):
    global s
    if s.count(old) != 1:
        sys.exit(f"ANCORA '{what}': esperado 1, achei {s.count(old)}")
    s = s.replace(old, new, 1)

def regex_once(pat, repl, what, flags=re.S):
    global s
    m = list(re.finditer(pat, s, flags))
    if len(m) != 1:
        sys.exit(f"ANCORA '{what}': esperado 1, achei {len(m)}")
    s = s[:m[0].start()] + repl + s[m[0].end():]

# 1) imagem do Semaphore: NUNCA latest (baseline v2.19.12, a mesma do instalador)
once('image: semaphoreui/semaphore:latest', 'image: semaphoreui/semaphore:v2.19.12', 'imagem latest')

# 1b) api(): a API oficial v2.19.12 exige project_id no corpo de todo POST/PUT de /api/project/{id}/...
#     (verificado no servidor real: "Project ID in body and URL must be the same"). Injeta de forma central.
once("""  local metodo="$1" caminho="$2" corpo="${3:-}"
  if [ -n "$corpo" ]; then""",
"""  local metodo="$1" caminho="$2" corpo="${3:-}"
  if [ -n "$corpo" ] && { [ "$metodo" = "POST" ] || [ "$metodo" = "PUT" ]; } && [[ "$caminho" =~ ^/api/project/([0-9]+)/ ]]; then
    corpo=$(echo "$corpo" | jq -c --argjson pid "${BASH_REMATCH[1]}" 'if type=="object" and (has("project_id")|not) then . + {project_id:$pid} else . end')
  fi
  if [ -n "$corpo" ]; then""", 'api() project_id')

# 2) acesso ao Git: GIT_AUTH=none (repositorio publico/local) alem do token. Default continua token.
once('GIT_BRANCH="${GIT_BRANCH:-main}"',
     'GIT_BRANCH="${GIT_BRANCH:-main}"\nGIT_AUTH="${GIT_AUTH:-token}"   # token (repo privado, padrao Synapse) | none (repo publico/local)', 'GIT_BRANCH')
once('elif [ -n "${GITHUB_TOKEN:-}" ]; then\n  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" "$(jq -n --arg tok "$GITHUB_TOKEN"',
     'elif [ "$GIT_AUTH" = "none" ]; then\n'
     '  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" \'{"name":"github-nomen-me","type":"none"}\')\n'
     '  GIT_KEY_ID=$(echo "$RESP" | jq -r \'.id // empty\')\n'
     '  [ -n "$GIT_KEY_ID" ] && registrar_criado "Chave de acesso ao Git (tipo none, GIT_AUTH=none)" \\\n'
     '    || registrar_pendente "Chave de acesso ao Git (none)  falhou ao criar"\n'
     'elif [ -n "${GITHUB_TOKEN:-}" ]; then\n  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" "$(jq -n --arg tok "$GITHUB_TOKEN"',
     'chave git none')

# 3) sincronizacao do repo: arquivos GERENCIADOS pelo bundle sao atualizados quando diferem
#    (playbooks + provisionador). Inventory e aplicar_motor_composicao continuam preservados.
once('      ["provisionar_cliente_synapse.sh"]="${SCRIPT_DIR}/provisionar_cliente_synapse.sh"',
     '      ["provisionar_cliente_synapse2.sh"]="${SCRIPT_DIR}/provisionar_cliente_synapse2.sh"', 'arquivo provisionador')
once('      ["playbooks/backup_diario.yml"]="${SCRIPT_DIR}/playbooks/backup_diario.yml"',
     '      ["playbooks/backup_diario.yml"]="${SCRIPT_DIR}/playbooks/backup_diario.yml"\n      ["playbooks/validar_integracao.yml"]="${SCRIPT_DIR}/playbooks/validar_integracao.yml"', 'arquivo validar')
once('''      if [ -f "$REPO_TMPDIR/$destino" ]; then
        ok "Repo já tem '${destino}' — preservado, não sobrescrevi."
        registrar_preservado "Arquivo '${destino}' no repositório (já existia, não tocado)"
      elif [ -f "$origem" ]; then''',
     '''      GERENCIADO="nao"
      case "$destino" in provisionar_cliente_synapse2.sh|playbooks/*) GERENCIADO="sim" ;; esac
      if [ -f "$REPO_TMPDIR/$destino" ] && [ "$GERENCIADO" = "sim" ] && [ -f "$origem" ] && ! cmp -s "$origem" "$REPO_TMPDIR/$destino"; then
        # arquivo gerenciado pelo bundle (versao fixada): o repo passa a refletir o bundle
        cp "$origem" "$REPO_TMPDIR/$destino"; git add "$destino"; ALGO_MUDOU="sim"
        ok "Arquivo gerenciado '${destino}' atualizado para a versao do bundle."
        registrar_corrigido "Arquivo '${destino}' no repositorio atualizado para a versao do bundle"
      elif [ -f "$REPO_TMPDIR/$destino" ]; then
        ok "Repo ja tem '${destino}' atualizado/preservado."
        registrar_preservado "Arquivo '${destino}' no repositorio (ja existia, nao tocado)"
      elif [ -f "$origem" ]; then''', 'sync gerenciado')
once('''else
  registrar_pendente "Sincronização de arquivos no repositório — não executada (GITHUB_TOKEN não fornecido)"
fi''',
     '''elif [ "$GIT_AUTH" = "none" ]; then
  log "GIT_AUTH=none: repositório público/local, sem escrita — o conteúdo já deve estar no repositório (bundle fixado)."
else
  registrar_pendente "Sincronização de arquivos no repositório — não executada (GITHUB_TOKEN não fornecido)"
fi''', 'sync sem token')
once('if [ -n "${GITHUB_TOKEN:-}" ]; then\n  log "Sincronizando playbooks/inventory/scripts no repositório..."',
     'if [ "$GIT_AUTH" = "token" ] && [ -n "${GITHUB_TOKEN:-}" ]; then\n  log "Sincronizando playbooks/inventory/scripts no repositório..."', 'sync condicao')

# 4) Environment: so variaveis CONSUMIDAS pelos playbooks (GOOGLE_PLACES_API_KEY nao e usada em nenhum)
s = s.replace('LYRA_CENTRAL_URL LYRA_ADMIN_API_KEY GITHUB_TOKEN GOOGLE_PLACES_API_KEY', 'LYRA_CENTRAL_URL LYRA_ADMIN_API_KEY GITHUB_TOKEN')
if 'GOOGLE_PLACES_API_KEY; do' in s: sys.exit("GOOGLE_PLACES ainda em loop")

# 5) template de VALIDACAO da integracao (mesmo contrato do Provisionar Cliente, executa so em localhost)
once('TID_BACKUP=$(criar_template_se_ausente "Backup Diário" "playbooks/backup_diario.yml" \'[]\')',
     'TID_BACKUP=$(criar_template_se_ausente "Backup Diário" "playbooks/backup_diario.yml" \'[]\')\n'
     'criar_template_se_ausente "Validar Integracao" "playbooks/validar_integracao.yml" \'[]\'; TID_VALIDAR="$TID_RESULT"', 'template validar')

# 5b) criar_template_se_ausente: devolve o id em TID_RESULT (global) e roda no shell principal.
#     Antes: ok/err/registrar_* dentro de $(...) poluiam o id com logs e PERDIAM registrar_pendente
#     (uma falha de template nao derrubaria a instalacao).
a = s.index('criar_template_se_ausente() {'); b = s.index('\n}\n', a) + 3
fn = s[a:b]
if fn.count('echo "$tid"') != 2 or fn.count('echo ""') != 1: sys.exit("ANCORA criar_template_se_ausente: echo inesperado")
fn = fn.replace('echo "$tid"', 'TID_RESULT="$tid"').replace('    echo ""', '    TID_RESULT=""')
fn = fn.replace('    TID_RESULT="$tid"\n    return', '    TID_RESULT="$tid"\n    return 0')
s = s[:a] + fn + s[b:]
s, n = re.subn(r"^(TID_[A-Z]+)=\$\(criar_template_se_ausente (.*?)'\)\n", lambda m: f"criar_template_se_ausente {m.group(2)}'; {m.group(1)}=\"$TID_RESULT\"\n", s, flags=re.S | re.M)
if n != 3: sys.exit(f"ANCORA chamadas de template: esperado 3, achei {n}")


# 5c) INVENTORY: a v2.19.12 so aceita chave login_password como become ("access key type not supported
#     for ansible become user" - toda task falhava). Os playbooks ja conectam como root: sem become_key.
#     Tambem corrige inventory ja existente criado pela versao anterior do script (deriva).
once(', repository_id:$repo, ssh_key_id:$key, become_key_id:$key}', ', repository_id:$repo, ssh_key_id:$key}', 'inventory become_key')
ANT_INV = '''  registrar_preservado "Inventory 'clientes_ativos' (id ${INV_ID})"
elif [ -n "${REPO_ID:-}" ]; then'''
NOVO_INV = '''  registrar_preservado "Inventory 'clientes_ativos' (id ${INV_ID})"
  INV_JSON=$(api GET "/api/project/${PROJECT_ID}/inventory/${INV_ID}")
  if [ -n "$(echo "$INV_JSON" | jq -r '.become_key_id // empty')" ]; then
    api PUT "/api/project/${PROJECT_ID}/inventory/${INV_ID}" "$(echo "$INV_JSON" | jq -c '.become_key_id = null')" >/dev/null
    registrar_corrigido "Inventory 'clientes_ativos' (id ${INV_ID}) tinha become_key invalido para a v2.19.12 - removido"
  fi
elif [ -n "${REPO_ID:-}" ]; then'''
once(ANT_INV, NOVO_INV, 'inventory existente')

# 6) INTEGRACAO n8n <-> Semaphore (API oficial: /project/{id}/integrations, /values, /aliases)
INTEGRACAO = r'''
# =============================================================================
# 9.9 INTEGRACAO N8N/HARMONIA  (API oficial v2.19.12: POST /project/{id}/integrations,
#     /integrations/{id}/values, /integrations/{id}/aliases; disparo: POST /api/integrations/{alias})
#     Contrato (o que o n8n envia no corpo JSON  tudo CONSUMIDO pelos playbooks):
#       vps_ip, dominio, nome_loja  ->  extra vars da task (variable_type=environment)
#     Autenticacao: auth_method=token. O cabecalho X-Synapse-Token e comparado com o segredo guardado
#     no Key Store (login_password). O segredo NUNCA vai ao repo/HTML/workflow: so ao .env 600 do
#     Harmonia (instalador) e ao arquivo ${INTEGRACAO_ENV_FILE} (600). Idempotente por nome.
#     ATENCAO (verificado no Semaphore real): o endpoint de disparo responde 204 mesmo com token errado;
#     so cria task com o token certo. Por isso a validacao confere a TASK, nao o HTTP.
# =============================================================================
INTEGRACAO_ENV_FILE="${CRED_DIR}/integracao_n8n.env"
INTEGRACAO_HEADER="X-Synapse-Token"
garantir_integracao() {
  local nome="$1" tid="$2" var_prefix="$3"
  if [ -z "${tid:-}" ] || [ -z "${SECRET_KEY_ID:-}" ]; then
    registrar_pendente "Integracao '${nome}'  nao criada (template ou segredo ausente)"; return
  fi
  local iid
  iid=$(api GET "/api/project/${PROJECT_ID}/integrations" | jq -r --arg n "$nome" '.[] | select(.name==$n) | .id' | head -n1)
  local corpo
  corpo=$(jq -n --arg n "$nome" --argjson p "$PROJECT_ID" --argjson t "$tid" --arg h "$INTEGRACAO_HEADER" --argjson k "$SECRET_KEY_ID" \
    '{name:$n, project_id:$p, template_id:$t, auth_method:"token", auth_header:$h, auth_secret_id:$k, searchable:false}')
  if [ -z "${iid:-}" ] || [ "$iid" = "null" ]; then
    local resp; resp=$(api POST "/api/project/${PROJECT_ID}/integrations" "$corpo")
    iid=$(echo "$resp" | jq -r '.id // empty')
    if [ -n "$iid" ]; then ok "Integracao '${nome}' criada (id ${iid})."; registrar_criado "Integracao '${nome}' (id ${iid})  template ${tid}, auth token"
    else err "Falha ao criar a integracao '${nome}'. Resposta: $(echo "$resp" | jq -c . 2>/dev/null || echo "$resp")"; registrar_pendente "Integracao '${nome}'  falhou ao criar"; return; fi
  else
    # existe: garante template/segredo/cabecalho corretos (corrige deriva sem duplicar)
    api PUT "/api/project/${PROJECT_ID}/integrations/${iid}" "$(echo "$corpo" | jq --argjson i "$iid" '. + {id:$i}')" >/dev/null
    ok "Integracao '${nome}' ja existe (id ${iid})  alinhada ao template/segredo atuais."
    registrar_preservado "Integracao '${nome}' (id ${iid})"
  fi
  # extractors: um por parametro do contrato; so cria os que faltam
  local existentes var
  existentes=$(api GET "/api/project/${PROJECT_ID}/integrations/${iid}/values" | jq -r '.[].variable' 2>/dev/null)
  for var in vps_ip dominio nome_loja; do
    if ! echo "$existentes" | grep -qx "$var"; then
      local r; r=$(api POST "/api/project/${PROJECT_ID}/integrations/${iid}/values" "$(jq -n --arg v "$var" --argjson i "$iid" \
        '{name:$v, value_source:"body", body_data_type:"json", key:$v, variable:$v, variable_type:"environment", integration_id:$i}')")
      if [ -n "$(echo "$r" | jq -r '.id // empty')" ]; then registrar_criado "Parametro '${var}' da integracao '${nome}'"
      else err "Falha ao criar o parametro ${var} da integracao ${nome}: $r"; registrar_pendente "Parametro '${var}' da integracao '${nome}'  falhou"; fi
    fi
  done
  # alias (URL de disparo): reaproveita o existente
  local alias_url
  alias_url=$(api GET "/api/project/${PROJECT_ID}/integrations/${iid}/aliases" | jq -r '.[0].url // empty')
  if [ -z "$alias_url" ]; then
    alias_url=$(api POST "/api/project/${PROJECT_ID}/integrations/${iid}/aliases" '{}' | jq -r '.url // empty')
    [ -n "$alias_url" ] && registrar_criado "Alias (URL de disparo) da integracao '${nome}'"
  fi
  local caminho="/api/integrations/${alias_url##*/api/integrations/}"
  if [ -z "$alias_url" ] || [ "$caminho" = "/api/integrations/${alias_url}" ]; then
    registrar_pendente "Alias da integracao '${nome}'  nao obtido"; return
  fi
  # o host do 'url' vem do web_host do Semaphore (nao confiavel); guardamos so o CAMINHO
  sed -i "/^${var_prefix}_PATH=/d" "$INTEGRACAO_ENV_FILE"; echo "${var_prefix}_PATH=${caminho}" >> "$INTEGRACAO_ENV_FILE"
}

if [ -z "${REPO_ID:-}" ] || [ -z "${PROJECT_ID:-}" ]; then
  registrar_pendente "Integracao n8n -> Semaphore  nao criada (repositorio/projeto ausentes)"
else
  touch "$INTEGRACAO_ENV_FILE"; chmod 600 "$INTEGRACAO_ENV_FILE"
  # segredo do token: o Key Store NAO devolve a senha depois de criada; por isso o valor vive no arquivo 600
  SEGREDO_ATUAL=$(grep '^SEMAPHORE_INTEGRACAO_TOKEN=' "$INTEGRACAO_ENV_FILE" 2>/dev/null | cut -d= -f2-)
  SECRET_KEY_ID=$(api GET "/api/project/${PROJECT_ID}/keys" | jq -r '.[] | select(.name=="n8n-integracao-token") | .id' | head -n1)
  if [ -z "$SEGREDO_ATUAL" ] || [ -z "$SECRET_KEY_ID" ]; then
    SEGREDO_NOVO=$(head -c32 /dev/urandom | od -An -tx1 | tr -d ' \n')
    CORPO_KEY=$(jq -n --arg p "$SEGREDO_NOVO" --argjson proj "$PROJECT_ID" '{name:"n8n-integracao-token", type:"login_password", project_id:$proj, login_password:{login:"n8n", password:$p}}')
    if [ -n "$SECRET_KEY_ID" ]; then
      api PUT "/api/project/${PROJECT_ID}/keys/${SECRET_KEY_ID}" "$(echo "$CORPO_KEY" | jq --argjson i "$SECRET_KEY_ID" '. + {id:$i}')" >/dev/null
      registrar_corrigido "Segredo 'n8n-integracao-token' rotacionado (valor anterior nao estava disponivel localmente)"
    else
      SECRET_KEY_ID=$(api POST "/api/project/${PROJECT_ID}/keys" "$CORPO_KEY" | jq -r '.id // empty')
      [ -n "$SECRET_KEY_ID" ] && registrar_criado "Segredo 'n8n-integracao-token' no Key Store (id ${SECRET_KEY_ID})"
    fi
    if [ -n "$SECRET_KEY_ID" ]; then
      sed -i '/^SEMAPHORE_INTEGRACAO_TOKEN=/d;/^SEMAPHORE_INTEGRACAO_HEADER=/d' "$INTEGRACAO_ENV_FILE"
      echo "SEMAPHORE_INTEGRACAO_TOKEN=${SEGREDO_NOVO}" >> "$INTEGRACAO_ENV_FILE"
      echo "SEMAPHORE_INTEGRACAO_HEADER=${INTEGRACAO_HEADER}" >> "$INTEGRACAO_ENV_FILE"
    else registrar_pendente "Segredo 'n8n-integracao-token'  falhou ao criar no Key Store"; fi
  else
    registrar_preservado "Segredo 'n8n-integracao-token' (id ${SECRET_KEY_ID})  valor preservado"
  fi
  garantir_integracao "n8n-provisionar-cliente" "${TID_PROVISIONAR:-}" "SEMAPHORE_INTEGRACAO_PROVISIONAR"
  garantir_integracao "n8n-validar-integracao"  "${TID_VALIDAR:-}"     "SEMAPHORE_INTEGRACAO_VALIDAR"
  for v in SEMAPHORE_INTEGRACAO_TOKEN SEMAPHORE_INTEGRACAO_HEADER SEMAPHORE_INTEGRACAO_PROVISIONAR_PATH SEMAPHORE_INTEGRACAO_VALIDAR_PATH; do
    grep -q "^${v}=.\+" "$INTEGRACAO_ENV_FILE" || registrar_pendente "Arquivo ${INTEGRACAO_ENV_FILE} sem ${v}"
  done
fi
'''
regex_once(r'registrar_pendente "Integração n8n -> Semaphore: endpoints POST[^\n]*\n', INTEGRACAO, 'bloco integracao pendente', flags=re.S)

# 7) relatorio: sem "passo manual"; pendencia => FAIL (exit 1)
regex_once(r'echo -e "\$\{YELLOW\}Passo manual restante.*?echo -e "  4\. Isso d. ao n8n[^\n]*\n',
           'echo -e "${BLUE}Integracao n8n -> Semaphore criada automaticamente (ver secoes 1-3).${NC}"\n', 'passo manual', flags=re.S)
s = s.replace('NÃO executada por este script (dispare pela UI)', 'validada pelo instalador (ciclo n8n -> Semaphore -> Ansible)')
s = s.replace('NÃO executada por este script (dispare pela UI ou aguarde 03:00 ${FUSO_SISTEMA:-})', 'agendado para 03:00 ${FUSO_SISTEMA:-}')
if not s.rstrip().endswith('FAIL_AUTOMATIZACAO'):
    s = s.rstrip('\n') + '''

# Pendencia = FAIL. Nada de "configure depois".
if [ ${#RELATORIO_PENDENTE[@]} -gt 0 ]; then
  echo -e "${RED}FAIL  AUTOMATIZACAO INCOMPLETA (${#RELATORIO_PENDENTE[@]} pendencia(s)).${NC}"
  exit 1
fi
echo -e "${GREEN}PASS  Semaphore configurado sem pendencias.${NC}"
# FAIL_AUTOMATIZACAO
'''
open(sh_path, 'w', encoding='utf-8').write(s)

# --- playbooks ---
pb = os.path.join(bundle, 'playbooks', 'provisionar_cliente.yml')
p = open(pb, encoding='utf-8').read()
def ponce(old, new, what):
    global p
    if p.count(old) != 1: sys.exit(f"ANCORA playbook '{what}': esperado 1, achei {p.count(old)}")
    p = p.replace(old, new, 1)
n_antigo = p.count('provisionar_cliente_synapse.sh')
if n_antigo < 4: sys.exit(f"ANCORA playbook: esperava >=4 referencias ao script antigo, achei {n_antigo}")
p = p.replace('provisionar_cliente_synapse.sh', 'provisionar_cliente_synapse2.sh')   # src, dest, comando e comentarios
open(pb, 'w', encoding='utf-8').write(p)

open(os.path.join(bundle, 'playbooks', 'validar_integracao.yml'), 'w', encoding='utf-8').write('''---
# Template Semaphore: "Validar Integracao"
# Mesmo contrato do "Provisionar Cliente" (extra vars vps_ip, dominio, nome_loja), mas NAO toca em nenhuma VPS:
# executa so em localhost. Serve para provar, de ponta a ponta, n8n -> Integration -> Task -> Ansible.
- name: "Synapse - validar integracao n8n -> Semaphore"
  hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - name: Parametros do contrato presentes e bem formados
      ansible.builtin.assert:
        that:
          - vps_ip is defined
          - dominio is defined
          - vps_ip is match('^([0-9]{1,3}\\.){3}[0-9]{1,3}$')
          - dominio is match('^([a-z0-9-]+\\.)+[a-z]{2,}$')
          - nome_loja is defined
        fail_msg: "Contrato da integracao violado (vps_ip, dominio, nome_loja)."
    - name: Eco do contrato recebido
      ansible.builtin.debug:
        msg: "INTEGRACAO_OK vps_ip={{ vps_ip }} dominio={{ dominio }} nome_loja={{ nome_loja }}"
''')

# provisionador atual (o mesmo arquivo que o cliente executa), no lugar do antigo
old = os.path.join(bundle, 'provisionar_cliente_synapse.sh')
if os.path.exists(old): os.remove(old)
shutil.copy(provisionador, os.path.join(bundle, 'provisionar_cliente_synapse2.sh'))
print("OK bundle semaphore corrigido")
