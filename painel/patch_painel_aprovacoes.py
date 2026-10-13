#!/usr/bin/env python3
"""Adiciona a aba 'Aprovacoes' (Aprovar/Rejeitar) ao Painel oficial (nomen-me/painel-synapse).
Uso: patch_painel_aprovacoes.py <index.html de entrada> <saida>   Toda ancora e verificada: falta => aborta."""
import sys, os, re
here = os.path.dirname(os.path.abspath(__file__))
src = open(sys.argv[1], encoding='utf-8').read()
if 'page-aprovacoes' in src:
    open(sys.argv[2], 'w', encoding='utf-8').write(src); print("ja aplicado"); sys.exit(0)
js, page, tab = (open(os.path.join(here, f), encoding='utf-8').read() for f in ('aprov.js.txt', 'aprov.page.txt', 'aprov.tab.txt'))
def once(s, old, new, what):
    if s.count(old) != 1: sys.exit(f"ANCORA '{what}': esperado 1, achei {s.count(old)}")
    return s.replace(old, new, 1)
s = src
s = once(s, '      <div class="htab" data-page="cadastros"', tab + '      <div class="htab" data-page="cadastros"', 'aba cadastros')
s = once(s, '    <div class="page" id="page-cadastros">', page + '    <div class="page" id="page-cadastros">', 'pagina cadastros')
s = once(s, "const ABAS_DA_MATRIZ = ['dashboard','pedidos','atendimento','cadastros'", "const ABAS_DA_MATRIZ = ['dashboard','pedidos','atendimento','aprovacoes','cadastros'", 'ABAS_DA_MATRIZ')
s = once(s, "'Lojista':          ['dashboard','pedidos','atendimento','cadastros'", "'Lojista':          ['dashboard','pedidos','atendimento','aprovacoes','cadastros'", 'perfil Lojista')
s = once(s, "'Gerente':          ['dashboard','pedidos','atendimento','cadastros'", "'Gerente':          ['dashboard','pedidos','atendimento','aprovacoes','cadastros'", 'perfil Gerente')
s = once(s, "  if (pagina === 'consumo'){\n    renderConsumo();\n  }", "  if (pagina === 'aprovacoes'){\n    carregarAprovacoes();\n  }\n  if (pagina === 'consumo'){\n    renderConsumo();\n  }", 'nav aprovacoes')
s = once(s, "function nav(pagina, el){", js + "\nfunction nav(pagina, el){", 'funcao nav')
open(sys.argv[2], 'w', encoding='utf-8').write(s); print("OK", len(src), "->", len(s))
