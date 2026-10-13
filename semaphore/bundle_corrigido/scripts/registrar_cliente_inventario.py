#!/usr/bin/env python3
"""
Insere (ou atualiza) um host no grupo 'clientes_ativos' de um inventário
Ansible no formato usado por inventory/clientes_ativos.yml, de forma
idempotente. Chamado pelo playbook playbooks/provisionar_cliente.yml —
deliberadamente separado do YAML do playbook pra não misturar Jinja com
aspas de Python (fonte de bugs difíceis de depurar).

Uso:
    registrar_cliente_inventario.py <caminho_yaml> <dominio> <vps_ip> <nome_loja> [timestamp_iso8601]

Saída (stdout), pra o playbook decidir se precisa commitar:
    ADICIONADO   -> o host não existia e foi inserido/alterado
    JA_EXISTIA   -> o host já estava lá com os mesmos dados, nada mudou
Código de saída != 0 em qualquer erro (arquivo ausente, YAML inválido, etc.)
— nunca falha silenciosamente.
"""
import sys
import yaml


def main():
    if len(sys.argv) < 4:
        print("uso: registrar_cliente_inventario.py <yaml> <dominio> <vps_ip> <nome_loja> [timestamp]", file=sys.stderr)
        sys.exit(2)

    caminho, dominio, vps_ip, nome_loja = sys.argv[1:5]
    timestamp = sys.argv[5] if len(sys.argv) > 5 else "desconhecido"

    with open(caminho) as f:
        inv = yaml.safe_load(f) or {}

    inv.setdefault("all", {}).setdefault("children", {})
    inv["all"]["children"].setdefault("clientes_ativos", {}).setdefault("hosts", {})
    inv["all"]["children"].setdefault("clientes_inativos", {}).setdefault("hosts", {})

    hosts = inv["all"]["children"]["clientes_ativos"]["hosts"]
    novo_registro = {
        "ansible_host": vps_ip,
        "nome_loja": nome_loja,
        "provisionado_em": timestamp,
    }

    if hosts.get(dominio) == novo_registro:
        print("JA_EXISTIA")
        return

    hosts[dominio] = novo_registro

    with open(caminho, "w") as f:
        yaml.safe_dump(inv, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print("ADICIONADO")


if __name__ == "__main__":
    main()
