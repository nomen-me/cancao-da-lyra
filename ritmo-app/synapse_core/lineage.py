"""Linhagem causal (Ordem Final secoes 2 e 3). Funcoes puras, sem I/O."""
import uuid

ORIGIN_ACTORS = ("HUMAN", "LYRA", "SYSTEM")


def new_id():
    return str(uuid.uuid4())


def next_lineage(parent, event_id, limit, chain_stats=None, new_chain_id=None):
    """Calcula root/parent/chain/depth do proximo evento.

    parent: None (evento sem causa anterior conhecida) ou dict com
            event_id, root_event_id, chain_id, causal_depth.
    limit:  causal_depth_limit. Uma cadeia admite profundidades 0..limit.
            O evento que seria limit+1 encerra a cadeia e abre outra.
    chain_stats: agregados da cadeia que esta sendo encerrada (usado no resumo).

    Regra: nao carrega payload historico para a nova cadeia; apenas
    previous_chain_id + causal_summary compacto.
    """
    new_chain_id = new_chain_id or new_id()
    if not parent:
        return {
            "root_event_id": event_id, "parent_event_id": None, "chain_id": new_chain_id,
            "previous_chain_id": None, "causal_depth": 0, "causal_summary": None, "chain_closed": False,
        }
    depth = int(parent.get("causal_depth") or 0) + 1
    if depth <= limit:
        return {
            "root_event_id": parent["root_event_id"], "parent_event_id": parent["event_id"],
            "chain_id": parent["chain_id"], "previous_chain_id": None, "causal_depth": depth,
            "causal_summary": None, "chain_closed": False,
        }
    summary = summarize(parent, chain_stats or {})
    return {
        "root_event_id": event_id, "parent_event_id": parent["event_id"], "chain_id": new_chain_id,
        "previous_chain_id": parent["chain_id"], "causal_depth": 0, "causal_summary": summary, "chain_closed": True,
    }


def summarize(parent, stats):
    """Resumo deterministico e limitado (tamanho independe do tamanho da cadeia)."""
    return {
        "closed_chain_id": parent["chain_id"],
        "root_event_id": parent["root_event_id"],
        "last_event_id": parent["event_id"],
        "depth_reached": int(parent.get("causal_depth") or 0),
        "event_count": int(stats.get("event_count", 0)),
        "event_types": dict(stats.get("event_types", {})),
        "origin_actors": dict(stats.get("origin_actors", {})),
        "entity_type": parent.get("entity_type"),
        "entity_id": parent.get("entity_id"),
    }
