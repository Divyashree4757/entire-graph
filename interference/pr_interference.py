#!/usr/bin/env python3
"""Entire Interference — pre-merge structural conflict detection.

    python pr_interference.py --repo . --base main --pr-a feat/a --pr-b feat/b

Graph is evidence, not an oracle: every finding is tagged with the
weakest-link quality of the evidence path that produced it (see
classify_evidence), and a finding resting on anything less than fully
parser-resolved evidence is capped below HIGH.
"""
import argparse
import json
import sys
from collections import deque, defaultdict
from pathlib import Path

import entire_graph_adapter as G

# A change that resolves to more than this many symbols carries about as
# little information as a whole-file body_changed entry (see pr_radius) even
# if it technically matched a name in the index — treat it the same way.
LOW_PRECISION_FANOUT_LIMIT = 50

EVIDENCE_TIER_RANK = {"CONFIRMED": 0, "HEURISTIC": 1, "UNVERIFIED": 2}

BLIND_SPOTS = [
    "JSON, Markdown, and YAML are inventory-only in this graph (CONTAINS/"
    "DEFINES only, no dependency relations) — coupling introduced through "
    "shared config or docs is structurally invisible to this analysis.",
]


def bfs(seed_budgets, adj):
    """Multi-source BFS where each seed carries its own hop budget.

    A low-precision seed (whole-file fallback, or a change with an
    implausibly large fan-out) gets a budget of 1, so its influence dies out
    immediately; a precise symbol-level seed gets the full requested depth.
    Budgets are carried along each path and decremented per hop. When every
    seed shares the same budget this reduces to plain depth-capped BFS."""
    dist = {s: 0 for s in seed_budgets}
    parent = {s: None for s in seed_budgets}
    budget = dict(seed_budgets)
    q = deque(seed_budgets)
    while q:
        n = q.popleft()
        if budget[n] <= 0:
            continue
        for nxt in adj.get(n, ()):
            if nxt not in dist:
                dist[nxt] = dist[n] + 1
                parent[nxt] = n
                budget[nxt] = budget[n] - 1
                q.append(nxt)
    return dist, parent


def path_to(node, parent):
    p = [node]
    while parent.get(p[-1]) is not None:
        p.append(parent[p[-1]])
    return list(reversed(p))


def edges_on_path(path, adj):
    """Edge records for each hop of a node-id path (empty if path is a
    single node, i.e. the node is itself a seed on that side)."""
    return [adj[path[i]][path[i + 1]] for i in range(len(path) - 1)]


def classify_evidence(path_a, path_b, adj, incomplete):
    """Weakest-link classification across both PRs' evidence paths to a
    shared node. `incomplete` outranks any edge-level evidence: if the
    graph or the diff admitted to missing data for this run, nothing
    derived from it can be called CONFIRMED no matter how clean the edges
    on the path look."""
    edges = edges_on_path(path_a, adj) + edges_on_path(path_b, adj)
    tier = "CONFIRMED"
    weakest = None
    for edge in edges:
        edge_tier = G.classify_edge_tier(edge)
        edge_confidence = edge.get("confidence") if edge.get("confidence") is not None else 1.0
        weakest_confidence = (weakest.get("confidence") if weakest and weakest.get("confidence") is not None
                              else 1.0)
        worse = (
            weakest is None
            or EVIDENCE_TIER_RANK[edge_tier] > EVIDENCE_TIER_RANK[tier]
            or (edge_tier == tier and edge_confidence < weakest_confidence)
        )
        if worse:
            tier, weakest = edge_tier, edge

    if incomplete:
        tier = "UNVERIFIED"
    return tier, weakest


def _weakest_edge_view(edge):
    if edge is None:
        return None
    return {"type": edge.get("type"), "resolution": edge.get("resolution"),
            "confidence": edge.get("confidence")}


def verification_hint(sym, tier):
    """A concrete next step, not just a warning label."""
    name = sym.get("name") or "this symbol"
    path = sym.get("path")
    line = sym.get("start_line")
    if tier == "CONFIRMED":
        return f"run tests covering {name}"
    if path and line:
        return f"verify by reading {path}:{line}"
    if path:
        return f"verify by reading {path}"
    return f"run tests covering {name}"


def _is_incomplete(completeness):
    level = completeness.get("completeness_level")
    return bool(level not in (None, "ok", "unknown") or (completeness.get("partial_failures") or 0) > 0)


def _worse_level(a, b):
    candidates = [lvl for lvl in (a, b) if lvl is not None]
    if not candidates:
        return "unknown"
    bad = [lvl for lvl in candidates if lvl not in ("ok", "unknown")]
    return bad[0] if bad else candidates[0]


