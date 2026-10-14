const test = require('node:test'); const assert = require('node:assert');
const { validate, gate } = require('../cancao_logic.js');
const base = () => ({ event_id: 'e1', seq: 1, root_event_id: 'e1', chain_id: 'c', origin_actor: 'HUMAN', entity_type: 'Item', entity_id: 'X', event_type: 'item.stock_changed', state_version: 3, event_timestamp: '2026-09-29 10:00:00', causal_depth: 0, schema_version: '1', payload: {} });
test('envelope valido passa', () => assert.deepStrictEqual(validate(base()), { ok: true }));
for (const k of ['event_id', 'seq', 'chain_id', 'state_version', 'schema_version']) {
  test('campo ausente ' + k, () => { const e = base(); delete e[k]; assert.strictEqual(validate(e).ok, false); });
}
test('schema desconhecido', () => assert.strictEqual(validate({ ...base(), schema_version: '99' }).ok, false));
test('ator invalido', () => assert.strictEqual(validate({ ...base(), origin_actor: 'ROBO' }).ok, false));
test('payload corrompido', () => assert.match(validate({ ...base(), payload: { __unparseable__: true } }).reason, /corrompido/));
test('gate: estoque agrega', () => assert.deepStrictEqual(gate(base()), { action: 'AGGREGATE', agg_key: 'item.stock_changed|Item|X' }));
test('gate: conflito vai para a Lyra', () => assert.strictEqual(gate({ ...base(), event_type: 'concurrency_conflict' }).action, 'FORWARD'));
test('gate: snapshot ignorado', () => assert.strictEqual(gate({ ...base(), event_type: 'state_snapshot' }).action, 'IGNORE'));
test('gate: evento originado pela LYRA NAO e suprimido', () => assert.strictEqual(gate({ ...base(), origin_actor: 'LYRA', event_type: 'sales_order.submitted' }).action, 'FORWARD'));
test('gate: evento DESCONHECIDO nao vai a Lyra (registrado/sinalizado)', () => {
  for (const t of ['algo.novo', 'payment_entry.created', 'delivery_note.updated', 'sales_invoice.created']) assert.deepStrictEqual(gate({ ...base(), event_type: t }), { action: 'UNKNOWN', agg_key: null });
});
test('gate: estoque, preco e item sao agregados (contrato registrado)', () => {
  for (const t of ['item.stock_changed', 'item.price_changed', 'item.updated']) assert.strictEqual(gate({ ...base(), event_type: t }).action, 'AGGREGATE');
});
test('gate: nao depende de valor monetario', () => {
  const a = gate({ ...base(), event_type: 'sales_order.created', payload: { grand_total: 1 } });
  const b = gate({ ...base(), event_type: 'sales_order.created', payload: { grand_total: 1e6 } });
  assert.deepStrictEqual(a, b);
});
