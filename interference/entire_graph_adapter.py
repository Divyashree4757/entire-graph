"""Real Entire Graph integration (plugin v0.4.0)."""
import json
import subprocess
from collections import defaultdict

# Relations representing genuine dependency coupling.
# DEFINES and CONTAINS are deliberately EXCLUDED: they link a file node to
# every symbol inside it, so traversing them makes any two symbols in the
# same file look 2 hops apart and inflates every impact radius to file scope.
DEPENDENCY_RELATIONS = {
    "CALLS", "ASYNC_CALLS", "CONSTRUCTS", "USES_TYPE", "EXTENDS",
    "IMPLEMENTS", "IMPORTS", "HANDLES_ROUTE", "EMITS", "LISTENS_ON",
    "CONFIGURES", "TESTS", "RESOURCE_DEPENDS_ON", "DATA_FLOWS",
}

# An added entity cannot break an existing caller. These can.
CONTRACT_CHANGE_TYPES = {"signature_changed", "removed", "renamed"}


def _stream(cmd, cwd):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{out.stderr}")
    for line in out.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _empty_completeness():
    return {"completeness_level": "unknown", "partial_failures": 0, "warnings": []}


def _extract_completeness(summary_rec):
    """Both `symbols` and `edges` end their stream with a trailing summary
    record carrying aggregate stats and completeness. Callers used to skip
    it entirely (record_type didn't match symbol/relation); surface it
    instead so a partial run can be reported rather than silently trusted."""
    stats = summary_rec.get("stats") or {}
    return {
        "completeness_level": stats.get("completeness_level", "unknown"),
        "partial_failures": stats.get("partial_failures", 0),
        "warnings": summary_rec.get("warnings") or [],
    }


def load_symbols(repo):
    """Returns (symbols, completeness).

    symbol_id -> {id, path, name, kind, start_line}
    """
    symbols = {}
    completeness = _empty_completeness()
    for rec in _stream(["entire", "graph", "symbols", "--repo", ".",
                        "--format", "ndjson"], repo):
        rt = rec.get("record_type")
        if rt == "summary":
            completeness = _extract_completeness(rec)
            continue
        if rt not in (None, "symbol", "entity", "node"):
            continue
        sid = rec.get("id") or rec.get("symbol_id")
        if not sid:
            continue
        symbols[sid] = {
            "id": sid,
            "path": rec.get("path") or rec.get("file") or rec.get("file_path"),
            "name": rec.get("name") or rec.get("symbol") or "",
            "kind": rec.get("kind") or rec.get("symbol_kind") or "",
            "start_line": rec.get("start_line"),
        }
    return symbols, completeness


def load_capabilities(repo):
    out = subprocess.run(["entire", "graph", "capabilities", "--json"],
                         cwd=repo, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"graph capabilities failed:\n{out.stderr}")
    return json.loads(out.stdout)


def load_heuristic_relation_types(repo) -> set:
    """The graph is the authority on which relation types are heuristic —
    read it at runtime instead of hardcoding a list that can drift out of
    sync with what `capabilities` actually reports."""
    return set(load_capabilities(repo).get("heuristic_relation_types") or [])


# CALLS resolutions whose own `reason` text starts "direct call expression
# resolved to..." — a literal, unambiguous AST call site matched to its
# target. "exact" (same-file) and "package" (same-package, different file)
# differ only in scope, not in resolution method: both are as parser-certain
# as this graph gets. Every other resolution — name_only ("top-level call
# expression resolved to symbol", no confirmed call site), import_external,
# type_inferred, import_resolved, pattern — involves name-guessing, type
# inference, or crossing an external boundary, and is genuinely weaker
# evidence. Verified against this repo's live edge stream (see the CALLS
# reason-text survey run alongside the demo-live regression check).
CONFIRMED_RESOLUTIONS = {"exact", "package"}


def classify_edge_tier(edge) -> str:
    """CONFIRMED: a direct, non-heuristic resolution (see
    CONFIRMED_RESOLUTIONS) with no warning_codes. Anything weaker is
    HEURISTIC. This is keyed on `resolution`, not a numeric confidence
    floor: on live data, `confidence` never reaches a fixed 1.0 ceiling
    even at "exact" resolution (observed max ~0.92 on this repo's CALLS
    edges) — `resolution` is the graph's own qualitative certainty label
    and is what "parser-resolved" actually maps to, while `confidence`
    reflects something else (evidence breadth). A fixed >=1.0 floor would
    make CONFIRMED unreachable on real output, silently defeating this
    tier for every live run. This is the single source of truth for edge
    quality — both load_edges' edge-count tally and pr_interference's
    weakest-link classification call this instead of re-deriving it."""
    if edge.get("heuristic"):
        return "HEURISTIC"
    if edge.get("resolution") in CONFIRMED_RESOLUTIONS and not edge.get("warning_codes"):
        return "CONFIRMED"
    return "HEURISTIC"


def _edge_quality(edge):
    tier = classify_edge_tier(edge)
    confidence = edge.get("confidence") if edge.get("confidence") is not None else 0.0
    return (1 if tier == "CONFIRMED" else 0, confidence)


