"""Turn junit xml files from mutate_eval into an answers.json (failing test numbers)."""
import json, sys, os, glob
import xml.etree.ElementTree as ET
OUT, TESTS = sys.argv[1], sys.argv[2]
tests = json.load(open(TESTS))["tests"]
num = {t["id"]: t["number"] for t in tests}
ans = {}
for x in sorted(glob.glob(os.path.join(OUT, "*.xml"))):
    cid = os.path.basename(x)[:-4]
    failing = set()
    for tc in ET.parse(x).iter("testcase"):
        if any(ch.tag in ("failure", "error") for ch in tc):
            cls = tc.get("classname", "")
            parts = cls.split(".")
            # classname like tests.dialects.test_x.TestX
            path = "/".join(parts[:-1]) + ".py"
            nid = f"{path}::{parts[-1]}::{tc.get('name')}"
            if nid in num:
                failing.add(num[nid])
            else:
                # collection errors: whole file
                for t in tests:
                    if t["file"] == path or t["file"] == cls.replace(".", "/") + ".py":
                        failing.add(t["number"])
    ans[cid] = {"failing": sorted(failing), "kind": "mutant"}
json.dump({"commits": ans}, open(os.path.join(OUT, "answers.json"), "w"), indent=1)
print(len(ans), sum(1 for v in ans.values() if v["failing"]))
