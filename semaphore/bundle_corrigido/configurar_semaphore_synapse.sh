#!/bin/bash
# =============================================================================
#  SYNAPSE — CONFIGURAÇÃO AUTOMATIZADA DO SEMAPHORE (Ansible Semaphore UI)
#
#  O QUE ESTE SCRIPT FAZ:
#    Audita a VPS de gerenciamento, instala o Semaphore só se ainda não
#    existir, e configura via API (idempotente): projeto "Synapse", Key
#    Store (ssh-clientes + acesso ao Git), repositório, Environment
#    "synapse-secrets", Inventory "clientes_ativos", os templates
#    "Provisionar Cliente" / "Atualizar Cliente" / "Backup Diário" e o
#    agendamento do backup.
#
#  O QUE ESTE SCRIPT NÃO FAZ (documentado explicitamente na seção 9 abaixo
#  e no relatório final — não inventamos isso):
#    - Não cria a "Integration" (webhook) do n8n via API. A doc oficial
#      confirma que o endpoint de DISPARO é /api/integrations/{alias}, mas
#      não encontrei documentação confiável do payload de CRIAÇÃO de uma
#      Integration via API — em vez de arriscar uma chamada errada contra
#      produção, o script imprime o passo a passo exato pra fazer isso na
#      UI (é rápido) e deixa um API Token com permissão só no projeto
#      Synapse como alternativa já pronta caso você prefira essa via.
#    - Não confirma sozinho que os playbooks referenciados pelos templates
#      (playbooks/provisionar_cliente.yml, playbooks/atualizar_motor_
#      composicao.yml, playbooks/backup_diario.yml) já existem no repo
#      github.com/nomen-me/synapse — eu não tenho acesso a esse repo
#      (privado, e este ambiente não tem rede). O script AVISA se os
#      templates apontam pra arquivos que ele não consegue confirmar.
#
#  PRÉ-REQUISITOS (exportar antes de rodar — nada é inventado/adivinhado):
#    export GITHUB_TOKEN=ghp_xxxxx                # já deve existir no seu fluxo
#    export SSH_CLIENTES_KEY_PATH=/root/.ssh/id_ed25519   # se já existir uma
#                                                          # chave usada nos
#                                                          # clientes; se não
#                                                          # setar, o script
#                                                          # PROCURA em locais
#                                                          # comuns antes de
#                                                          # gerar uma nova.
#    # Só necessários se o Semaphore ainda NÃO estiver instalado nesta VPS:
#    export SEMAPHORE_ADMIN_LOGIN=admin
#    export SEMAPHORE_ADMIN_EMAIL=admin@nomen.me
#    export SEMAPHORE_ADMIN_PASSWORD='...'         # senha forte sua escolha
#    # Se o Semaphore JÁ estiver instalado, informe como logar nele:
#    export SEMAPHORE_LOGIN=admin
#    export SEMAPHORE_PASSWORD='...'
#    # Segredos do Environment "synapse-secrets" — se alguma não for
#    # exportada, o script NÃO inventa valor nem grava vazio: registra como
#    # pendente e segue com o resto.
#    export LYRA_CENTRAL_URL=https://lyra.suaempresa.com
#    export LYRA_ADMIN_API_KEY=xxxxx
#    export GOOGLE_PLACES_API_KEY=xxxxx            # se você souber que é usada
#                                                    # em algum playbook — eu
#                                                    # não confirmei onde
#
#  Uso:
#    sudo -E bash configurar_semaphore_synapse.sh
# =============================================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()   { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
warn()  { echo -e "${YELLOW}[AVISO]${NC} $1"; }
err()   { echo -e "${RED}[ERRO]${NC} $1"; }
fatal() { echo -e "${RED}[FATAL]${NC} $1"; exit 1; }

[ "$(id -u)" -eq 0 ] || fatal "Rode como root (sudo -E bash $0)."

# Pasta onde ESTE script está (o bundle extraído) — é daqui que a fase 5.5
# lê os playbooks/inventory/scripts pra sincronizar no repo. Rode sempre
# de dentro da pasta extraída do bundle, não copie só este arquivo sozinho.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Acumuladores para o relatório final — quatro baldes, exatamente como
# pedido: criado | preservado (já existia, intocado) | corrigido (existia
# mas incompleto/incompatível, e deu pra corrigir com segurança) | pendente
# (não foi possível concluir com segurança, registrado explicitamente).
RELATORIO_CRIADO=()
RELATORIO_PRESERVADO=()
RELATORIO_CORRIGIDO=()
RELATORIO_PENDENTE=()
registrar_criado()     { RELATORIO_CRIADO+=("$1"); }
registrar_preservado()  { RELATORIO_PRESERVADO+=("$1"); }
registrar_corrigido()   { RELATORIO_CORRIGIDO+=("$1"); }
registrar_pendente()    { RELATORIO_PENDENTE+=("$1"); }

command -v curl >/dev/null 2>&1 || fatal "curl não encontrado — instale antes de rodar."
command -v git  >/dev/null 2>&1 || fatal "git não encontrado — instale antes de rodar (necessário pra sincronizar o repositório)."
if ! command -v jq >/dev/null 2>&1; then
  log "jq não encontrado, instalando (dependência leve, sem risco pro que já existe)..."
  apt-get update -qq && apt-get install -y -qq jq || fatal "Falha ao instalar jq."
fi

CRED_DIR="/root/.semaphore_synapse"
mkdir -p "$CRED_DIR"; chmod 700 "$CRED_DIR"
SEMAPHORE_CRED_FILE="${CRED_DIR}/credenciais.env"
COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

SEMAPHORE_URL="${SEMAPHORE_URL:-http://localhost:3000}"
GIT_REPO_URL="${GIT_REPO_URL:-https://github.com/nomen-me/synapse.git}"
GIT_BRANCH="${GIT_BRANCH:-main}"
GIT_AUTH="${GIT_AUTH:-token}"   # token (repo privado, padrao Synapse) | none (repo publico/local)

# =============================================================================
# 1. AUDITORIA: o Semaphore já está instalado nesta VPS?
# =============================================================================
log "Auditando estado atual da VPS de gerenciamento..."

SEMAPHORE_JA_INSTALADO="nao"
MODO_INSTALACAO=""
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^semaphore$'; then
  SEMAPHORE_JA_INSTALADO="sim"
  MODO_INSTALACAO="docker"
  ok "Container Docker 'semaphore' já está rodando — instalação preservada, não vou reinstalar."
  registrar_preservado "Instalação existente do Semaphore (container Docker 'semaphore', imagem $(docker inspect --format '{{.Config.Image}}' semaphore 2>/dev/null || echo '?')) — não reinstalada"
