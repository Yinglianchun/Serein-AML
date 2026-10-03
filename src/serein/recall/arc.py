"""Narrative/quote handoff and dated material ordering; no implicit Arc body read."""

from .event import memory_date
import re


def handoff(intent):
    if intent == "narrative":
        return {"status": "use_narrative_reader", "tools": ["find_arc", "read_arc_materials", "read_memory"]}
    if intent == "exact":
        return {"status": "use_evidence_reader", "tools": ["read_memory"], "with_evidence": True}
    return None


def order_materials(hits, intent):
    if intent not in {"latest", "progress", "timeline"}:
        return hits
    if intent == "progress":
        hits = [hit for hit in hits if re.search(r"第\s*\d+\s*[章集话]|(?:看到|读到|追到)|(?:episode|mission)\s*\d+",
                                               hit["object"]["document"]["body_md"], re.IGNORECASE)]
    dated = [(memory_date(hit["object"]["document"]), hit) for hit in hits]
    dated = [(day, hit) for day, hit in dated if day is not None]
    dated.sort(key=lambda pair: (pair[0], pair[1]["kind"], pair[1]["id"]))
    if intent in {"latest", "progress"}:
        # A day-only date cannot order two same-day experiences by their IDs.
        return [hit for day, hit in dated if day == dated[-1][0]] if dated else []
    return [hit for _, hit in dated]
