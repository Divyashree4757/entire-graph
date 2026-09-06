#!/usr/bin/env python3
"""Entire Interference — pre-merge structural conflict detection.

    python pr_interference.py --repo . --base main --pr-a feat/a --pr-b feat/b
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


def pr_radius(repo, base, head, symbols, name_index, adj, depth):
    changes = G.semantic_diff(repo, base, head)

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
    return {"changes": changes, "seeds": seeds, "dist": dist, "parent": parent}


def analyze(repo, base, pr_a, pr_b, depth):
    symbols = G.load_symbols(repo)
    name_index = G.build_name_index(symbols)
    adj, etype = G.load_edges(repo)

    A = pr_radius(repo, base, pr_a, symbols, name_index, adj, depth)
    B = pr_radius(repo, base, pr_b, symbols, name_index, adj, depth)

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

        if contract and hops <= 2:
            risk = "HIGH"
        elif contract or hops <= 1 or deps >= 10:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        # A low-precision seed (whole-file fallback, or an oversized fan-out)
        # is evidence that *something* in the file may interact with the
        # other PR, not proof of a specific broken contract — never HIGH.
        if low_precision and risk == "HIGH":
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

    return {
        "repo": str(repo), "base": base, "pr_a": pr_a, "pr_b": pr_b, "depth": depth,
        "graph_source": "entire-graph",
        "graph_stats": {"symbols": len(symbols),
                        "edges": sum(len(v) for v in adj.values()) // 2},
        "semantic_changes": {"pr_a": A["changes"], "pr_b": B["changes"]},
        "overlaps": overlaps,
        "recommendation": rec,
        "suggested_merge_order": order,
    }


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

    print(f"\nEntire Interference: {args.pr_a} vs {args.pr_b}")
    print(f"  graph: {r['graph_stats']['symbols']} symbols, "
          f"{r['graph_stats']['edges']} edges")
    print(f"  overlapping nodes: {len(r['overlaps'])}")
    for o in r["overlaps"][:15]:
        print(f"    [{o['risk']:6}] {o['name'] or o['node']}  "
              f"(A={o['hops_from_pr_a']} B={o['hops_from_pr_b']}, "
              f"contract={o['contract_changed']}, deps={o['dependents_count']})")
    print(f"  RECOMMENDATION: {r['recommendation']}"
          + (f"  order={r['suggested_merge_order']}" if r["suggested_merge_order"] else ""))
    print(f"\nReport: {args.out}")


if __name__ == "__main__":
    sys.exit(main())
