from aml import delivery
from tests_aml.test_gap_search import gap_memory
from tests_aml.test_arc_planning import planned
from tests_aml.test_gap_search import test_no_menu_gap_search_keeps_joint_evidence_and_stops_after_one_round as run_case


def test_gap_flow_reports_counts_without_content(gap_memory, monkeypatch):
    events = []
    monkeypatch.setattr(delivery, 'observe', lambda stage, *args, **kw: events.append((stage, args, kw)))
    run_case(gap_memory, monkeypatch)
    gaps = [(stage, args, kw) for stage, args, kw in events if stage.startswith('gap_')]
    assert {'gap_retrieved', 'gap_existing', 'gap_read_guard_rejected', 'gap_rank_input', 'gap_eligible'} <= {s for s, _, _ in gaps}
    assert all(not args and set(kw) <= {'count'} for _, args, kw in gaps)
    assert any(s == 'gap_entity_absent' and kw['count'] > 0 for s, _, kw in gaps)
