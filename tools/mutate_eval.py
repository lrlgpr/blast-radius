"""Offline recall check: make random single-line mutations of sqlglot, find the
tests that really fail (full suite), and see whether the selector's candidate
set covers them. Not used at run time.

usage: mutate_eval.py SQLGLOT_DIR OUT_DIR N SEED
"""
import json, os, random, re, subprocess, sys, shutil, ast

SG, OUT, N, SEED = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
random.seed(SEED)
os.makedirs(os.path.join(OUT, "commits"), exist_ok=True)

SWAPS = [
    (r" and ", " or "), (r" or ", " and "), (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r" is not ", " is "), (r" is None", " == None"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bnot ", ""), (r"\+ 1\b", "- 1"), (r"\b0\b", "1"), (r"\b1\b", "2"),
]

files = []
for d, _, fs in os.walk(os.path.join(SG, "sqlglot")):
    for f in fs:
        if f.endswith(".py"):
            files.append(os.path.relpath(os.path.join(d, f), SG))
files.sort()
made = 0
tries = 0
while made < N and tries < N * 50:
    tries += 1
    rel = random.choice(files)
    src = open(os.path.join(SG, rel)).read().split("\n")
    i = random.randrange(len(src))
    line = src[i]
    if line.strip().startswith("#") or not line.strip():
        continue
    pat, rep = random.choice(SWAPS)
    ms = list(re.finditer(pat, line))
    if not ms:
        continue
    m = random.choice(ms)
    new = line[: m.start()] + rep + line[m.end():]
    mod = src[:]
    mod[i] = new
    try:
        ast.parse("\n".join(mod))
    except SyntaxError:
        continue
    cid = f"m{SEED}{made:03d}"
    work = os.path.join(OUT, "work")
    if os.path.exists(work):
        shutil.rmtree(work)
    shutil.copytree(SG, work, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    open(os.path.join(work, rel), "w").write("\n".join(mod))
    diff = subprocess.run(["git", "diff", "--no-index", "--", os.path.join(SG, rel), os.path.join(work, rel)], capture_output=True, text=True).stdout
    diff = diff.replace(os.path.join(SG, rel).lstrip("/"), rel).replace(os.path.join(work, rel).lstrip("/"), rel)
    diff = re.sub(r"^diff --git .*$", f"diff --git a/{rel} b/{rel}", diff, count=1, flags=re.M)
    diff = re.sub(r"^--- .*$", f"--- a/{rel}", diff, count=1, flags=re.M)
    diff = re.sub(r"^\+\+\+ .*$", f"+++ b/{rel}", diff, count=1, flags=re.M)
    open(os.path.join(OUT, "commits", cid + ".patch"), "w").write(diff)
    junit = os.path.join(OUT, cid + ".xml")
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=".")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", "8", "-p", "no:cacheprovider", "--timeout", "120",
                    "tests", "--junitxml", junit], cwd=work, env=env, capture_output=True)
    print(cid, rel, i + 1, repr(line.strip()[:60]), "->", repr(new.strip()[:60]), flush=True)
    made += 1