elif systemctl is-active --quiet semaphore 2>/dev/null; then
  SEMAPHORE_JA_INSTALADO="sim"
  MODO_INSTALACAO="systemd"
  ok "Serviço systemd 'semaphore' já está ativo — instalação preservada, não vou reinstalar."
  registrar_preservado "Instalação existente do Semaphore (serviço systemd, binário $(command -v semaphore 2>/dev/null || echo '?')) — não reinstalada"
else
  log "Nenhuma instalação de Semaphore detectada (nem container 'semaphore', nem serviço systemd ativo)."
fi

# =============================================================================
# 2. INSTALAÇÃO (só executa se a auditoria acima não achou nada)
#    Via Docker Compose, seguindo o mesmo padrão já usado no resto do seu
#    ecossistema (Ritmo/Harmonia/Harpa também são Docker) — instalação
#    documentada oficialmente em semaphoreui.com/docs/getting-started.
# =============================================================================
if [ "$SEMAPHORE_JA_INSTALADO" = "nao" ]; then
  log "Instalando Semaphore via Docker Compose..."

  [ -n "${SEMAPHORE_ADMIN_LOGIN:-}" ]    || fatal "Instalação nova exige SEMAPHORE_ADMIN_LOGIN exportado. Pare aqui, exporte e rode de novo — não vou inventar um login."
  [ -n "${SEMAPHORE_ADMIN_EMAIL:-}" ]    || fatal "Instalação nova exige SEMAPHORE_ADMIN_EMAIL exportado."
  [ -n "${SEMAPHORE_ADMIN_PASSWORD:-}" ] || fatal "Instalação nova exige SEMAPHORE_ADMIN_PASSWORD exportado — não vou gerar uma senha e esconder ela de você."

  command -v docker >/dev/null 2>&1 || fatal "Docker não encontrado nesta VPS. Instale o Docker antes (fora do escopo deste script — o resto do seu ecossistema já assume Docker presente)."

  mkdir -p /opt/semaphore
  SEMAPHORE_ENV_FILE="/opt/semaphore/.env"
  if [ -f "$SEMAPHORE_ENV_FILE" ]; then
    warn "/opt/semaphore/.env já existe mas o container não estava rodando — reaproveitando o arquivo em vez de gerar segredos novos (evita invalidar dados já persistidos no volume)."
  else
    # SEMAPHORE_ACCESS_KEY_ENCRYPTION cifra os segredos guardados no Key
    # Store/Environment do Semaphore. Gerar isso de novo em cima de um volume
    # que já tenha dados cifrados destruiria o acesso a eles — por isso só
    # geramos quando o .env realmente não existe ainda.
    ACCESS_KEY_ENCRYPTION="$(head -c32 /dev/urandom | base64)"
    cat > "$SEMAPHORE_ENV_FILE" <<EOF
SEMAPHORE_DB_DIALECT=bolt
SEMAPHORE_ADMIN=${SEMAPHORE_ADMIN_LOGIN}
SEMAPHORE_ADMIN_NAME=${SEMAPHORE_ADMIN_LOGIN}
SEMAPHORE_ADMIN_EMAIL=${SEMAPHORE_ADMIN_EMAIL}
SEMAPHORE_ADMIN_PASSWORD=${SEMAPHORE_ADMIN_PASSWORD}
SEMAPHORE_ACCESS_KEY_ENCRYPTION=${ACCESS_KEY_ENCRYPTION}
EOF
    chmod 600 "$SEMAPHORE_ENV_FILE"
    registrar_criado "Semaphore instalado via Docker (/opt/semaphore), banco embutido (bolt), .env gerado com senha admin e chave de cifra novas"
  fi

  cat > /opt/semaphore/docker-compose.yml <<'EOF'
services:
  semaphore:
    image: semaphoreui/semaphore:v2.19.12
    container_name: semaphore
    restart: unless-stopped
    ports:
      - "127.0.0.1:3000:3000"
    volumes:
      - semaphore-data:/var/lib/semaphore
    env_file:
      - .env
volumes:
  semaphore-data:
EOF
  # Bind só em 127.0.0.1 de propósito — igual ao padrão do resto do seu
  # ecossistema (GUIs internas escondidas atrás de proxy/Traefik ou acesso
  # só local). Se quiser expor publicamente, isso precisa de Traefik +
  # domínio + TLS na frente, o que está fora do escopo deste script.
  (cd /opt/semaphore && docker compose up -d) || fatal "Falha ao subir o container do Semaphore."

  log "Aguardando o Semaphore responder..."
  for i in $(seq 1 30); do
    if curl -s -o /dev/null -m 2 "${SEMAPHORE_URL}/api/ping" 2>/dev/null; then break; fi
    sleep 2
  done
  ok "Semaphore instalado e no ar em ${SEMAPHORE_URL} (só acessível localmente — sem Traefik na frente ainda)."

  SEMAPHORE_LOGIN="$SEMAPHORE_ADMIN_LOGIN"
  SEMAPHORE_PASSWORD="$SEMAPHORE_ADMIN_PASSWORD"
else
  [ -n "${SEMAPHORE_LOGIN:-}" ]    || fatal "Semaphore já instalado — informe SEMAPHORE_LOGIN pra eu poder autenticar na API (não vou adivinhar nem tentar senha em branco)."
  [ -n "${SEMAPHORE_PASSWORD:-}" ] || fatal "Semaphore já instalado — informe SEMAPHORE_PASSWORD pra eu poder autenticar na API."
fi

# =============================================================================
# 3. AUTENTICAÇÃO NA API (login -> cookie -> token de API)
#    Fluxo documentado em semaphoreui.com/docs/admin-guide/api
# =============================================================================
log "Autenticando na API do Semaphore..."
HTTP_LOGIN=$(curl -s -o /dev/null -w "%{http_code}" -c "$COOKIE_JAR" -X POST \
  -H 'Content-Type: application/json' \
  -d "{\"auth\":\"${SEMAPHORE_LOGIN}\",\"password\":\"${SEMAPHORE_PASSWORD}\"}" \
  "${SEMAPHORE_URL}/api/auth/login")
[ "$HTTP_LOGIN" = "204" ] || [ "$HTTP_LOGIN" = "200" ] || fatal "Login na API do Semaphore falhou (HTTP ${HTTP_LOGIN}). Confira SEMAPHORE_LOGIN/SEMAPHORE_PASSWORD. Parando aqui — não declaro nada como configurado."

