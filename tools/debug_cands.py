import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selector as S
tests = json.loads((S.DATA / "tests.json").read_text())["tests"]
ans = json.load(open(sys.argv[1]))["commits"]
sel = S.Selector(tests)
sys.path.insert(0, str(S.SRC))
for p in sorted((S.DATA / "commits").glob("*.patch")):
    if len(sys.argv) > 2 and p.stem not in sys.argv[2:]:
        continue
    files = S.parse_patch(p.read_text())
    cand, must, notes, tp = sel.analyse(files)
    fail = set(ans[p.stem]["failing"])
    miss = fail - cand - must
    print(p.stem, "cand", len(cand), "fail", len(fail), "miss", [sel.m.id_of[n] for n in sorted(miss)][:8], notes, [(f.old, f.new, len(f.hunks)) for f in files])

if __name__ == "__main__" and os.environ.get("HEAVY"):
    for p in sorted((S.DATA / "commits").glob("*.patch")):
        files = S.parse_patch(p.read_text())
        cand, must, notes, tp = sel.analyse(files)
        top = sorted(cand, key=lambda n: -sel.m.secs[n])[:5]
        print(p.stem, round(sum(sel.m.secs[n] for n in cand)/1000,1), [(sel.m.id_of[n].split("::")[-1], sel.m.secs[n]/1000, n in set(ans[p.stem]["failing"])) for n in top])
