// Logica deterministica da Harmonia (Ordem Final secoes 8, 10, 13, 14). Sem I/O.
// Este arquivo e a UNICA fonte: build_workflows.py o embute nos nos Code do n8n e os testes o importam.
const SUPPORTED_SCHEMAS = ['1'];
const ORIGIN_ACTORS = ['HUMAN', 'LYRA', 'SYSTEM'];
const REQUIRED = ['event_id', 'seq', 'root_event_id', 'chain_id', 'origin_actor', 'entity_type', 'entity_id', 'event_type', 'state_version', 'event_timestamp', 'causal_depth', 'schema_version'];

// Politica do gate: CONTRATO EXPLICITO por natureza de evento (nao por valor monetario).
// Evento fora de qualquer lista => UNKNOWN: registrado e sinalizado, NUNCA encaminhado a Lyra nem executado.
const GATE_POLICY = {
  // Eventos de controle do ciclo (respostas do Ritmo a operacoes) - a Lyra precisa reagir a eles.
  FORWARD_CONTROL: ['concurrency_conflict', 'context_changed', 'intent_expired', 'approval_requested', 'approval_rejected', 'operation_failed', 'operation_unauthorized'],
  // Eventos de negocio com contrato de decisao (acoes suportadas pela camada de execucao: criar/confirmar/cancelar venda).
  FORWARD_BUSINESS: ['sales_order.created', 'sales_order.submitted', 'sales_order.cancelled'],
  IGNORE_TYPES: ['state_snapshot', 'synapse.state_changed', 'operation_held'],
  AGGREGATE_TYPES: ['item.stock_changed', 'item.price_changed', 'item.updated', 'customer.updated'],
};

function validate(env) {
  if (!env || typeof env !== 'object') return { ok: false, reason: 'envelope ausente' };
  for (const k of REQUIRED) {
    if (env[k] === undefined || env[k] === null || env[k] === '') return { ok: false, reason: 'campo obrigatorio ausente: ' + k };
  }
  if (!SUPPORTED_SCHEMAS.includes(String(env.schema_version))) return { ok: false, reason: 'schema_version nao suportada: ' + env.schema_version };
  if (!ORIGIN_ACTORS.includes(env.origin_actor)) return { ok: false, reason: 'origin_actor invalido: ' + env.origin_actor };
  if (env.payload && env.payload.__unparseable__) return { ok: false, reason: 'payload corrompido (JSON invalido)' };
  if (!Number.isInteger(env.seq) || !Number.isInteger(env.state_version)) return { ok: false, reason: 'seq/state_version nao inteiros' };
  return { ok: true };
}

// IGNORE | AGGREGATE | FORWARD | UNKNOWN. origin_actor=LYRA NAO e motivo de supressao: o controle e causal (profundidade).
function gate(env) {
  const t = env.event_type;
  if (GATE_POLICY.FORWARD_CONTROL.includes(t) || GATE_POLICY.FORWARD_BUSINESS.includes(t)) return { action: 'FORWARD', agg_key: null };
  if (GATE_POLICY.IGNORE_TYPES.includes(t)) return { action: 'IGNORE', agg_key: null };
  if (GATE_POLICY.AGGREGATE_TYPES.includes(t)) return { action: 'AGGREGATE', agg_key: aggKey(env) };
  return { action: 'UNKNOWN', agg_key: null };
}

function aggKey(env) { return env.event_type + '|' + env.entity_type + '|' + env.entity_id; }

module.exports = { validate, gate, aggKey, GATE_POLICY, SUPPORTED_SCHEMAS, REQUIRED };