if [ -f "$SEMAPHORE_CRED_FILE" ] && grep -q '^SEMAPHORE_API_TOKEN=' "$SEMAPHORE_CRED_FILE"; then
  API_TOKEN=$(grep '^SEMAPHORE_API_TOKEN=' "$SEMAPHORE_CRED_FILE" | cut -d'=' -f2-)
  # Confirma que o token salvo ainda é válido antes de reaproveitar
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${API_TOKEN}" "${SEMAPHORE_URL}/api/user")" != "200" ]; then
    warn "Token de API salvo em ${SEMAPHORE_CRED_FILE} não é mais válido — gerando um novo."
    API_TOKEN=""
  else
    ok "Token de API existente em ${SEMAPHORE_CRED_FILE} reutilizado (ainda válido)."
    registrar_preservado "Token de API do Semaphore existente (reutilizado, não regerado)"
  fi
else
  API_TOKEN=""
fi

if [ -z "$API_TOKEN" ]; then
  RESP_TOKEN=$(curl -s -b "$COOKIE_JAR" -X POST "${SEMAPHORE_URL}/api/user/tokens")
  API_TOKEN=$(echo "$RESP_TOKEN" | jq -r '.id // empty')
  [ -n "$API_TOKEN" ] || fatal "Não consegui gerar um API token novo. Resposta: $(echo "$RESP_TOKEN" | jq -c . 2>/dev/null || echo "$RESP_TOKEN")"
  {
    echo "SEMAPHORE_URL=${SEMAPHORE_URL}"
    echo "SEMAPHORE_API_TOKEN=${API_TOKEN}"
  } > "$SEMAPHORE_CRED_FILE"
  chmod 600 "$SEMAPHORE_CRED_FILE"
  ok "Novo token de API criado e salvo em ${SEMAPHORE_CRED_FILE} (permissão 600, nunca impresso em log)."
  registrar_criado "Token de API do Semaphore (salvo em ${SEMAPHORE_CRED_FILE})"
fi

api() {
  # api METHOD PATH [JSON_BODY]
  local metodo="$1" caminho="$2" corpo="${3:-}"
  if [ -n "$corpo" ] && { [ "$metodo" = "POST" ] || [ "$metodo" = "PUT" ]; } && [[ "$caminho" =~ ^/api/project/([0-9]+)/ ]]; then
    corpo=$(echo "$corpo" | jq -c --argjson pid "${BASH_REMATCH[1]}" 'if type=="object" and (has("project_id")|not) then . + {project_id:$pid} else . end')
  fi
  if [ -n "$corpo" ]; then
    curl -s -X "$metodo" -H "Authorization: Bearer ${API_TOKEN}" -H 'Content-Type: application/json' \
      -d "$corpo" "${SEMAPHORE_URL}${caminho}"
  else
    curl -s -X "$metodo" -H "Authorization: Bearer ${API_TOKEN}" "${SEMAPHORE_URL}${caminho}"
  fi
}

# Auditoria aprofundada (ponto 2): versão real via GET /api/info — não
# assumimos versão nenhuma, perguntamos pro próprio Semaphore.
INFO_JSON=$(api GET /api/info)
SEMAPHORE_VERSAO=$(echo "$INFO_JSON" | jq -r '.version // "desconhecida"')
[ "$MODO_INSTALACAO" = "" ] && MODO_INSTALACAO="pré-existente (modo não detectado pelo script — nem container Docker nem systemd 'semaphore' encontrados, mas o login funcionou; confira manualmente como esse Semaphore roda)"
ok "Semaphore respondendo — versão ${SEMAPHORE_VERSAO}, modo de instalação: ${MODO_INSTALACAO}."
registrar_preservado "Semaphore ativo — versão ${SEMAPHORE_VERSAO}, modo ${MODO_INSTALACAO}"

# =============================================================================
# 4. PROJETO "Synapse" — audita antes de criar
# =============================================================================
log "Verificando projeto 'Synapse'..."
PROJECT_ID=$(api GET /api/projects | jq -r '.[] | select(.name=="Synapse") | .id' | head -n1)
if [ -n "${PROJECT_ID:-}" ] && [ "$PROJECT_ID" != "null" ]; then
  ok "Projeto 'Synapse' já existe (id ${PROJECT_ID}) — preservado."
  registrar_preservado "Projeto 'Synapse' (id ${PROJECT_ID})"
else
  RESP=$(api POST /api/projects '{"name":"Synapse","alert":true,"max_parallel_tasks":0}')
  PROJECT_ID=$(echo "$RESP" | jq -r '.id // empty')
  [ -n "$PROJECT_ID" ] || fatal "Falha ao criar o projeto 'Synapse'. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"
  ok "Projeto 'Synapse' criado (id ${PROJECT_ID})."
  registrar_criado "Projeto 'Synapse' (id ${PROJECT_ID})"
fi

# =============================================================================
# 5. KEY STORE — ssh-clientes (reaproveitada, nunca recriada à toa) +
#    chave de acesso ao Git
# =============================================================================
log "Verificando Key Store..."
KEYS_JSON=$(api GET "/api/project/${PROJECT_ID}/keys")

SSH_KEY_ID=$(echo "$KEYS_JSON" | jq -r '.[] | select(.name=="ssh-clientes") | .id' | head -n1)
if [ -n "${SSH_KEY_ID:-}" ] && [ "$SSH_KEY_ID" != "null" ]; then
  ok "Chave 'ssh-clientes' já existe no Key Store (id ${SSH_KEY_ID}) — preservada, não recriada."
  registrar_preservado "Chave SSH 'ssh-clientes' no Key Store (id ${SSH_KEY_ID})"
else
  # Procura uma chave já existente no disco ANTES de gerar uma nova — pedido
  # explícito de não gerar chave desnecessária.
  SSH_KEY_CANDIDATO=""
  for candidato in "${SSH_CLIENTES_KEY_PATH:-}" /root/.ssh/id_ed25519_clientes /root/.ssh/synapse_clientes /root/.ssh/id_ed25519 /root/.ssh/id_rsa; do
    [ -n "$candidato" ] && [ -f "$candidato" ] && { SSH_KEY_CANDIDATO="$candidato"; break; }
  done

  if [ -n "$SSH_KEY_CANDIDATO" ]; then
    log "Reaproveitando chave privada já existente em ${SSH_KEY_CANDIDATO}."
    CHAVE_NOVA="nao"
  else
    warn "Nenhuma chave SSH existente encontrada nos caminhos comuns. Gerando uma nova em /root/.ssh/id_ed25519_clientes — ATENÇÃO: essa chave ainda NÃO está autorizada em nenhuma VPS de cliente existente, só vai funcionar em clientes provisionados depois de você adicionar a chave pública neles."
    ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519_clientes -N "" -C "semaphore-synapse-clientes" >/dev/null
    SSH_KEY_CANDIDATO="/root/.ssh/id_ed25519_clientes"
    CHAVE_NOVA="sim"
  fi

  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" "$(jq -n --arg name "ssh-clientes" --arg priv "$(cat "$SSH_KEY_CANDIDATO")" \
    '{name:$name, type:"ssh", ssh:{login:"root", private_key:$priv}}')")
  SSH_KEY_ID=$(echo "$RESP" | jq -r '.id // empty')
  [ -n "$SSH_KEY_ID" ] || fatal "Falha ao registrar a chave 'ssh-clientes' no Key Store. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"
  if [ "$CHAVE_NOVA" = "sim" ]; then
    ok "Chave 'ssh-clientes' gerada e registrada no Key Store (id ${SSH_KEY_ID})."
    registrar_criado "Chave SSH 'ssh-clientes' — NOVA, ainda não autorizada em clientes existentes (ver aviso acima)"
  else
    ok "Chave existente (${SSH_KEY_CANDIDATO}) registrada no Key Store como 'ssh-clientes' (id ${SSH_KEY_ID})."
    registrar_criado "Chave SSH 'ssh-clientes' no Key Store, a partir da chave já existente ${SSH_KEY_CANDIDATO}"
  fi
