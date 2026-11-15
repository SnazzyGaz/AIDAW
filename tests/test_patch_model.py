import copy
import json
import random

from plaindaw import patch as P
from plaindaw.model import dumps, new_project, normalize, validate


def rand_doc(rng, depth=0):
    if depth > 3 or rng.random() < 0.3:
        return rng.choice([1, 2.5, "a", "b", True, None, [1, 2], {"x": 1}])
    if rng.random() < 0.5:
        return {k: rand_doc(rng, depth + 1) for k in rng.sample("abcdef", rng.randint(0, 4))}
    return [rand_doc(rng, depth + 1) for _ in range(rng.randint(0, 5))]


def mutate(rng, d):
    d = copy.deepcopy(d)
    if isinstance(d, dict):
        if d and rng.random() < 0.3:
            d.pop(rng.choice(list(d)))
        if rng.random() < 0.4:
            d[rng.choice("abcdefgh")] = rand_doc(rng, 2)
        for k in list(d):
            if rng.random() < 0.4:
                d[k] = mutate(rng, d[k])
    elif isinstance(d, list):
        if d and rng.random() < 0.3:
            d.pop(rng.randrange(len(d)))
        if rng.random() < 0.4:
            d.insert(rng.randint(0, len(d)), rand_doc(rng, 2))
        for i in range(len(d)):
            if rng.random() < 0.3:
                d[i] = mutate(rng, d[i])
    elif rng.random() < 0.5:
        return rand_doc(rng, 3)
    return d


def test_patch_roundtrip_property():
    rng = random.Random(42)
    for _ in range(600):
        a = {"root": rand_doc(rng)}
        b = mutate(rng, a)
        fwd = P.diff(a, b)
        assert P.apply(copy.deepcopy(a), fwd) == b
        inv = P.invert(a, fwd)
        assert P.apply(copy.deepcopy(b), inv) == a


def test_list_append_is_small():
    a = {"l": list(range(50))}
    b = {"l": list(range(50)) + [99]}
    fwd = P.diff(a, b)
    assert fwd == [{"op": "add", "path": ["l", 50], "value": 99}]


def test_canonical_json_idempotent_and_stable():
    d = normalize(new_project("x", 128))
    d["patterns"]["p"] = {"lengthBeats": 4, "notes": [{"t": 0.1 + 0.2, "len": 1, "key": 60, "vel": 100}]}
    s1 = dumps(d)
    s2 = dumps(json.loads(s1))
    assert s1 == s2
    assert '"t": 0.3' in s1          # floats rounded
    assert s1.index('"format"') < s1.index('"tracks"')


def test_validate_catches_problems():
    d = normalize(new_project())
    assert validate(d) == []
    bad = copy.deepcopy(d)
    bad["tracks"].append({"id": "a", "type": "audio", "name": "A", "output": "nope", "clips": [], "mix": {}})
    assert any("output" in e for e in validate(bad))
    loop = copy.deepcopy(d)
    loop["tracks"][:0] = [{"id": "b1", "type": "bus", "name": "b1", "output": "b2", "mix": {}},
                          {"id": "b2", "type": "bus", "name": "b2", "output": "b1", "mix": {}}]
    assert any("loop" in e.lower() or "cycle" in e.lower() for e in validate(loop))
