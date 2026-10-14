#!/bin/bash
# =============================================================================
#  SYNAPSE — APLICAR MOTOR DE COMPOSIÇÃO (caminho isolado p/ cliente EXISTENTE)
#
#  Este script existe especificamente para o caso que o instalador completo
#  (provisionar_cliente_synapse.sh) NÃO cobre com segurança: adicionar (ou
#  confirmar) o Motor de Composição — DocTypes "Synapse Composicao(*)" e
#  "Synapse Producao Item" + o campo Item.custom_composicao — num cliente
#  que JÁ foi provisionado e está em uso.
#
#  Diferente do instalador completo, este script:
#    - NÃO gera nem altera SENHA nenhuma.
#    - NÃO grava nem zera "/root/synapse_credentials.txt" (só LÊ o
#      DOMINIO_BASE de lá).
#    - NÃO cria nem rotaciona a api_key do usuário de automação.
#    - NÃO roda nenhuma outra fase (Docker, Traefik, N8N, Harpa, painel,
#      Lyra, DNS, etc.) — só a seção equivalente à 5.5 do instalador.
#  É seguro rodar em qualquer cliente já provisionado, quantas vezes for
#  preciso (idempotente objeto a objeto: só cria o que ainda não existe).
#
#  Uso, como root, na VPS do cliente (precisa já ter sido provisionado por
#  provisionar_cliente_synapse.sh, ou ter "/root/synapse_credentials.txt"
#  com DOMINIO_BASE preenchido):
#    sudo bash aplicar_motor_composicao.sh
# =============================================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()   { echo -e "[INFO] $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
err()   { echo -e "${RED}[ERRO]${NC} $1"; }
fatal() { echo -e "${RED}[FATAL]${NC} $1"; exit 1; }

CRED_FILE="/root/synapse_credentials.txt"
[ -f "$CRED_FILE" ] || fatal "Não achei ${CRED_FILE}. Esse é o arquivo gravado pelo provisionar_cliente_synapse.sh — confirme se esta VPS já foi provisionada com ele antes de rodar este script."

DOMINIO_BASE=$(grep '^DOMINIO_BASE=' "$CRED_FILE" | cut -d'=' -f2)
[ -n "$DOMINIO_BASE" ] || fatal "Não achei a chave DOMINIO_BASE dentro de ${CRED_FILE}. Confira o conteúdo do arquivo manualmente antes de rodar de novo."
DOMINIO_ERP="ritmo.${DOMINIO_BASE}"
log "Domínio detectado: ${DOMINIO_BASE} (site Ritmo: ${DOMINIO_ERP})"

if ! docker exec ritmo-backend-1 bash -c "cd /home/frappe/frappe-bench && bench --site ${DOMINIO_ERP} list-apps" >/dev/null 2>&1; then
  fatal "O site ${DOMINIO_ERP} não respondeu dentro do container ritmo-backend-1. Confirme se o domínio está certo e se o container está de pé (docker ps)."
fi

# Idêntica à do instalador completo, incluindo a correção do código de
# saída: o "return" devolve o status real do "bench execute", não o da
# limpeza do arquivo temporário (ver comentário completo na versão dentro
# de provisionar_cliente_synapse.sh).
ritmo_exec() {
  local tmp="/tmp/synapse_ritmo_$$_${RANDOM}.py"
  cat > "$tmp"
  local mod; mod="$(basename "$tmp" .py)"
  docker cp "$tmp" "ritmo-backend-1:/home/frappe/frappe-bench/apps/frappe/frappe/${mod}.py" 2>/dev/null
  docker exec -u frappe ritmo-backend-1 bash -c "cd /home/frappe/frappe-bench && bench --site ${DOMINIO_ERP} execute frappe.${mod}.main" 2>&1
  local rc=$?
  docker exec -u frappe ritmo-backend-1 rm -f "/home/frappe/frappe-bench/apps/frappe/frappe/${mod}.py" 2>/dev/null || true
  rm -f "$tmp"
  return "$rc"
}

