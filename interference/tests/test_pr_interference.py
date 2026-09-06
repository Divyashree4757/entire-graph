"""Tests for the "graph is evidence, not an oracle" curveball response.

These mock entire_graph_adapter's I/O-boundary functions (load_symbols,
load_edges, semantic_diff, load_heuristic_relation_types) with data shaped
exactly like real `entire graph` NDJSON/JSON records, so the tests are
hermetic and fast while still exercising the real BFS/risk/tier logic in
pr_interference.py unmodified.
"""
import pr_interference as PI


def _symbol(sid, path, name, kind="function", start_line=1):
    return {"id": sid, "path": path, "name": name, "kind": kind, "start_line": start_line}


def _edge(edge_type, confidence=0.92, resolution="exact", warning_codes=None, heuristic=False):
    return {"type": edge_type, "confidence": confidence, "resolution": resolution,
            "warning_codes": warning_codes or [], "heuristic": heuristic}


def _ok_completeness(**extra):
    c = {"completeness_level": "ok", "partial_failures": 0, "warnings": []}
    c.update(extra)
    return c


def _patch(monkeypatch, symbols, symbols_completeness, adj, edges_completeness,
           diffs, heuristic_types=frozenset()):
    """diffs: {head_ref: {"changes": [...], "partial": bool, "warnings": [...]}}"""
    monkeypatch.setattr(PI.G, "load_heuristic_relation_types", lambda repo: set(heuristic_types))
    monkeypatch.setattr(PI.G, "load_symbols", lambda repo: (symbols, symbols_completeness))
    monkeypatch.setattr(PI.G, "load_edges", lambda repo, heuristic_types=None: (adj, edges_completeness))
    monkeypatch.setattr(PI.G, "semantic_diff", lambda repo, base, head: diffs[head])


# ---------------------------------------------------------------------------
# Regression guard: fully-resolved evidence behaves exactly as before.
# ---------------------------------------------------------------------------

def test_fully_resolved_case_still_yields_high_and_serialize_merge_order(monkeypatch):
    symbols = {
        "x1": _symbol("x1", "pkg/checksum.go", "computeChecksum", start_line=10),
        "y1": _symbol("y1", "pkg/other.go", "otherFeature", start_line=20),
        "z1": _symbol("z1", "pkg/shared.go", "sharedHelper", start_line=5),
    }
    edge_x_z = _edge("CALLS")
    edge_y_z = _edge("CALLS")
    adj = {
        "x1": {"z1": edge_x_z},
        "z1": {"x1": edge_x_z, "y1": edge_y_z},
        "y1": {"z1": edge_y_z},
    }
    diffs = {
        "pr-a": {"changes": [{"path": "pkg/checksum.go", "language": "Go", "status": "M",
                              "type": "signature_changed", "kind": "function",
                              "name": "computeChecksum", "dependents_count": 5,
                              "contract_change": True}],
                "partial": False, "warnings": []},
        "pr-b": {"changes": [{"path": "pkg/other.go", "language": "Go", "status": "M",
                              "type": "body_changed", "kind": "function",
                              "name": "otherFeature", "dependents_count": 1,
                              "contract_change": False}],
                "partial": False, "warnings": []},
    }
    _patch(monkeypatch, symbols, _ok_completeness(), adj,
           _ok_completeness(confirmed_edges=2, heuristic_edges=0), diffs)

    r = PI.analyze("/repo", "main", "pr-a", "pr-b", depth=1)

    assert r["recommendation"] == "SERIALIZE_MERGE_ORDER"
    assert r["suggested_merge_order"] == ["pr-a", "pr-b"]
    assert len(r["overlaps"]) == 1
    overlap = r["overlaps"][0]

    # Pre-existing fields: unchanged values from before the tier system existed.
    assert overlap["node"] == "z1"
    assert overlap["name"] == "sharedHelper"
    assert overlap["risk"] == "HIGH"
    assert overlap["hops_from_pr_a"] == 1
    assert overlap["hops_from_pr_b"] == 1
    assert overlap["contract_changed"] is True
    assert overlap["dependents_count"] == 5
    assert overlap["low_precision"] is False

    # New fields: fully-resolved evidence must not be capped or flagged.
    assert overlap["evidence_tier"] == "CONFIRMED"
    assert overlap["weakest_edge"] == {"type": "CALLS", "resolution": "exact", "confidence": 0.92}
    assert overlap["verification_hint"] == "run tests covering sharedHelper"
    assert r["coverage"]["completeness_level"] == "ok"
    assert r["coverage"]["diff_partial"] is False


# ---------------------------------------------------------------------------
# Heuristic-edge case: capped at MEDIUM, tagged, with a verification hint.
# ---------------------------------------------------------------------------