def _add_best_edge(adj, a, b, edge):
    """Multiple relation types can connect the same pair (e.g. both CALLS
    and USES_TYPE). Previously the last one streamed silently won; now the
    higher-quality edge wins deliberately, so a strong edge between two
    symbols is never masked by a weaker one seen later in the stream."""
    existing = adj[a].get(b)
    if existing is None or _edge_quality(edge) > _edge_quality(existing):
        adj[a][b] = edge


def load_edges(repo, relations=DEPENDENCY_RELATIONS, heuristic_types=None):
    """Returns (adj, completeness).

    adj: node_id -> {neighbor_id: edge_record}. Undirected: interference
    propagates both ways — a caller is affected by its callee changing, and
    a callee is affected by gaining a new caller with different
    expectations. Each edge_record keeps confidence/resolution/
    warning_codes/heuristic so downstream code can classify evidence
    quality instead of treating every edge as equally authoritative.
    """
    if heuristic_types is None:
        heuristic_types = load_heuristic_relation_types(repo)

    adj = defaultdict(dict)
    completeness = _empty_completeness()
    confirmed_edges = 0
    heuristic_edges = 0
    for rec in _stream(["entire", "graph", "edges", "--repo", ".",
                        "--format", "ndjson"], repo):
        rt = rec.get("record_type")
        if rt == "summary":
            completeness = _extract_completeness(rec)
            continue
        if rt not in (None, "relation", "edge"):
            continue
        t = rec.get("type") or rec.get("relation")
        if relations and t not in relations:
            continue
        a, b = rec.get("from_id"), rec.get("to_id")
        if not a or not b:
            continue
        edge = {
            "type": t,
            "confidence": rec.get("confidence", 1.0),
            "resolution": rec.get("resolution"),
            "warning_codes": rec.get("warning_codes") or [],
            "heuristic": t in heuristic_types,
        }
        if classify_edge_tier(edge) == "CONFIRMED":
            confirmed_edges += 1
        else:
            heuristic_edges += 1
        _add_best_edge(adj, a, b, edge)
        _add_best_edge(adj, b, a, edge)

    completeness["confirmed_edges"] = confirmed_edges
    completeness["heuristic_edges"] = heuristic_edges
    return adj, completeness


def semantic_diff(repo, base, head):
    """Returns {"changes": [...], "partial": bool, "warnings": [...]}.

    A diff that hit its analysis budget stops early and reports
    W_ANALYSIS_BUDGET_EXCEEDED with a partial result — that must be
    surfaced, not treated as a complete answer.
    """
    out = subprocess.run(["entire", "graph", "diff", "--base", base,
                          "--head", head, "--json"],
                         cwd=repo, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"graph diff failed:\n{out.stderr}")
    data = json.loads(out.stdout)

    warnings = data.get("warnings") or []
    budget_exceeded = any(
        (w.get("code") if isinstance(w, dict) else w) == "W_ANALYSIS_BUDGET_EXCEEDED"
        for w in warnings
    )
    partial = bool(budget_exceeded or data.get("partial"))

    changes = []
    # "files" is explicitly null (not omitted) when base and head are
    # identical — `or []` handles that; a bare `.get("files", [])` would
    # crash on the null value instead of falling back.
    for f in data.get("files") or []:
        for c in f.get("changes", []):
            changes.append({
                "path": f.get("path"),
                "language": f.get("language"),
                "status": f.get("status"),
                "type": c.get("type"),
                "kind": c.get("kind"),
                "name": c.get("name"),
                "dependents_count": c.get("dependents_count") or 0,
                "contract_change": c.get("type") in CONTRACT_CHANGE_TYPES,
            })
    return {"changes": changes, "partial": partial, "warnings": warnings}


def build_name_index(symbols):
    idx = defaultdict(list)
    for s in symbols.values():
        if s["path"]:
            idx[(s["path"], s["name"])].append(s["id"])
    return idx


def resolve_change(change, symbols, name_index, allow_fallback=True) -> list:
    """Map a semantic-diff entity onto graph symbol ids.

    allow_fallback controls whether an unresolved change may seed every
    symbol in its file. Callers pass False once the file already has
    symbol-level changes, so a name-index miss just resolves to nothing
    instead of silently exploding into a whole-file seed."""
    hit = name_index.get((change["path"], change["name"]))
    if hit:
        return hit
    if not allow_fallback:
        return []
    # module-level change (whole file): seed every symbol in that file
    return [s["id"] for s in symbols.values() if s["path"] == change["path"]]


def debug_shapes(repo):
    """Print the first records of each stream — run if field names differ."""
    for cmd in (["entire", "graph", "symbols", "--repo", ".", "--format", "ndjson"],
                ["entire", "graph", "edges", "--repo", ".", "--format", "ndjson"]):
        print(f"\n$ {' '.join(cmd)}")
        for i, rec in enumerate(_stream(cmd, repo)):
            print(json.dumps(rec, indent=2))
            if i >= 1:
                break
