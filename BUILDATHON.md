# Entire Interference

## One-sentence summary

A pre-merge structural conflict detector that uses Entire Graph to find pull requests that are individually correct and Git-mergeable but break when combined — and tags every finding with the confidence tier of the evidence behind it.

## Problem, intended user and why it matters

Git detects textual conflicts when two PRs touch the same lines, and CI verifies each PR independently. Neither catches two PRs that are individually correct, pass CI, and merge cleanly — yet interfere through shared dependencies. PR A changes a function's contract; PR B, in a different file, adds a caller depending on the old contract. Git reports no conflict. Both pipelines stay green. The break appears only after both land.

Intended user: a reviewer or merge-queue operator deciding whether two open PRs can land independently, and coding agents needing to know their change is safe alongside concurrent work. This failure class scales with parallelism.

## Selected Entire track and why Entire is essential

Track 2 — Build with Graph Intelligence. Entire Graph is the structural source of truth:

1. `entire graph symbols` / `edges` expose ~11,400 symbols and ~28,400 typed relations across 30 relation types. The impact radius is a traversal over these. Without them we would do text matching — what Git already does, and what misses this bug class.
2. `entire graph diff --json` reports what KIND of change occurred (`signature_changed`, `removed`, `renamed`) with dependent counts. `signature_changed` is our contract-change signal; a line diff cannot distinguish a contract change from a reformat.
3. Every relation carries `confidence`, `resolution`, `warning_codes`, and `capabilities` declares which relation types are heuristic. Our evidence tiers are built entirely on data the graph already provides.

Checkpoints preserved our reasoning across the noon session boundary: a fresh session reconstructed the whole project from checkpoint context alone, without reading code.

## Architecture and main workflow

graph symbols + edges (each keeping confidence/resolution/warning_codes) -> BFS impact radius seeded from graph diff's semantically-changed entities -> intersect the two PRs' radii -> evidence paths -> evidence tier (weakest link) + risk score + verification hint -> coverage report + JSON.

Key decisions: DEFINES/CONTAINS excluded (they link a file to every symbol in it, inflating radii to file scope). Symbols both PRs directly edited are excluded — that is an ordinary Git conflict. Per-file triage prefers symbol-level changes over module-level `body_changed`. Low-precision seeds are capped at MEDIUM and traversed 1 hop. Non-CONFIRMED evidence can never be HIGH.

## Entire Graph findings and verification

Demo: `pr-a` changes the signature of `symbolFlowParameterNames` (internal/sem/types.go) and updates its two existing callers. `pr-b` adds internal/sem/flow_demo.go with a new caller using the OLD signature.

Verified: pr-a builds and vets clean alone (exit 0). pr-b builds and vets clean alone (exit 0). Git merges both with zero conflicts. The merged tree fails:

    internal/sem/flow_demo.go:7:34: not enough arguments in call to symbolFlowParameterNames
            have (SymbolRecord) want (SymbolRecord, bool)

Our tool named that symbol before anything compiled:

    coverage: completeness=ok confirmed_edges=20308 heuristic_edges=13959
    [HIGH   CONFIRMED ] summarizeFlowParameters   (A=1 B=0, contract=True)
    [HIGH   CONFIRMED ] symbolFlowParameterNames  (A=0 B=1, contract=True)
    [MEDIUM HEURISTIC ] SymbolRecord              (A=1 B=1, contract=False)
        -> verify by reading internal/sem/provider.go:319
    RECOMMENDATION: SERIALIZE_MERGE_ORDER  order=['pr-a', 'pr-b']

An earlier version flagged 6,309 of 11,397 symbols — worse than useless. Per-file triage, low-precision tagging, hop budgets and a fan-out guard cut HIGH false alarms 82% (511 -> 94). Preserved in checkpoint ed9c6964fc35.

## Noon Curveball: what changed and how we adapted

ASSUMPTION INVALIDATED: we treated every graph edge as equally authoritative. Our pre-edit graph impact analysis proved we were already violating this: `load_edges` read only type/from_id/to_id, discarding the `confidence`, `resolution` and `warning_codes` every record carries; and DEPENDENCY_RELATIONS included HANDLES_ROUTE, EMITS, LISTENS_ON and TESTS — all four listed by `capabilities` as heuristic — mixed unweighted with parser-resolved relations. The graph had been reporting its own certainty all along and we were discarding it.

DESIGN CHANGE: per-edge evidence records (also fixing a latent bug where the last-streamed relation type silently won a collision). Heuristic types read from `capabilities` at runtime, not hardcoded. Three tiers computed weakest-link across both evidence paths: CONFIRMED (direct parser-resolved, no warnings), HEURISTIC, UNVERIFIED (run reported incomplete data). Non-CONFIRMED can never be HIGH, reusing the existing low_precision cap. Completeness surfaced from the trailing summary record we previously skipped; `semantic_diff` detects W_ANALYSIS_BUDGET_EXCEEDED. A verification hint per finding. Declared blind spots. A coverage header on every run.

