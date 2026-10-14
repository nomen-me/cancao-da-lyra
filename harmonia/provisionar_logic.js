// Contrato n8n -> Semaphore (Integration). Funcao pura: mesma fonte do nó Code do workflow e dos testes.
// Parametros enviados ao Semaphore = EXATAMENTE os consumidos pelos playbooks: vps_ip, dominio, nome_loja.
const OPERACOES = ['provisionar', 'validar'];
const RE_IPV4 = /^((25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])$/;
const RE_FQDN = /^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

function autenticar(headers, esperado) {
  const h = headers || {};
  const recebido = h['x-synapse-provision-token'];
  if (!esperado) return { ok: false, status: 500, reason: 'SYNAPSE_PROVISION_TOKEN nao configurado no Harmonia' };
  if (!recebido || recebido !== esperado) return { ok: false, status: 401, reason: 'token invalido' };
  return { ok: true };
}

function validarContrato(body) {
  const b = body || {};
  if (!OPERACOES.includes(b.operacao)) return { ok: false, status: 400, reason: 'operacao deve ser provisionar|validar' };
  if (typeof b.vps_ip !== 'string' || !RE_IPV4.test(b.vps_ip)) return { ok: false, status: 400, reason: 'vps_ip invalido (IPv4)' };
  const dominio = typeof b.dominio === 'string' ? b.dominio.trim().toLowerCase() : '';
  if (!RE_FQDN.test(dominio)) return { ok: false, status: 400, reason: 'dominio invalido (FQDN)' };
  let nome = typeof b.nome_loja === 'string' ? b.nome_loja.trim() : '';
  if (!nome) nome = dominio;
  if (nome.length > 120 || /[\u0000-\u001f"\\$`]/.test(nome)) return { ok: false, status: 400, reason: 'nome_loja invalido' };
  return { ok: true, operacao: b.operacao, payload: { vps_ip: b.vps_ip, dominio, nome_loja: nome } };
}

module.exports = { autenticar, validarContrato, OPERACOES };
