const test = require('node:test'); const assert = require('node:assert');
const { autenticar, validarContrato } = require('../provisionar_logic.js');
test('sem token -> 401', () => assert.strictEqual(autenticar({}, 'x').status, 401));
test('token errado -> 401', () => assert.strictEqual(autenticar({ 'x-synapse-provision-token': 'y' }, 'x').status, 401));
test('token certo', () => assert.strictEqual(autenticar({ 'x-synapse-provision-token': 'x' }, 'x').ok, true));
test('servidor sem token configurado nunca autentica', () => assert.strictEqual(autenticar({ 'x-synapse-provision-token': '' }, '').ok, false));
test('contrato valido: so os 3 parametros consumidos seguem', () => {
  const r = validarContrato({ operacao: 'provisionar', vps_ip: '203.0.113.10', dominio: 'LojaDemo.com.br', nome_loja: 'Loja Demo', extra: 'x' });
  assert.deepStrictEqual(r.payload, { vps_ip: '203.0.113.10', dominio: 'lojademo.com.br', nome_loja: 'Loja Demo' });
});
test('nome_loja default = dominio', () => assert.strictEqual(validarContrato({ operacao: 'validar', vps_ip: '10.0.0.1', dominio: 'a.com' }).payload.nome_loja, 'a.com'));
for (const [k, v] of [['operacao', 'apagar'], ['vps_ip', '999.1.1.1'], ['vps_ip', '1.2.3'], ['dominio', 'semponto'], ['dominio', 'a b.com'], ['dominio', 'a.com;rm -rf /']]) {
  test(`rejeita ${k}=${v}`, () => assert.strictEqual(validarContrato({ operacao: 'provisionar', vps_ip: '1.2.3.4', dominio: 'a.com', [k]: v }).ok, false));
}
test('nome_loja com aspas/$/crase e rejeitado (vai a linha de comando do playbook)', () => {
  for (const n of ['a"b', 'a$b', 'a`b', 'a\\b']) assert.strictEqual(validarContrato({ operacao: 'provisionar', vps_ip: '1.2.3.4', dominio: 'a.com', nome_loja: n }).ok, false);
});
