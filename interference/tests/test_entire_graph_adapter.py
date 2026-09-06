import json
import subprocess

import entire_graph_adapter as G


class FakeCompletedProcess:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _ndjson(records):
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _patch_run(monkeypatch, records):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return FakeCompletedProcess(_ndjson(records))
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_classify_edge_tier_confirmed_on_exact_resolution():
    # Real "exact" CALLS edges on this repo top out around confidence 0.92 —
    # tier must key on resolution, not a numeric confidence floor.
    edge = {"heuristic": False, "confidence": 0.85, "resolution": "exact", "warning_codes": []}
    assert G.classify_edge_tier(edge) == "CONFIRMED"


def test_classify_edge_tier_confirmed_on_package_resolution():
    # "package" (direct call, same package, different file) is just as
    # parser-certain as "exact" (direct call, same file) -- confirmed via the
    # live demo-live regression run: the pr-b caller of the pr-a
    # contract-changed symbol resolves as "package", and treating it as
    # anything less than CONFIRMED wrongly capped a real, verified HIGH
    # finding to MEDIUM.
    edge = {"heuristic": False, "confidence": 0.8, "resolution": "package", "warning_codes": []}
    assert G.classify_edge_tier(edge) == "CONFIRMED"


def test_classify_edge_tier_heuristic_type_outranks_exact_resolution():
    edge = {"heuristic": True, "confidence": 0.9, "resolution": "exact", "warning_codes": []}
    assert G.classify_edge_tier(edge) == "HEURISTIC"


def test_classify_edge_tier_non_exact_resolution_is_heuristic_even_at_high_confidence():
    edge = {"heuristic": False, "confidence": 0.99, "resolution": "name_only", "warning_codes": []}
    assert G.classify_edge_tier(edge) == "HEURISTIC"


def test_classify_edge_tier_warning_codes_demote_exact_resolution():
    edge = {"heuristic": False, "confidence": 0.9, "resolution": "exact",
            "warning_codes": ["W_AMBIGUOUS"]}
    assert G.classify_edge_tier(edge) == "HEURISTIC"


def test_load_edges_keeps_confidence_resolution_and_warnings(monkeypatch):
    records = [
        {"record_type": "relation", "type": "CALLS", "from_id": "a", "to_id": "b",
         "confidence": 0.92, "resolution": "exact", "warning_codes": []},
        {"record_type": "summary", "stats": {"completeness_level": "ok", "partial_failures": 0}},
    ]
    _patch_run(monkeypatch, records)
    adj, completeness = G.load_edges("/repo", relations={"CALLS"}, heuristic_types=set())
    edge = adj["a"]["b"]
    assert edge["type"] == "CALLS"
    assert edge["confidence"] == 0.92
    assert edge["resolution"] == "exact"
    assert edge["warning_codes"] == []
    assert completeness["completeness_level"] == "ok"
    assert completeness["confirmed_edges"] == 1
    assert completeness["heuristic_edges"] == 0


def test_load_edges_fixes_collision_overwrite_bug(monkeypatch):
    """Two relation types connect the same pair: a weak heuristic one
    streamed first, a strong parser-resolved one streamed second. The
    stronger edge must win regardless of stream order (previously the last
    edge seen silently won no matter its quality)."""
    records = [
        {"record_type": "relation", "type": "TESTS", "from_id": "a", "to_id": "b",
         "confidence": 0.4, "resolution": "heuristic_match", "warning_codes": []},
        {"record_type": "relation", "type": "CALLS", "from_id": "a", "to_id": "b",
         "confidence": 0.92, "resolution": "exact", "warning_codes": []},
    ]
    _patch_run(monkeypatch, records)
    adj, _ = G.load_edges("/repo", relations={"TESTS", "CALLS"}, heuristic_types={"TESTS"})
    assert adj["a"]["b"]["type"] == "CALLS"
    assert adj["b"]["a"]["type"] == "CALLS"


def test_load_edges_collision_overwrite_reverse_order(monkeypatch):
    """Same as above but the strong edge streams first — it must not be
    displaced by the weaker one seen later."""
    records = [
        {"record_type": "relation", "type": "CALLS", "from_id": "a", "to_id": "b",
         "confidence": 0.92, "resolution": "exact", "warning_codes": []},
        {"record_type": "relation", "type": "TESTS", "from_id": "a", "to_id": "b",
         "confidence": 0.4, "resolution": "heuristic_match", "warning_codes": []},
    ]
    _patch_run(monkeypatch, records)
    adj, _ = G.load_edges("/repo", relations={"TESTS", "CALLS"}, heuristic_types={"TESTS"})
    assert adj["a"]["b"]["type"] == "CALLS"


def test_load_edges_tallies_confirmed_and_heuristic(monkeypatch):
    records = [
        {"record_type": "relation", "type": "CALLS", "from_id": "a", "to_id": "b",
         "confidence": 0.92, "resolution": "exact", "warning_codes": []},
        {"record_type": "relation", "type": "TESTS", "from_id": "c", "to_id": "d",
         "confidence": 0.4, "resolution": "heuristic_match", "warning_codes": []},
    ]
    _patch_run(monkeypatch, records)
    _, completeness = G.load_edges("/repo", relations={"CALLS", "TESTS"}, heuristic_types={"TESTS"})
    assert completeness["confirmed_edges"] == 1
    assert completeness["heuristic_edges"] == 1


def test_load_symbols_captures_completeness_and_start_line(monkeypatch):
    records = [
        {"record_type": "symbol", "id": "s1", "file_path": "f.py", "name": "foo",
         "kind": "function", "start_line": 12},
        {"record_type": "summary", "stats": {"completeness_level": "partial", "partial_failures": 2}},
    ]
    _patch_run(monkeypatch, records)
    symbols, completeness = G.load_symbols("/repo")
    assert symbols["s1"]["start_line"] == 12
    assert completeness["completeness_level"] == "partial"
    assert completeness["partial_failures"] == 2


def test_semantic_diff_handles_null_files(monkeypatch):
    """base == head returns {"files": null} — must not crash."""
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return FakeCompletedProcess(json.dumps({"base": "x", "head": "x", "files": None}))
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = G.semantic_diff("/repo", "x", "x")
    assert result["changes"] == []
    assert result["partial"] is False


def test_semantic_diff_flags_budget_exceeded(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return FakeCompletedProcess(json.dumps({
            "files": [],
            "warnings": [{"code": "W_ANALYSIS_BUDGET_EXCEEDED", "detail": "stopped early"}],
        }))
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = G.semantic_diff("/repo", "x", "y")
    assert result["partial"] is True
    assert result["warnings"][0]["code"] == "W_ANALYSIS_BUDGET_EXCEEDED"


def test_semantic_diff_complete_run_not_flagged_partial(monkeypatch):
    def fake_run(cmd, cwd, capture_output, text, timeout):
        return FakeCompletedProcess(json.dumps({"files": [], "warnings": []}))
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = G.semantic_diff("/repo", "x", "y")
    assert result["partial"] is False
