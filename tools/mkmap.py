import json, glob, os, sys, collections
from coverage import CoverageData
S, ROOT, TESTS, OUT = sys.argv[1:5]
tests = json.load(open(TESTS))["tests"]
num = {t["id"]: t["number"] for t in tests}
cls_of = collections.defaultdict(set)
by_file = collections.defaultdict(set)
for t in tests:
    by_file[t["file"]].add(t["number"])
for t in tests:
    cls_of[t["id"].rsplit("::", 1)[0]].add(t["number"])

def ctx_tests(ctx):
    if "|" not in ctx:
        return None
    ph, nid = ctx.split("|", 1)
    if ph == "M":
        return set(by_file.get(nid, ()))
    if nid not in num:
        return set()
    if ph == "S":
        return cls_of[nid.rsplit("::", 1)[0]] | {num[nid]}
    return {num[nid]}

lines = collections.defaultdict(lambda: collections.defaultdict(set))
importlines = collections.defaultdict(set)
for f in glob.glob(os.path.join(S, "cov.*")):
    d = CoverageData(f); d.read()
    for fn in d.measured_files():
        rel = os.path.relpath(fn, ROOT)
        for ln, ctxs in d.contexts_by_lineno(fn).items():
            mod_only = set()
            per_test = False
            for c in ctxs:
                if c.startswith("M|") and rel.startswith("sqlglot/"):
                    mod_only |= ctx_tests(c)
                    continue
                if "|" in c:
                    per_test = True
            if mod_only and not per_test:
                # run only while some test module imports: those modules' tests
                lines[rel][ln] |= mod_only
            for c in ctxs:
                if c.startswith("M|") and rel.startswith("sqlglot/"):
                    continue
                ts = ctx_tests(c)
                if ts is None:
                    if c == "import":
                        importlines[rel].add(ln)
                    continue
                lines[rel][ln] |= ts
extra = collections.defaultdict(lambda: collections.defaultdict(set))
for f in glob.glob(os.path.join(S, "extra.*.json")):
    for c, v in json.load(open(f)).items():
        ts = ctx_tests(c)
        if not ts:
            continue
        for k, vals in v.items():
            for x in vals:
                extra[k][x] |= ts
sets, setidx = [], {}
def setid(ts):
    k = tuple(sorted(ts))
    if k not in setidx:
        setidx[k] = len(sets)
        sets.append(list(k))
    return setidx[k]
out = {
    "ids": [t["id"] for t in tests],
    "lines": {f: {str(l): setid(ts) for l, ts in sorted(d.items())} for f, d in sorted(lines.items())},
    "import_lines": {f: sorted(v) for f, v in sorted(importlines.items())},
    "files": {k: sorted(v) for k, v in sorted(extra["files"].items())},
    "dialects": {k: sorted(v) for k, v in sorted(extra["dialects"].items())},
    "exprs": {k: sorted(v) for k, v in sorted(extra["exprs"].items())},
}
out["sets"] = sets
json.dump(out, open(OUT, "w"), separators=(",", ":"))
print(len(out["lines"]), sum(len(v) for v in out["lines"].values()), len(out["files"]), len(out["dialects"]), len(out["exprs"]))
print(os.path.getsize(OUT))