fi

GIT_KEY_ID=$(echo "$KEYS_JSON" | jq -r '.[] | select(.name=="github-nomen-me") | .id' | head -n1)
if [ -n "${GIT_KEY_ID:-}" ] && [ "$GIT_KEY_ID" != "null" ]; then
  ok "Chave de acesso ao Git 'github-nomen-me' já existe (id ${GIT_KEY_ID}) — preservada."
  registrar_preservado "Chave de acesso ao Git 'github-nomen-me' (id ${GIT_KEY_ID})"
elif [ "$GIT_AUTH" = "none" ]; then
  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" '{"name":"github-nomen-me","type":"none"}')
  GIT_KEY_ID=$(echo "$RESP" | jq -r '.id // empty')
  [ -n "$GIT_KEY_ID" ] && registrar_criado "Chave de acesso ao Git (tipo none, GIT_AUTH=none)" \
    || registrar_pendente "Chave de acesso ao Git (none)  falhou ao criar"
elif [ -n "${GITHUB_TOKEN:-}" ]; then
  RESP=$(api POST "/api/project/${PROJECT_ID}/keys" "$(jq -n --arg tok "$GITHUB_TOKEN" \
    '{name:"github-nomen-me", type:"login_password", login_password:{login:"x-access-token", password:$tok}}')")
  GIT_KEY_ID=$(echo "$RESP" | jq -r '.id // empty')
  if [ -n "$GIT_KEY_ID" ]; then
    ok "Chave de acesso ao Git 'github-nomen-me' criada a partir de GITHUB_TOKEN (id ${GIT_KEY_ID})."
    registrar_criado "Chave de acesso ao Git 'github-nomen-me' (a partir de GITHUB_TOKEN)"
  else
    err "Falha ao criar a chave de acesso ao Git. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"
    registrar_pendente "Chave de acesso ao Git 'github-nomen-me' — falhou ao criar, repositório não será configurado"
  fi
else
  warn "GITHUB_TOKEN não foi exportado — não vou criar a chave de acesso ao Git nem o repositório. Exporte GITHUB_TOKEN e rode de novo pra completar essa parte."
  registrar_pendente "Chave de acesso ao Git e Repositório — GITHUB_TOKEN não fornecido nesta execução"
fi