def pr_radius(repo, base, head, symbols, name_index, adj, depth):
    diff = G.semantic_diff(repo, base, head)
    changes = diff["changes"]

    # A `kind == "module"` entry (graph-diff's body_changed-on-the-whole-file
    # signal) only tells us "something in this file changed," not what — so
    # seeding every symbol in the file for it destroys the overlap signal on
    # any file with real edits. Decide per file: if any change is
    # symbol-level, trust only those and drop the module entry for that file;
    # fall back to whole-file seeding only when the module entry is the only
    # signal we have (e.g. a language the parser doesn't cover), and mark
    # that fallback low_precision so it can't masquerade as a precise finding.
    by_file = defaultdict(list)
    for ch in changes:
        by_file[ch["path"]].append(ch)

    seeds = {}
    for file_changes in by_file.values():
        symbol_level = [c for c in file_changes if c["kind"] != "module"]
        if symbol_level:
            candidates, allow_fallback, coarse = symbol_level, False, False
        else:
            candidates, allow_fallback, coarse = file_changes, True, True

        for ch in candidates:
            sids = G.resolve_change(ch, symbols, name_index, allow_fallback=allow_fallback)
            low_precision = coarse or len(sids) > LOW_PRECISION_FANOUT_LIMIT
            for sid in sids:
                prev = seeds.get(sid)
                if prev is None or (ch["contract_change"] and not prev.get("contract_change")):
                    seeds[sid] = dict(ch, low_precision=low_precision)

    # Low-precision seeds propagate only 1 hop instead of the full depth, so a
    # vague "this file changed" signal can't radiate across the whole graph.
    seed_budgets = {sid: (1 if s["low_precision"] else depth) for sid, s in seeds.items()}
    dist, parent = bfs(seed_budgets, adj)
    return {"changes": changes, "seeds": seeds, "dist": dist, "parent": parent,
            "diff_partial": diff["partial"], "diff_warnings": diff["warnings"]}