RESULT_COMPOSICAO=$(ritmo_exec <<'PYEOF'
def main():
    import frappe

    if not frappe.db.exists("Module Def", "Synapse"):
        frappe.get_doc({"doctype": "Module Def", "module_name": "Synapse", "app_name": "frappe", "custom": 1}).insert(ignore_permissions=True)

    # ---- 1) Synapse Composicao Opcao (child table, sem dependência) ----
    if not frappe.db.exists("DocType", "Synapse Composicao Opcao"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Synapse Composicao Opcao", "module": "Synapse",
            "custom": 1, "istable": 1,
            "fields": [
                {"fieldname": "nome", "label": "Nome", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
                {"fieldname": "preco", "label": "Ajuste de Preço", "fieldtype": "Currency", "default": "0", "in_list_view": 1},
            ]
        }).insert(ignore_permissions=True)
        print("criado: Synapse Composicao Opcao")
    else:
        print("ja existia: Synapse Composicao Opcao")

    # ---- 2) Synapse Composicao Grupo (child table; composicao_filha vem depois, quebra de dependência circular) ----
    if not frappe.db.exists("DocType", "Synapse Composicao Grupo"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Synapse Composicao Grupo", "module": "Synapse",
            "custom": 1, "istable": 1,
            "fields": [
                {"fieldname": "nome", "label": "Nome", "fieldtype": "Data", "reqd": 1, "in_list_view": 1},
                {"fieldname": "min", "label": "Mínimo", "fieldtype": "Int", "default": "0", "in_list_view": 1},
                {"fieldname": "max", "label": "Máximo", "fieldtype": "Int", "default": "1", "in_list_view": 1},
                {"fieldname": "regra", "label": "Regra", "fieldtype": "Select", "options": "soma\nproporcional", "default": "soma"},
                {"fieldname": "opcoes", "label": "Opções", "fieldtype": "Table", "options": "Synapse Composicao Opcao"},
            ]
        }).insert(ignore_permissions=True)
        print("criado: Synapse Composicao Grupo")
    else:
        print("ja existia: Synapse Composicao Grupo")

    # ---- 3) Synapse Composicao (pai) ----
    if not frappe.db.exists("DocType", "Synapse Composicao"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Synapse Composicao", "module": "Synapse",
            "custom": 1, "istable": 0, "autoname": "field:titulo",
            "fields": [
                {"fieldname": "titulo", "label": "Título", "fieldtype": "Data", "reqd": 1, "unique": 1, "in_list_view": 1},
                {"fieldname": "grupos", "label": "Grupos", "fieldtype": "Table", "options": "Synapse Composicao Grupo"},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Synapse Lojista", "read": 1, "write": 1, "create": 1},
            ]
        }).insert(ignore_permissions=True)
        print("criado: Synapse Composicao")
    else:
        print("ja existia: Synapse Composicao")

    # ---- 4) fecha a recursão: adiciona composicao_filha no Grupo agora que Composicao existe ----
    grupo_doc = frappe.get_doc("DocType", "Synapse Composicao Grupo")
    if not any(f.fieldname == "composicao_filha" for f in grupo_doc.fields):
        grupo_doc.append("fields", {
            "fieldname": "composicao_filha", "label": "Composição Filha (recursão)",
            "fieldtype": "Link", "options": "Synapse Composicao",
            "insert_after": "regra"
        })
        grupo_doc.save(ignore_permissions=True)
        print("campo composicao_filha adicionado em Synapse Composicao Grupo")
    else:
        print("composicao_filha ja existia em Synapse Composicao Grupo")

    # ---- 5) Custom Field no Item ----
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
    create_custom_fields({
        "Item": [
            {"fieldname": "custom_composicao", "label": "Composição (Motor)", "fieldtype": "Link",
             "options": "Synapse Composicao", "insert_after": "item_group"},
        ]
    }, ignore_validate=True)
    print("custom_composicao garantido em Item")

    # ---- 6) Synapse Producao Item ----
    if not frappe.db.exists("DocType", "Synapse Producao Item"):
        frappe.get_doc({
            "doctype": "DocType", "name": "Synapse Producao Item", "module": "Synapse",
            "custom": 1, "istable": 0, "autoname": "field:synapse_op_id",
            "track_changes": 1,
            "fields": [
                {"fieldname": "synapse_op_id", "label": "Op ID (idempotência)", "fieldtype": "Data", "reqd": 1, "unique": 1},
                {"fieldname": "pos_invoice", "label": "POS Invoice", "fieldtype": "Link", "options": "POS Invoice", "in_list_view": 1},
                {"fieldname": "item_code", "label": "Item", "fieldtype": "Link", "options": "Item", "in_list_view": 1},
                {"fieldname": "frase", "label": "Descrição", "fieldtype": "Data", "in_list_view": 1},
                {"fieldname": "preenchimentos_json", "label": "Escolhas (JSON)", "fieldtype": "Long Text"},
                {"fieldname": "alerta", "label": "Alerta Cozinha", "fieldtype": "Check", "default": "0"},
                {"fieldname": "status_producao", "label": "Status", "fieldtype": "Select",
                 "options": "Pendente\nEm Preparo\nPronto\nEnviado\nEntregue\nCancelado",
                 "default": "Pendente", "in_list_view": 1},
            ],
            "permissions": [
                {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
                {"role": "Synapse Lojista", "read": 1, "write": 1, "create": 1},
                {"role": "Synapse Funcionário", "read": 1, "write": 1},
                {"role": "Synapse Automação", "read": 1, "write": 1, "create": 1},
            ]
        }).insert(ignore_permissions=True)
        print("criado: Synapse Producao Item")
    else:
        print("ja existia: Synapse Producao Item")

    frappe.db.commit()
PYEOF
)
RC_CRIACAO=$?
echo "$RESULT_COMPOSICAO"