TWO CALIBRATIONS FOUND BY RUNNING AGAINST REAL DATA, NOT BY REASONING:
1. `confidence >= 1.0` was empirically unreachable — real CALLS edges top out at 0.92. Taking it literally produced confirmed_edges=0 of 34,263. Our mocked tests passed anyway, because they used hand-written 1.0 fixtures. We re-keyed CONFIRMED on the graph's own `resolution` label instead of a number we invented.
2. `resolution == "exact"` alone was too narrow: the live demo regressed to MEDIUM/REVIEW_CAREFULLY because the critical cross-file same-package call resolves as "package". Rather than widen the rule to make the demo pass, we checked the graph's own `reason` text: "exact" and "package" both read "direct call expression resolved to..." — identical parser certainty, differing only in scope. Weaker resolutions (name_only, import_external, type_inferred, import_resolved, pattern) involve inference. So "package" belongs in CONFIRMED.

That is the curveball's principle applied to ourselves: we did not trust a label until we checked the evidence behind it.

WHY THE NEW RESULT IS SAFE: against a freshly rebuilt merged tree the demo still produces HIGH/CONFIRMED with SERIALIZE_MERGE_ORDER — verified by running the real tool, which is how calibration 2 was caught. 17 tests pass covering the fully-resolved regression guard, the heuristic-edge case (capped MEDIUM, tagged, hint present), and two incomplete-analysis cases (partial_failures > 0, budget-exceeded diff). Every finding states its tier and weakest supporting edge; anything below CONFIRMED is capped and carries a concrete next step.

Incomplete-analysis coverage is provided by tests rather than the distributed fixture; organisers confirmed this was acceptable.

## Checkpoint links and what each checkpoint proves

- ed9c6964fc35 (commit 04103ce) — pre-noon stable state: intent, architecture, rejected approaches (textual overlap, custom parser, DEFINES/CONTAINS traversal), the 6,309/11,397 measurement that forced the precision redesign, the verification chain, and the open hub-node risk.
- 7ca2b8503a0f (commit 59936d8) — Curveball adaptation: the invalidated assumption, the evidence-tier design, both calibration misses recorded honestly, the live regression and its diagnosis, and the preserved-behaviour verification.

Checkpoint ref: https://github.com/Divyashree4757/entire-graph/tree/entire/checkpoints/v1
Entire project: https://entire.io/gh/Divyashree4757/entire-graph

The first checkpoint intentionally merges the "initial architecture" and "pre-noon stable state" milestones. Additional checkpoints on pr-a (81e973a4b0a2, 7d317f88bf80) capture demo construction and build-independence verification.

## Setup, run and test instructions

Prerequisites: Python 3.9+, Entire CLI, graph plugin (`entire plugin install graph`; `entire graph init-agents --repo .`).

    cd interference
    python pr_interference.py --repo .. --base <base> --pr-a <a> --pr-b <b>
    python -m pytest tests/ -v      # 17 tests

Run from inside interference/ (it imports entire_graph_adapter as a sibling). Reproduce the demo:

    git checkout -b demo-view && git merge pr-a -m x && git merge pr-b -m x
    cd interference && python pr_interference.py --repo .. --base entire-interference --pr-a pr-a --pr-b pr-b
    cd .. && CC=clang CXX=clang++ CGO_ENABLED=1 go build ./...   # fails on exactly that symbol
    git checkout entire-interference && git branch -D demo-view

Both branches must be present in the working tree for the graph to resolve the new caller — analysis runs against the merged view, the state whose safety is in question.

Flags: --depth N (default 1), --out FILE, --debug-shapes.

## Databricks use, data sources and limitations

Not used. A Delta-backed history layer was designed but not built within the time budget, and we are not claiming it. All graph analysis runs locally with no model calls or network requests.

## Known limitations and next steps

Hub-node propagation (known, measured, unfixed): SymbolRecord has 779 incoming type edges, so at depth >= 2 the BFS spills through it — depth 1 gives 3 overlaps, depth 2 gives 357, depth 3 gives 1,609. Default is depth 1, the highest-precision setting, but it under-detects transitive interference. Designed fix: cap propagation through any node whose fan-in exceeds a threshold, as low-precision seeds already are.

Other limitations: config-file coupling is invisible (JSON/Markdown/YAML are inventory-only in the graph) — the tool reports this rather than hiding it. Demo branches were cut before the last base commit, so our own tooling files appear as lower-ranked findings. Traversal is undirected. Two PRs at a time; a merge queue needs N-way analysis. CONFIRMED_RESOLUTIONS is calibrated against this graph's Go output.

Next steps: hub-node fan-in capping; deriving CONFIRMED_RESOLUTIONS from capabilities; a CI check posting the evidence path and tier as a PR comment, gating on HIGH/CONFIRMED only; N-way analysis.