def analyze(repo, base, pr_a, pr_b, depth):
    heuristic_types = G.load_heuristic_relation_types(repo)
    symbols, symbols_completeness = G.load_symbols(repo)
    name_index = G.build_name_index(symbols)
    adj, edges_completeness = G.load_edges(repo, heuristic_types=heuristic_types)

    A = pr_radius(repo, base, pr_a, symbols, name_index, adj, depth)
    B = pr_radius(repo, base, pr_b, symbols, name_index, adj, depth)

    # If either symbols/edges collection or either PR's diff admitted to
    # being partial, no finding derived from this run can be called fully
    # confirmed — apply the same doubt to every finding rather than
    # pretending to attribute it more precisely than the graph itself can.
    incomplete = (_is_incomplete(symbols_completeness) or _is_incomplete(edges_completeness)
                 or A["diff_partial"] or B["diff_partial"])

    # Symbols both PRs directly changed are an ordinary git-visible conflict.
    # The interesting class is everything else they both reach.
    both_seeded = set(A["seeds"]) & set(B["seeds"])
    overlap = (set(A["dist"]) & set(B["dist"])) - both_seeded

    overlaps = []
    for nid in sorted(overlap):
        pa, pb = path_to(nid, A["parent"]), path_to(nid, B["parent"])
        seed_a, seed_b = A["seeds"].get(pa[0], {}), B["seeds"].get(pb[0], {})
        contract = bool(seed_a.get("contract_change") or seed_b.get("contract_change"))
        deps = max(seed_a.get("dependents_count", 0), seed_b.get("dependents_count", 0))
        hops = min(A["dist"][nid], B["dist"][nid])
        low_precision = bool(seed_a.get("low_precision") or seed_b.get("low_precision"))
        evidence_tier, weakest_edge = classify_evidence(pa, pb, adj, incomplete)

        if contract and hops <= 2:
            risk = "HIGH"
        elif contract or hops <= 1 or deps >= 10:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        # A low-precision seed (whole-file fallback, or an oversized fan-out)
        # is evidence that *something* in the file may interact with the
        # other PR, not proof of a specific broken contract. Same logic
        # applies to any evidence path that isn't fully parser-resolved
        # (HEURISTIC/UNVERIFIED tier) — graph is evidence, not an oracle, so
        # neither can justify HIGH on its own.
        if (low_precision or evidence_tier != "CONFIRMED") and risk == "HIGH":
            risk = "MEDIUM"

        sym = symbols.get(nid, {})
        overlaps.append({
            "node": nid,
            "name": sym.get("name"),
            "kind": sym.get("kind"),
            "file": sym.get("path"),
            "risk": risk,
            "hops_from_pr_a": A["dist"][nid],
            "hops_from_pr_b": B["dist"][nid],
            "contract_changed": contract,
            "dependents_count": deps,
            "low_precision": low_precision,
            "why_pr_a": f"{seed_a.get('name')} {seed_a.get('type')}" if seed_a else None,
            "why_pr_b": f"{seed_b.get('name')} {seed_b.get('type')}" if seed_b else None,
            "evidence_path_from_pr_a": pa,
            "evidence_path_from_pr_b": pb,
            "evidence_tier": evidence_tier,
            "weakest_edge": _weakest_edge_view(weakest_edge),
            "verification_hint": verification_hint(sym, evidence_tier),
        })

    overlaps.sort(key=lambda o: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[o["risk"]],
                                 o["hops_from_pr_a"] + o["hops_from_pr_b"]))

    if not overlaps:
        rec, order = "MERGE_INDEPENDENTLY", None
    elif any(o["risk"] == "HIGH" for o in overlaps):
        rec = "SERIALIZE_MERGE_ORDER"
        a_breaks = any(c["contract_change"] for c in A["changes"])
        order = [pr_a, pr_b] if a_breaks else [pr_b, pr_a]
    elif any(o["risk"] == "MEDIUM" for o in overlaps):
        rec, order = "REVIEW_CAREFULLY", None
    else:
        rec, order = "MERGE_INDEPENDENTLY", None

    coverage = {
        "completeness_level": _worse_level(symbols_completeness.get("completeness_level"),
                                           edges_completeness.get("completeness_level")),
        "graph_partial_failures": ((symbols_completeness.get("partial_failures") or 0)
                                   + (edges_completeness.get("partial_failures") or 0)),
        "diff_partial": bool(A["diff_partial"] or B["diff_partial"]),
        "confirmed_edges": edges_completeness.get("confirmed_edges", 0),
        "heuristic_edges": edges_completeness.get("heuristic_edges", 0),
        "blind_spots": list(BLIND_SPOTS),
    }

    return {
        "repo": str(repo), "base": base, "pr_a": pr_a, "pr_b": pr_b, "depth": depth,
        "graph_source": "entire-graph",
        "graph_stats": {"symbols": len(symbols),
                        "edges": sum(len(v) for v in adj.values()) // 2},
        "coverage": coverage,
        "semantic_changes": {"pr_a": A["changes"], "pr_b": B["changes"]},
        "overlaps": overlaps,
        "recommendation": rec,
        "suggested_merge_order": order,
    }


def report_text(r):
    lines = [f"\nEntire Interference: {r['pr_a']} vs {r['pr_b']}",
             f"  graph: {r['graph_stats']['symbols']} symbols, "
             f"{r['graph_stats']['edges']} edges"]

    cov = r["coverage"]
    coverage_line = (f"  coverage: completeness={cov['completeness_level']} "
                     f"confirmed_edges={cov['confirmed_edges']} "
                     f"heuristic_edges={cov['heuristic_edges']}")
    if cov["graph_partial_failures"]:
        coverage_line += f" partial_failures={cov['graph_partial_failures']}"
    if cov["diff_partial"]:
        coverage_line += " diff=PARTIAL"
    lines.append(coverage_line)
    for spot in cov["blind_spots"]:
        lines.append(f"  blind spot: {spot}")

    lines.append(f"  overlapping nodes: {len(r['overlaps'])}")
    for o in r["overlaps"][:15]:
        lines.append(f"    [{o['risk']:6} {o['evidence_tier']:10}] {o['name'] or o['node']}  "
                     f"(A={o['hops_from_pr_a']} B={o['hops_from_pr_b']}, "
                     f"contract={o['contract_changed']}, deps={o['dependents_count']})")
        if o.get("verification_hint"):
            lines.append(f"        -> {o['verification_hint']}")

    lines.append(f"  RECOMMENDATION: {r['recommendation']}"
                 + (f"  order={r['suggested_merge_order']}" if r["suggested_merge_order"] else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", required=True)
    ap.add_argument("--pr-a", required=True)
    ap.add_argument("--pr-b", required=True)
    # A hub type/symbol with a large fan-in (e.g. a struct used package-wide)
    # spills the BFS into hundreds of unrelated nodes past 1 hop — see the
    # 779-edge SymbolRecord case. Until propagation is capped per-node by
    # fan-in, depth 1 is the only value that stays a precise signal instead
    # of an almost-whole-graph one.
    ap.add_argument("--depth", type=int, default=1)
    ap.add_argument("--out", default="interference_report.json")
    ap.add_argument("--debug-shapes", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    if args.debug_shapes:
        G.debug_shapes(repo)
        return

    r = analyze(repo, args.base, args.pr_a, args.pr_b, args.depth)
    Path(args.out).write_text(json.dumps(r, indent=2))

    print(report_text(r))
    print(f"\nReport: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