def test_heuristic_edge_caps_at_medium_and_carries_hint(monkeypatch):
    symbols = {
        "x1": _symbol("x1", "pkg/a.go", "Foo", start_line=7),
        "w1": _symbol("w1", "pkg/w.go", "Bar", start_line=30),
        "z1": _symbol("z1", "pkg/shared.go", "SharedThing", start_line=42),
    }
    edge_x_z = _edge("TESTS", confidence=0.6, resolution="heuristic_match", heuristic=True)
    edge_w_z = _edge("CALLS")
    adj = {
        "x1": {"z1": edge_x_z},
        "z1": {"x1": edge_x_z, "w1": edge_w_z},
        "w1": {"z1": edge_w_z},
    }
    diffs = {
        "pr-a": {"changes": [{"path": "pkg/a.go", "language": "Go", "status": "M",
                              "type": "signature_changed", "kind": "function",
                              "name": "Foo", "dependents_count": 5, "contract_change": True}],
                "partial": False, "warnings": []},
        "pr-b": {"changes": [{"path": "pkg/w.go", "language": "Go", "status": "M",
                              "type": "body_changed", "kind": "function",
                              "name": "Bar", "dependents_count": 1, "contract_change": False}],
                "partial": False, "warnings": []},
    }
    _patch(monkeypatch, symbols, _ok_completeness(), adj,
           _ok_completeness(confirmed_edges=1, heuristic_edges=1), diffs,
           heuristic_types={"TESTS"})

    r = PI.analyze("/repo", "main", "pr-a", "pr-b", depth=1)

    assert len(r["overlaps"]) == 1
    overlap = r["overlaps"][0]
    # Without the cap this would be HIGH: contract_changed=True and hops<=2.
    assert overlap["risk"] == "MEDIUM"
    assert overlap["evidence_tier"] == "HEURISTIC"
    assert overlap["weakest_edge"] == {"type": "TESTS", "resolution": "heuristic_match", "confidence": 0.6}
    assert overlap["verification_hint"] == "verify by reading pkg/shared.go:42"


# ---------------------------------------------------------------------------
# Incomplete-analysis case: partial graph data or a truncated diff.
# ---------------------------------------------------------------------------

def _single_overlap_fixture():
    symbols = {
        "x1": _symbol("x1", "pkg/a.go", "Foo", start_line=7),
        "w1": _symbol("w1", "pkg/w.go", "Bar", start_line=30),
        "z1": _symbol("z1", "pkg/shared.go", "SharedThing", start_line=42),
    }
    edge_x_z = _edge("CALLS")
    edge_w_z = _edge("CALLS")
    adj = {
        "x1": {"z1": edge_x_z},
        "z1": {"x1": edge_x_z, "w1": edge_w_z},
        "w1": {"z1": edge_w_z},
    }
    base_diffs = {
        "pr-a": {"changes": [{"path": "pkg/a.go", "language": "Go", "status": "M",
                              "type": "signature_changed", "kind": "function",
                              "name": "Foo", "dependents_count": 5, "contract_change": True}],
                "partial": False, "warnings": []},
        "pr-b": {"changes": [{"path": "pkg/w.go", "language": "Go", "status": "M",
                              "type": "body_changed", "kind": "function",
                              "name": "Bar", "dependents_count": 1, "contract_change": False}],
                "partial": False, "warnings": []},
    }
    return symbols, adj, base_diffs


def test_partial_failures_marks_findings_unverified(monkeypatch):
    symbols, adj, diffs = _single_overlap_fixture()
    symbols_completeness = _ok_completeness(completeness_level="degraded", partial_failures=3)
    edges_completeness = _ok_completeness(confirmed_edges=2, heuristic_edges=0)
    _patch(monkeypatch, symbols, symbols_completeness, adj, edges_completeness, diffs)

    r = PI.analyze("/repo", "main", "pr-a", "pr-b", depth=1)

    overlap = r["overlaps"][0]
    # Would be HIGH on edge evidence alone (all-CALLS, contract change, hops<=2)
    # but the graph admitted partial_failures, so it can't be CONFIRMED.
    assert overlap["evidence_tier"] == "UNVERIFIED"
    assert overlap["risk"] == "MEDIUM"
    assert r["coverage"]["completeness_level"] == "degraded"
    assert r["coverage"]["graph_partial_failures"] == 3


def test_budget_exceeded_diff_marks_findings_unverified_and_reports_partial(monkeypatch):
    symbols, adj, diffs = _single_overlap_fixture()
    diffs = dict(diffs)
    diffs["pr-a"] = dict(diffs["pr-a"], partial=True,
                         warnings=[{"code": "W_ANALYSIS_BUDGET_EXCEEDED"}])
    _patch(monkeypatch, symbols, _ok_completeness(), adj,
           _ok_completeness(confirmed_edges=2, heuristic_edges=0), diffs)

    r = PI.analyze("/repo", "main", "pr-a", "pr-b", depth=1)

    overlap = r["overlaps"][0]
    assert overlap["evidence_tier"] == "UNVERIFIED"
    assert overlap["risk"] == "MEDIUM"
    assert r["coverage"]["diff_partial"] is True

    text = PI.report_text(r)
    assert "diff=PARTIAL" in text
    assert "UNVERIFIED" in text
    assert "blind spot" in text