if [ "$RC_CRIACAO" -ne 0 ] || echo "$RESULT_COMPOSICAO" | grep -qiE "Traceback|Error|Exception"; then
  fatal "A execução no Ritmo falhou (bench execute retornou código ${RC_CRIACAO}). Veja a saída acima. Nenhuma confirmação de sucesso será dada — corrija o problema e rode de novo (este script é seguro de reaplicar, objeto a objeto)."
fi

docker exec ritmo-backend-1 bash -c "cd /home/frappe/frappe-bench && bench --site ${DOMINIO_ERP} clear-cache"

# ---- Verificação final: exige as 5 confirmações (4 DocTypes + 1 Custom
#      Field). Consulta vazia, incompleta, ou que não bata com o esperado
#      NUNCA é lida como sucesso. ----
log "Confirmando no Ritmo que os 5 objetos do Motor de Composição realmente existem..."
VERIFICACAO=$(ritmo_exec <<'PYEOF'
def main():
    import frappe
    esperado = [
        "Synapse Composicao Opcao",
        "Synapse Composicao Grupo",
        "Synapse Composicao",
        "Synapse Producao Item",
    ]
    for nome in esperado:
        print(f"SYNAPSE_CHECK|{nome}|{1 if frappe.db.exists('DocType', nome) else 0}")
    tem_campo = frappe.db.exists("Custom Field", {"dt": "Item", "fieldname": "custom_composicao"})
    print(f"SYNAPSE_CHECK|Item.custom_composicao|{1 if tem_campo else 0}")
PYEOF
)
RC_VERIFICACAO=$?
echo "$VERIFICACAO"

ESPERADOS=5
LINHAS=$(echo "$VERIFICACAO" | grep -c '^SYNAPSE_CHECK|')
CONFIRMADOS=0
while IFS='|' read -r _ nome existe; do
  [ -z "$nome" ] && continue
  if [ "$existe" = "1" ]; then
    ok "confirmado: ${nome}"
    CONFIRMADOS=$((CONFIRMADOS + 1))
  else
    err "NÃO encontrado: ${nome}"
  fi
done < <(echo "$VERIFICACAO" | grep '^SYNAPSE_CHECK|')

if [ "$RC_VERIFICACAO" -ne 0 ]; then
  fatal "A consulta de verificação falhou (bench execute retornou código ${RC_VERIFICACAO}). Sem confirmação — trate como FALHA e investigue antes de considerar concluído."
fi
if [ "$LINHAS" -ne "$ESPERADOS" ]; then
  fatal "A verificação deveria retornar ${ESPERADOS} resultados e retornou ${LINHAS}. A consulta pode ter parado no meio. Sem confirmação — trate como FALHA."
fi
if [ "$CONFIRMADOS" -ne "$ESPERADOS" ]; then
  fatal "Só ${CONFIRMADOS} de ${ESPERADOS} objetos foram confirmados. Trate como FALHA — revise a saída acima antes de considerar isto concluído."
fi

ok "[FIM] Motor de Composição confirmado 5/5 em ${DOMINIO_ERP}."
