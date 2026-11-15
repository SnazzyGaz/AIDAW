"""Minimal structural diff/patch for JSON documents (used for undo/redo).

A patch is a list of steps {"op": "replace"|"add"|"remove", "path": [...], "value": ...}.
Lists are diffed by trimming the common prefix/suffix, so appending a track is a
single "add" instead of a whole-list replace.
"""
from __future__ import annotations

import copy
import difflib
import json
from typing import Any

_MISSING = object()


def diff(a: Any, b: Any, path: list | None = None) -> list[dict]:
    path = path or []
    if type(a) is not type(b) and not (isinstance(a, (int, float)) and isinstance(b, (int, float))
                                       and not isinstance(a, bool) and not isinstance(b, bool)):
        return [{"op": "replace", "path": path, "value": copy.deepcopy(b)}]
    if isinstance(a, dict):
        out = []
        for k in a:
            if k not in b:
                out.append({"op": "remove", "path": path + [k], "old": copy.deepcopy(a[k])})
            else:
                out.extend(diff(a[k], b[k], path + [k]))
        for k in b:
            if k not in a:
                out.append({"op": "add", "path": path + [k], "value": copy.deepcopy(b[k])})
        return out
    if isinstance(a, list):
        if len(a) == len(b) and all(_key(x) == _key(y) for x, y in zip(a, b)):
            out = []
            for i, (x, y) in enumerate(zip(a, b)):
                out.extend(diff(x, y, path + [i]))
            return out
        ka, kb = [_key(x) for x in a], [_key(x) for x in b]
        ops = difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes()
        out = []
        # walk blocks from the end so earlier indices stay valid while applying in order
        for tag, i1, i2, j1, j2 in reversed(ops):
            if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
                for k in range(i2 - i1 - 1, -1, -1):
                    out.extend(diff(a[i1 + k], b[j1 + k], path + [i1 + k]))
                continue
            for i in range(i2 - 1, i1 - 1, -1):
                out.append({"op": "remove", "path": path + [i], "old": copy.deepcopy(a[i])})
            for k, j in enumerate(range(j1, j2)):
                out.append({"op": "add", "path": path + [i1 + k], "value": copy.deepcopy(b[j])})
        return out
    if a != b:
        return [{"op": "replace", "path": path, "value": copy.deepcopy(b), "old": copy.deepcopy(a)}]
    return []


def _key(x):
    """Identity used to align list items: dicts with an id match by id, else by content."""
    if isinstance(x, dict) and isinstance(x.get("id"), str):
        return "id:" + x["id"]
    return json.dumps(x, sort_keys=True, default=str)


def _parent(doc, path):
    cur = doc
    for k in path[:-1]:
        cur = cur[k]
    return cur


def get(doc, path, default=_MISSING):
    cur = doc
    try:
        for k in path:
            cur = cur[k]
        return cur
    except (KeyError, IndexError, TypeError):
        if default is _MISSING:
            raise
        return default


def apply(doc, patch: list[dict]):
    """Apply in place (returns doc). A replace at [] swaps the whole doc."""
    for st in patch:
        p = st["path"]
        if not p:
            if st["op"] == "replace":
                doc = copy.deepcopy(st["value"])
                continue
            raise ValueError("cannot add/remove root")
        par = _parent(doc, p)
        k = p[-1]
        if st["op"] == "replace":
            par[k] = copy.deepcopy(st["value"])
        elif st["op"] == "add":
            if isinstance(par, list):
                par.insert(k, copy.deepcopy(st["value"]))
            else:
                par[k] = copy.deepcopy(st["value"])
        elif st["op"] == "remove":
            del par[k]
        else:
            raise ValueError(st["op"])
    return doc


def invert(before, patch: list[dict]) -> list[dict]:
    """Inverse of `patch` given the document state *before* it was applied."""
    inv = []
    cur = copy.deepcopy(before)
    for st in patch:
        p = st["path"]
        if st["op"] == "replace":
            inv.append({"op": "replace", "path": p, "value": copy.deepcopy(get(cur, p)) if p else copy.deepcopy(cur)})
        elif st["op"] == "add":
            par = _parent(cur, p)
            if isinstance(par, list) or p[-1] not in par:
                inv.append({"op": "remove", "path": p})
            else:
                inv.append({"op": "replace", "path": p, "value": copy.deepcopy(par[p[-1]])})
        elif st["op"] == "remove":
            inv.append({"op": "add", "path": p, "value": copy.deepcopy(get(cur, p))})
        cur = apply(cur, [st])
    inv.reverse()
    return inv


def touched(patch: list[dict]) -> list[str]:
    return sorted({"/".join(str(x) for x in st["path"]) or "/" for st in patch})