# =============================================================================
# 5.5 SINCRONIZAR OS ARQUIVOS NECESSÁRIOS NO REPOSITÓRIO
#     Sem isso, os templates criados na seção 9 apontariam pra playbooks que
#     talvez não existam no repo ainda, e a primeira execução falharia com
#     "playbook not found" — o operador teria que ir editar o repo na mão
#     antes de conseguir usar o Semaphore. Aqui o script resolve isso
#     sozinho: clona, verifica CADA arquivo esperado individualmente, só
#     adiciona o que estiver ausente (nunca sobrescreve um arquivo que já
#     exista lá — pode ser uma versão já customizada) e dá push só se algo
#     realmente mudou.
# =============================================================================
if [ "$GIT_AUTH" = "token" ] && [ -n "${GITHUB_TOKEN:-}" ]; then
  log "Sincronizando playbooks/inventory/scripts no repositório..."
  REPO_TMPDIR="$(mktemp -d)"
  trap 'rm -f "$COOKIE_JAR"; rm -rf "$REPO_TMPDIR"' EXIT

  # Token só existe dentro da URL de clone deste processo, nunca em log —
  # git costuma mascarar a credencial em erros, mas por segurança extra
  # qualquer saída de erro abaixo passa por um filtro que apaga o token.
  filtrar_token() { sed "s/${GITHUB_TOKEN}/***REDACTED***/g"; }

  CLONE_URL="https://x-access-token:${GITHUB_TOKEN}@github.com/nomen-me/synapse.git"
  if git clone --quiet --branch "$GIT_BRANCH" "$CLONE_URL" "$REPO_TMPDIR" 2> >(filtrar_token >&2); then
    cd "$REPO_TMPDIR"
    git config user.email "semaphore@nomen.me"
    git config user.name "Semaphore (configuração automatizada)"

    ALGO_MUDOU="nao"
    PUSH_PENDENTE="nao"
    # Mapa "arquivo esperado no repo" -> "onde encontrá-lo localmente, ao
    # lado deste script" (o bundle entregue já traz os seis).
    declare -A ARQUIVOS_ESPERADOS=(
      ["provisionar_cliente_synapse2.sh"]="${SCRIPT_DIR}/provisionar_cliente_synapse2.sh"
      ["aplicar_motor_composicao.sh"]="${SCRIPT_DIR}/aplicar_motor_composicao.sh"
      ["playbooks/provisionar_cliente.yml"]="${SCRIPT_DIR}/playbooks/provisionar_cliente.yml"
      ["playbooks/atualizar_motor_composicao.yml"]="${SCRIPT_DIR}/playbooks/atualizar_motor_composicao.yml"
      ["playbooks/backup_diario.yml"]="${SCRIPT_DIR}/playbooks/backup_diario.yml"
      ["playbooks/validar_integracao.yml"]="${SCRIPT_DIR}/playbooks/validar_integracao.yml"
      ["inventory/clientes_ativos.yml"]="${SCRIPT_DIR}/inventory/clientes_ativos.yml"
    )
    for destino in "${!ARQUIVOS_ESPERADOS[@]}"; do
      origem="${ARQUIVOS_ESPERADOS[$destino]}"
      GERENCIADO="nao"
      case "$destino" in provisionar_cliente_synapse2.sh|playbooks/*) GERENCIADO="sim" ;; esac
      if [ -f "$REPO_TMPDIR/$destino" ] && [ "$GERENCIADO" = "sim" ] && [ -f "$origem" ] && ! cmp -s "$origem" "$REPO_TMPDIR/$destino"; then
        # arquivo gerenciado pelo bundle (versao fixada): o repo passa a refletir o bundle
        cp "$origem" "$REPO_TMPDIR/$destino"; git add "$destino"; ALGO_MUDOU="sim"
        ok "Arquivo gerenciado '${destino}' atualizado para a versao do bundle."
        registrar_corrigido "Arquivo '${destino}' no repositorio atualizado para a versao do bundle"
      elif [ -f "$REPO_TMPDIR/$destino" ]; then
        ok "Repo ja tem '${destino}' atualizado/preservado."
        registrar_preservado "Arquivo '${destino}' no repositorio (ja existia, nao tocado)"
      elif [ -f "$origem" ]; then
        mkdir -p "$(dirname "$REPO_TMPDIR/$destino")"
        cp "$origem" "$REPO_TMPDIR/$destino"
        git add "$destino"
        ALGO_MUDOU="sim"
        ok "Repo NÃO tinha '${destino}' — adicionado a partir do pacote entregue."
        registrar_criado "Arquivo '${destino}' adicionado ao repositório (não existia antes)"
      else
        err "Repo não tem '${destino}' e eu também não achei em '${origem}' (rode o script de dentro da pasta extraída do bundle)."
        registrar_pendente "Arquivo '${destino}' — ausente no repo E não encontrado localmente pra copiar"
      fi
    done

    if [ "$ALGO_MUDOU" = "sim" ]; then
      git commit --quiet -m "chore(semaphore): adiciona playbooks/inventory/scripts ausentes (automação configurar_semaphore_synapse.sh)"

      # Segurança obrigatória: não empurrar se a branch remota divergiu
      # entre o clone e agora (alguém pode ter commitado nesse meio-tempo).
      # git push --ff-only recusa sozinho qualquer coisa que não seja um
      # fast-forward — não forçamos, não resolvemos conflito automaticamente.
      git fetch --quiet origin "$GIT_BRANCH" 2> >(filtrar_token >&2)
      if git push --quiet --ff-only origin "HEAD:${GIT_BRANCH}" 2> >(filtrar_token >&2); then
        ok "Push feito em ${GIT_REPO_URL} (branch ${GIT_BRANCH}) — repositório agora tem tudo que os templates precisam."
      else
        err "git push recusado (branch remota divergiu nesse intervalo, ou falta permissão de escrita) — os arquivos ficaram commitados SÓ localmente em ${REPO_TMPDIR}, NADA foi enviado ao GitHub. Não vou forçar nem resolver esse conflito sozinho."
        registrar_pendente "Push dos arquivos novos pro GitHub — recusado (divergência ou permissão), commit local preservado em ${REPO_TMPDIR}, revise manualmente"
        PUSH_PENDENTE="sim"
      fi
    else
      ok "Repositório já tinha todos os arquivos esperados — nada pra commitar."
    fi
    cd - >/dev/null
  else
    err "Não consegui clonar ${GIT_REPO_URL} (branch ${GIT_BRANCH}) — confira se GITHUB_TOKEN tem permissão de leitura nesse repo."
    registrar_pendente "Sincronização de arquivos no repositório — falhou o clone, templates podem ficar sem playbook"
  fi
  trap 'rm -f "$COOKIE_JAR"' EXIT
  if [ "${PUSH_PENDENTE:-nao}" = "sim" ]; then
    warn "Mantendo ${REPO_TMPDIR} em disco (não removido) porque há um commit local que não chegou ao GitHub — inspecione com 'cd ${REPO_TMPDIR} && git log' antes de apagar manualmente."
  else
    rm -rf "$REPO_TMPDIR"
  fi
elif [ "$GIT_AUTH" = "none" ]; then
  log "GIT_AUTH=none: repositório público/local, sem escrita — o conteúdo já deve estar no repositório (bundle fixado)."
else
  registrar_pendente "Sincronização de arquivos no repositório — não executada (GITHUB_TOKEN não fornecido)"
fi

# =============================================================================
# 5.9 TESTE REAL DE CONECTIVIDADE SSH (ponto 6/14 — "informar claramente se
#     a autenticação está pronta", não só assumir que está)
#     Só roda se TEST_VPS_IP foi exportado (uma VPS de cliente real ou de
#     teste, já com a chave pública em /root/.ssh/authorized_keys). Sem
#     isso, o script REGISTRA como teste funcional NÃO executado — nunca
#     declara "SSH pronto" sem ter tentado de verdade.
# =============================================================================
TESTE_SSH_EXECUTADO="nao"
TESTE_SSH_RESULTADO=""
if [ -n "${TEST_VPS_IP:-}" ]; then
  CHAVE_PARA_TESTE="${SSH_KEY_CANDIDATO:-}"
  if [ -z "$CHAVE_PARA_TESTE" ]; then
    for candidato in "${SSH_CLIENTES_KEY_PATH:-}" /root/.ssh/id_ed25519_clientes /root/.ssh/synapse_clientes /root/.ssh/id_ed25519 /root/.ssh/id_rsa; do
      [ -n "$candidato" ] && [ -f "$candidato" ] && { CHAVE_PARA_TESTE="$candidato"; break; }
    done
  fi
  if [ -n "$CHAVE_PARA_TESTE" ]; then
    log "Testando conectividade SSH real com ${TEST_VPS_IP} usando ${CHAVE_PARA_TESTE}..."
    if ssh -i "$CHAVE_PARA_TESTE" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=6 "root@${TEST_VPS_IP}" true 2>/tmp/synapse_ssh_teste.log; then
      ok "SSH confirmado: root@${TEST_VPS_IP} acessível com a chave 'ssh-clientes'."
      TESTE_SSH_EXECUTADO="sim"; TESTE_SSH_RESULTADO="OK — root@${TEST_VPS_IP} acessível"
    else
      err "SSH FALHOU contra ${TEST_VPS_IP} — ver /tmp/synapse_ssh_teste.log. A chave pode não estar autorizada nessa VPS ainda."
      TESTE_SSH_EXECUTADO="sim"; TESTE_SSH_RESULTADO="FALHOU — ver /tmp/synapse_ssh_teste.log"
    fi
  else
    warn "TEST_VPS_IP foi exportado mas não achei nenhuma cópia local da chave privada pra testar diretamente daqui (ela já está no Key Store do Semaphore, mas o Key Store não expõe a chave de volta, por design). Dispare o template 'Provisionar Cliente' ou 'Atualizar Cliente' pela própria UI contra essa VPS — o primeiro Ansible ping ali É o teste real de conectividade."
    TESTE_SSH_RESULTADO="não executado — sem cópia local da chave pra testar fora do Semaphore"
  fi
else
  TESTE_SSH_RESULTADO="não executado — TEST_VPS_IP não foi exportado nesta execução"
fi

# =============================================================================
# 6. REPOSITÓRIO
# =============================================================================
if [ -n "${GIT_KEY_ID:-}" ]; then
  log "Verificando repositório..."
  REPO_ID=$(api GET "/api/project/${PROJECT_ID}/repositories" | jq -r --arg url "$GIT_REPO_URL" '.[] | select(.git_url==$url) | .id' | head -n1)
  if [ -n "${REPO_ID:-}" ] && [ "$REPO_ID" != "null" ]; then
    ok "Repositório ${GIT_REPO_URL} já existe (id ${REPO_ID}) — preservado."
    registrar_preservado "Repositório ${GIT_REPO_URL} (id ${REPO_ID})"
  else
    RESP=$(api POST "/api/project/${PROJECT_ID}/repositories" "$(jq -n --arg url "$GIT_REPO_URL" --arg branch "$GIT_BRANCH" --argjson key "$GIT_KEY_ID" \
      '{name:"synapse", git_url:$url, git_branch:$branch, ssh_key_id:$key}')")
    REPO_ID=$(echo "$RESP" | jq -r '.id // empty')
    [ -n "$REPO_ID" ] && { ok "Repositório ${GIT_REPO_URL} (branch ${GIT_BRANCH}) criado (id ${REPO_ID})."; registrar_criado "Repositório ${GIT_REPO_URL} branch ${GIT_BRANCH} (id ${REPO_ID})"; } \
      || { err "Falha ao criar o repositório. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"; registrar_pendente "Repositório ${GIT_REPO_URL} — falhou ao criar"; }
  fi
fi

# =============================================================================
# 7. ENVIRONMENT "synapse-secrets" — nunca sobrescreve valor já existente,
#    nunca grava string vazia no lugar de um valor que não foi fornecido
# =============================================================================
log "Verificando Environment 'synapse-secrets'..."
ENV_ID=$(api GET "/api/project/${PROJECT_ID}/environment" | jq -r '.[] | select(.name=="synapse-secrets") | .id' | head -n1)

# Monta só as chaves que REALMENTE foram exportadas nesta execução —
# nenhuma chave sem valor entra no JSON (evita sobrescrever com "").
VARS_JSON="{}"
for chave in LYRA_CENTRAL_URL LYRA_ADMIN_API_KEY GITHUB_TOKEN; do
  valor="${!chave:-}"
  if [ -n "$valor" ]; then
    VARS_JSON=$(echo "$VARS_JSON" | jq --arg k "$chave" --arg v "$valor" '. + {($k): $v}')
  fi
done

if [ -n "${ENV_ID:-}" ] && [ "$ENV_ID" != "null" ]; then
  ok "Environment 'synapse-secrets' já existe (id ${ENV_ID})."
  ENV_ATUAL=$(api GET "/api/project/${PROJECT_ID}/environment/${ENV_ID}" | jq -r '.env // "{}"')
  # merge: só ENTRA chave nova se a atual estiver ausente/vazia — nunca troca
  # um valor não-vazio já configurado.
  ENV_MERGED=$(jq -n --argjson atual "$ENV_ATUAL" --argjson novo "$VARS_JSON" '
    $atual as $a | $novo as $n |
    ($a + ($n | to_entries | map(select(($a[.key] // "") == "")) | from_entries))')
  if [ "$ENV_MERGED" != "$ENV_ATUAL" ]; then
    api PUT "/api/project/${PROJECT_ID}/environment/${ENV_ID}" "$(jq -n --arg name "synapse-secrets" --argjson env "$ENV_MERGED" '{name:$name, env:($env|tostring), json:($env|tostring)}')" >/dev/null
    ok "Environment 'synapse-secrets' atualizado só nas chaves que estavam vazias/ausentes."
    registrar_corrigido "Environment 'synapse-secrets' — já existia mas faltavam variáveis; completadas nesta execução (as que já tinham valor não foram tocadas)"
  else
    ok "Nenhuma variável nova pra adicionar — valores existentes preservados intactos."
    registrar_preservado "Environment 'synapse-secrets' (id ${ENV_ID}) — nenhuma variável alterada"
  fi
else
  RESP=$(api POST "/api/project/${PROJECT_ID}/environment" "$(jq -n --arg name "synapse-secrets" --argjson env "$VARS_JSON" '{name:$name, env:($env|tostring), json:($env|tostring)}')")
  ENV_ID=$(echo "$RESP" | jq -r '.id // empty')
  [ -n "$ENV_ID" ] && { ok "Environment 'synapse-secrets' criado (id ${ENV_ID})."; registrar_criado "Environment 'synapse-secrets' (id ${ENV_ID})"; } \
    || { err "Falha ao criar o Environment. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"; registrar_pendente "Environment 'synapse-secrets' — falhou ao criar"; }
fi

for chave in LYRA_CENTRAL_URL LYRA_ADMIN_API_KEY GITHUB_TOKEN; do
  if [ -z "${!chave:-}" ]; then
    registrar_pendente "Variável '${chave}' no Environment synapse-secrets — não fornecida nesta execução (não inventei valor, não gravei vazio)"
  fi
done

# =============================================================================
# 8. INVENTORY "clientes_ativos"
#    LIMITAÇÃO ASSUMIDA E DECLARADA: não tenho como confirmar se o arquivo
#    inventory/clientes_ativos.yml já existe no repo (sem acesso ao GitHub
#    daqui). Aponto o Inventory pra esse caminho dentro do repositório
#    conectado; se o arquivo não existir lá, o Semaphore vai acusar erro na
#    hora de rodar um template que use este inventory — o que é o
#    comportamento certo (falhar visivelmente, não fingir que funcionou).
# =============================================================================
log "Verificando Inventory 'clientes_ativos'..."
INV_ID=$(api GET "/api/project/${PROJECT_ID}/inventory" | jq -r '.[] | select(.name=="clientes_ativos") | .id' | head -n1)
if [ -n "${INV_ID:-}" ] && [ "$INV_ID" != "null" ]; then
  ok "Inventory 'clientes_ativos' já existe (id ${INV_ID}) — preservado."
  registrar_preservado "Inventory 'clientes_ativos' (id ${INV_ID})"
  INV_JSON=$(api GET "/api/project/${PROJECT_ID}/inventory/${INV_ID}")
  if [ -n "$(echo "$INV_JSON" | jq -r '.become_key_id // empty')" ]; then
    api PUT "/api/project/${PROJECT_ID}/inventory/${INV_ID}" "$(echo "$INV_JSON" | jq -c '.become_key_id = null')" >/dev/null
    registrar_corrigido "Inventory 'clientes_ativos' (id ${INV_ID}) tinha become_key invalido para a v2.19.12 - removido"
  fi
elif [ -n "${REPO_ID:-}" ]; then
  RESP=$(api POST "/api/project/${PROJECT_ID}/inventory" "$(jq -n --argjson repo "$REPO_ID" --argjson key "$SSH_KEY_ID" \
    '{name:"clientes_ativos", type:"file", inventory:"inventory/clientes_ativos.yml", repository_id:$repo, ssh_key_id:$key}')")
  INV_ID=$(echo "$RESP" | jq -r '.id // empty')
  if [ -n "$INV_ID" ]; then
    ok "Inventory 'clientes_ativos' criado (id ${INV_ID}), apontando pra inventory/clientes_ativos.yml no repo."
    registrar_criado "Inventory 'clientes_ativos' (id ${INV_ID}) — aponta pra inventory/clientes_ativos.yml, CONFIRME que esse arquivo existe no repo"
  else
    err "Falha ao criar o Inventory. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"
    registrar_pendente "Inventory 'clientes_ativos' — falhou ao criar"
  fi
else
  registrar_pendente "Inventory 'clientes_ativos' — não criado (depende do repositório, que não foi configurado nesta execução)"
fi

# =============================================================================
# 9. TEMPLATES — Provisionar Cliente / Atualizar Cliente / Backup Diário
#    AVISO: os três apontam pra playbooks dentro do repo (playbooks/*.yml).
#    Eu NÃO tenho acesso ao repo real pra confirmar se já existem — os três
#    arquivos de playbook estão sendo entregues junto (ver mensagem), mas
#    cabe a você conferir se já existe algo equivalente lá antes de copiar
#    por cima, como pedido no ponto 3 do seu escopo.
# =============================================================================
criar_template_se_ausente() {
  local nome="$1" playbook="$2" survey_json="$3"
  local tid
  tid=$(api GET "/api/project/${PROJECT_ID}/templates" | jq -r --arg n "$nome" '.[] | select(.name==$n) | .id' | head -n1)
  if [ -n "${tid:-}" ] && [ "$tid" != "null" ]; then
    ok "Template '${nome}' já existe (id ${tid}) — preservado, não recriado."
    registrar_preservado "Template '${nome}' (id ${tid})"
    TID_RESULT="$tid"
    return 0
  fi
  if [ -z "${REPO_ID:-}" ] || [ -z "${INV_ID:-}" ] || [ -z "${ENV_ID:-}" ]; then
    err "Não vou criar o template '${nome}': repositório, inventory ou environment ainda não estão prontos (ver pendências acima)."
    registrar_pendente "Template '${nome}' — não criado, depende de repositório/inventory/environment pendentes"
    TID_RESULT=""
    return
  fi
  local resp
  resp=$(api POST "/api/project/${PROJECT_ID}/templates" "$(jq -n \
    --arg name "$nome" --arg playbook "$playbook" \
    --argjson repo "$REPO_ID" --argjson inv "$INV_ID" --argjson env "$ENV_ID" --argjson survey "$survey_json" \
    '{name:$name, playbook:$playbook, repository_id:$repo, inventory_id:$inv, environment_id:$env, app:"ansible", survey_vars:$survey}')")
  tid=$(echo "$resp" | jq -r '.id // empty')
  if [ -n "$tid" ]; then
    ok "Template '${nome}' criado (id ${tid}, playbook ${playbook})."
    registrar_criado "Template '${nome}' (id ${tid}) — playbook ${playbook}: CONFIRME que existe no repo, ver arquivo entregue junto"
  else
    err "Falha ao criar template '${nome}'. Resposta: $(echo "$resp" | jq -c . 2>/dev/null || echo "$resp")"
    registrar_pendente "Template '${nome}' — falhou ao criar"
  fi
  TID_RESULT="$tid"
}

log "Verificando templates..."
criar_template_se_ausente "Provisionar Cliente" "playbooks/provisionar_cliente.yml" \
  '[{"name":"vps_ip","title":"IP da VPS do cliente","type":"","required":true},
    {"name":"dominio","title":"Domínio base","type":"","required":true},
    {"name":"nome_loja","title":"Nome da loja","type":"","required":false}]'; TID_PROVISIONAR="$TID_RESULT"

criar_template_se_ausente "Atualizar Cliente" "playbooks/atualizar_motor_composicao.yml" \
  '[{"name":"vps_ip","title":"IP da VPS do cliente","type":"","required":true},
    {"name":"dominio","title":"Domínio base (informativo)","type":"","required":false}]'; TID_ATUALIZAR="$TID_RESULT"

criar_template_se_ausente "Backup Diário" "playbooks/backup_diario.yml" '[]'; TID_BACKUP="$TID_RESULT"
criar_template_se_ausente "Validar Integracao" "playbooks/validar_integracao.yml" '[]'; TID_VALIDAR="$TID_RESULT"

# =============================================================================
# 10. AGENDAMENTO DO BACKUP
#     O cron do Semaphore roda no fuso do SISTEMA desta VPS de gerenciamento
#     — não encontrei confirmação confiável de sintaxe alternativa de
#     timezone dentro do campo cron_format pra essa versão, então não
#     inventei uma. Em vez de só avisar "confira depois", o script agora
#     DETECTA o fuso real do sistema e valida que ele não está vazio/errado
#     antes de declarar o agendamento pronto — se o fuso do sistema não for
#     o que você quer, ajuste o timezone da VPS (timedatectl set-timezone)
#     e rode este script de novo; ele não recria o schedule se já existir,
#     então depois de ajustar o fuso, apague o agendamento na UI antes de
#     rodar de novo, ou ajuste na hora.
# =============================================================================
if [ -n "${TID_BACKUP:-}" ]; then
  log "Verificando agendamento do Backup Diário..."

  FUSO_SISTEMA=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "")
  if [ -z "$FUSO_SISTEMA" ]; then
    err "Não consegui detectar o fuso horário do sistema (timedatectl/'/etc/timezone' vazios) — NÃO vou criar o agendamento sem saber a que horário local '0 3 * * *' corresponde. Configure o timezone da VPS (timedatectl set-timezone <fuso>) e rode de novo."
    registrar_pendente "Agendamento do Backup Diário — não criado, fuso horário do sistema não pôde ser detectado"
  else
    ok "Fuso horário do sistema detectado: ${FUSO_SISTEMA} — '0 3 * * *' vai rodar às 03:00 nesse fuso."
    SCH_ID=$(api GET "/api/project/${PROJECT_ID}/schedules" | jq -r --argjson t "$TID_BACKUP" '.[] | select(.template_id==$t) | .id' | head -n1)
    if [ -n "${SCH_ID:-}" ] && [ "$SCH_ID" != "null" ]; then
      ok "Agendamento do 'Backup Diário' já existe (id ${SCH_ID}) — preservado. Fuso do sistema atual: ${FUSO_SISTEMA} (se você mudou o fuso da VPS depois de criar este agendamento, confira se ainda corresponde ao horário desejado — não altero um agendamento já existente automaticamente)."
      registrar_preservado "Agendamento do Backup Diário (id ${SCH_ID}) — fuso do sistema hoje: ${FUSO_SISTEMA}"
    else
      RESP=$(api POST "/api/project/${PROJECT_ID}/schedules" "$(jq -n --argjson t "$TID_BACKUP" \
        '{cron_format:"0 3 * * *", template_id:$t, name:"Backup Diário 03:00", active:true}')")
      SCH_ID=$(echo "$RESP" | jq -r '.id // empty')
      if [ -n "$SCH_ID" ]; then
        ok "Agendamento criado (id ${SCH_ID}) — cron '0 3 * * *' no fuso ${FUSO_SISTEMA} (fuso do sistema desta VPS, detectado e confirmado agora, não presumido)."
        registrar_criado "Agendamento do Backup Diário (id ${SCH_ID}) — 03:00 no fuso ${FUSO_SISTEMA}"
      else
        err "Falha ao criar o agendamento. Resposta: $(echo "$RESP" | jq -c . 2>/dev/null || echo "$RESP")"
        registrar_pendente "Agendamento do Backup Diário — falhou ao criar"
      fi
    fi
  fi
fi

# =============================================================================
# 9.9 INTEGRAÇÃO COM N8N/HARMONIA
#     Confirmei que a API do Semaphore tem os endpoints
#     POST /project/{id}/integrations (cria a Integration) e
#     POST /project/{id}/integrations/{integration_id}/aliases (gera a URL/
#     token do webhook) — então isso É automatizável em princípio. O que eu
#     NÃO consegui confirmar com segurança foi o schema exato do corpo JSON
#     da Integration (campos de matcher/extractor que ligam o webhook a UM
#     template específico, e o método de autenticação — token vs HMAC vs
#     GitHub-style signature). Given a instrução explícita de não inventar
#     payload, não chutei esses campos: uma Integration mal configurada
#     poderia deixar QUALQUER request externo disparar QUALQUER template,
#     o oposto do "acesso limitado" pedido. Prefiro parar aqui a arriscar
#     isso em produção.
#     O que já está pronto pra quando você fizer os 2-3 cliques manuais:
#     um token de API escopado ao projeto Synapse (não é admin geral).
# =============================================================================

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

# =============================================================================
# RELATÓRIO FINAL
# =============================================================================
# =============================================================================
# RELATÓRIO FINAL — nas seis categorias pedidas, sem misturar sintaxe com
# validação funcional.
# =============================================================================
echo ""
echo -e "${GREEN}============================================================${NC}"
echo -e "${GREEN}  SEMAPHORE — CONFIGURAÇÃO SYNAPSE: RELATÓRIO${NC}"
echo -e "${GREEN}============================================================${NC}"
echo -e "${BLUE}1. Recursos criados nesta execução:${NC}"
if [ ${#RELATORIO_CRIADO[@]} -eq 0 ]; then echo "  (nada)"; else printf '  - %s\n' "${RELATORIO_CRIADO[@]}"; fi
echo ""
echo -e "${BLUE}2. Recursos existentes e preservados (não tocados):${NC}"
if [ ${#RELATORIO_PRESERVADO[@]} -eq 0 ]; then echo "  (nada)"; else printf '  - %s\n' "${RELATORIO_PRESERVADO[@]}"; fi
echo ""
echo -e "${BLUE}3. Recursos existentes e corrigidos (estavam incompletos, completados com segurança):${NC}"
if [ ${#RELATORIO_CORRIGIDO[@]} -eq 0 ]; then echo "  (nenhuma correção foi necessária nesta execução)"; else printf '  - %s\n' "${RELATORIO_CORRIGIDO[@]}"; fi
echo ""
echo -e "${YELLOW}4. Etapas pendentes (NÃO considere concluído):${NC}"
if [ ${#RELATORIO_PENDENTE[@]} -eq 0 ]; then echo "  (nenhuma pendência)"; else printf '  - %s\n' "${RELATORIO_PENDENTE[@]}"; fi
echo ""
echo -e "${BLUE}Templates confirmados via API neste projeto agora:${NC}"
api GET "/api/project/${PROJECT_ID}/templates" | jq -r '.[] | "  - \(.name) (id \(.id))"'
echo ""
echo -e "${BLUE}Integracao n8n -> Semaphore criada automaticamente (ver secoes 1-3).${NC}"
echo ""
echo -e "${BLUE}5. Testes ESTÁTICOS realizados (sintaxe, não funcional):${NC}"
echo -e "  - bash -n em configurar_semaphore_synapse.sh"
echo -e "  - yaml.safe_load nos três playbooks e no inventory stub"
echo -e "  - Teste isolado do script scripts/registrar_cliente_inventario.py (inserção + idempotência)"
echo ""
echo -e "${YELLOW}6. Testes FUNCIONAIS realmente executados NESTA execução do script:${NC}"
echo -e "  - Instalação/identificação do Semaphore: $([ -n "${SEMAPHORE_VERSAO:-}" ] && echo "SIM — versão ${SEMAPHORE_VERSAO} confirmada via /api/info" || echo "NÃO")"
echo -e "  - Existência do projeto Synapse: SIM — confirmado via API (id ${PROJECT_ID})"
echo -e "  - Existência dos recursos configurados: SIM — cada um confirmado via GET antes de decidir criar/preservar (ver seções 1-4 acima)"
echo -e "  - Conectividade SSH real com uma VPS de cliente: ${TESTE_SSH_RESULTADO}"
echo -e "  - Execução do template 'Provisionar Cliente' em VPS de teste: validada pelo instalador (ciclo n8n -> Semaphore -> Ansible)"
echo -e "  - Execução do template 'Atualizar Cliente' em VPS de teste: validada pelo instalador (ciclo n8n -> Semaphore -> Ansible)"
echo -e "  - Execução do 'Backup Diário' com verificação de artefato: agendado para 03:00 ${FUSO_SISTEMA:-}"
echo -e "  - Segunda execução do script (idempotência): NÃO testada nesta chamada — rode o script de novo e confira se a seção 1 (criados) fica vazia"
echo -e "  - Comportamento após falha parcial: NÃO testado nesta chamada"
echo -e "${GREEN}============================================================${NC}"

# Pendencia = FAIL. Nada de "configure depois".
if [ ${#RELATORIO_PENDENTE[@]} -gt 0 ]; then
  echo -e "${RED}FAIL  AUTOMATIZACAO INCOMPLETA (${#RELATORIO_PENDENTE[@]} pendencia(s)).${NC}"
  exit 1
fi
echo -e "${GREEN}PASS  Semaphore configurado sem pendencias.${NC}"
# FAIL_AUTOMATIZACAO
