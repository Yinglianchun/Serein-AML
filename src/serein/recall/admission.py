"""Candidate discovery is distinct from evidence admission and delivery."""

import math

from .scene import evidence_text


def decide(hit, query, policy, rerank_score=None):
    document = hit["object"]["document"]
    ref = f"{hit['kind']}:{hit['id']}"
    if ref in query.exclude_ids or hit["id"] in query.exclude_ids:
        return "reject", "explicitly_excluded"
    if query.mode == "lookup":
        return "lookup", "explicit_lookup_candidate"
    if query.names_title(document["title"]):
        return "direct", "explicit_full_title"
    # Cue/BM25 scores are not semantic evidence scores. They stay candidates.
    if hit.get('method') == 'entity':
        return 'candidate', 'entity_handle_not_body_evidence'
    if hit.get("method") != "cosine" and rerank_score is None:
        return "candidate", "lexical_or_cue_candidate_only"
    body = evidence_text(document) if hit["kind"] == "scene" else document["body_md"]
    if not body.strip():
        return "reject", "no_body_evidence"
    if rerank_score is None:
        return "candidate", "reranker_unavailable"
    if not math.isfinite(rerank_score) or rerank_score < policy.direct_threshold:
        return "reject", "reranker_below_threshold"
    return "direct", "reranked_body_evidence"
