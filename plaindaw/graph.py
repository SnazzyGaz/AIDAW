"""Signal-flow graph: processing order of tracks (outputs, sends, sidechains)."""
from __future__ import annotations


class GraphError(ValueError):
    pass


def deps(doc: dict) -> dict[str, set[str]]:
    """track id -> ids it needs rendered before it."""
    d = {t["id"]: set() for t in doc["tracks"]}
    for t in doc["tracks"]:
        if t.get("type") != "master" and t.get("output") in d:
            d[t["output"]].add(t["id"])
        for s in t.get("sends", []):
            if s.get("to") in d:
                d[s["to"]].add(t["id"])
        for p in t.get("inserts", []):
            if p.get("sidechain") in d:
                d[t["id"]].add(p["sidechain"])
    return d


def order(doc: dict) -> list[str]:
    d = deps(doc)
    out, state = [], {}

    def visit(n, stack):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise GraphError("routing loop: " + " -> ".join(stack + [n]))
        state[n] = 1
        for m in sorted(d[n]):
            visit(m, stack + [n])
        state[n] = 2
        out.append(n)

    for t in doc["tracks"]:
        visit(t["id"], [])
    return out
