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


def load_symbols(repo) -> dict:
    """symbol_id -> {id, path, name, kind}"""
    symbols = {}
    for rec in _stream(["entire", "graph", "symbols", "--repo", ".",
                        "--format", "ndjson"], repo):
        if rec.get("record_type") not in (None, "symbol", "entity", "node"):
            continue
        sid = rec.get("id") or rec.get("symbol_id")
        if not sid:
            continue
        symbols[sid] = {
            "id": sid,
            "path": rec.get("path") or rec.get("file") or rec.get("file_path"),
            "name": rec.get("name") or rec.get("symbol") or "",
            "kind": rec.get("kind") or rec.get("symbol_kind") or "",
        }
    return symbols


def load_edges(repo, relations=DEPENDENCY_RELATIONS):
    """Returns (adjacency, edge_types). Undirected: interference propagates
    both ways — a caller is affected by its callee changing, and a callee is
    affected by gaining a new caller with different expectations."""
    adj = defaultdict(set)
    etype = {}
    for rec in _stream(["entire", "graph", "edges", "--repo", ".",
                        "--format", "ndjson"], repo):
        if rec.get("record_type") not in (None, "relation", "edge"):
            continue
        t = rec.get("type") or rec.get("relation")
        if relations and t not in relations:
            continue
        a, b = rec.get("from_id"), rec.get("to_id")
        if not a or not b:
            continue
        adj[a].add(b)
        adj[b].add(a)
        etype[(a, b)] = etype[(b, a)] = t
    return adj, etype


def semantic_diff(repo, base, head) -> list:
    out = subprocess.run(["entire", "graph", "diff", "--base", base,
                          "--head", head, "--json"],
                         cwd=repo, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"graph diff failed:\n{out.stderr}")
    data = json.loads(out.stdout)

    changes = []
    for f in data.get("files", []):
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
    return changes


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
