"""Blast Radius selector.

For every patch:
  1. work out which tests could possibly notice the change, from a per-test
     coverage / fixture / dialect-usage map built once on the pinned commit;
  2. run those candidates for real against a patched copy of sqlglot, in a
     forked child that already has pytest (and usually sqlglot) imported;
  3. print only the tests that actually failed (plus any expensive candidates
     that were cheaper to hand to the CI workers than to run here).
"""

import ast
import json
import os
import re
import select
import signal
import sys
import time
from pathlib import Path

SRC = Path(os.environ.get("BR_SRC", "/app/src"))
MAP = Path(os.environ.get("BR_MAP", "/app/map.json"))
DATA = Path(os.environ.get("BR_DATA", "/data"))
TMP = Path(os.environ.get("BR_TMP", "/tmp"))

MAX_WORKERS = 8
WORKER_MS = 2000
CPU_PER_MS = 1.15  # CPU ms we expect to burn per recorded test ms
CHILD_MS = 600  # fixed CPU cost of one child run
CHILD_PURGE_MS = 900  # extra when sqlglot itself has to be re-imported
T0 = time.time()
DEADLINE = float(os.environ.get("BR_DEADLINE", "120"))  # stop starting runs after this (s)
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")


def log(*a):
    print(f"[{time.time() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- patch


class FilePatch:
    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []  # (old_start, [lines])
        self.binary = False

    @property
    def path(self):
        return self.new if self.new not in (None, "/dev/null") else self.old


def _strip(p):
    p = p.split("\t")[0].strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return p
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch()
            files.append(cur)
            m = re.match(r"diff --git (\S+) (\S+)", ln)
            if m:
                cur.old, cur.new = _strip(m.group(1)), _strip(m.group(2))
        elif ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if cur is None or cur.hunks:
                cur = FilePatch()
                files.append(cur)
            cur.old = _strip(ln[4:])
            cur.new = _strip(lines[i + 1][4:])
            i += 1
        elif ln.startswith("rename from ") and cur:
            cur.old = ln[len("rename from "):].strip()
        elif ln.startswith("rename to ") and cur:
            cur.new = ln[len("rename to "):].strip()
        elif ln.startswith("new file mode") and cur:
            cur.old = "/dev/null"
        elif ln.startswith("deleted file mode") and cur:
            cur.new = "/dev/null"
        elif (ln.startswith("Binary files") or ln.startswith("GIT binary patch")) and cur:
            cur.binary = True
        elif ln.startswith("@@") and cur:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            old_len = int(m.group(2)) if m.group(2) is not None else 1
            new_len = int(m.group(4)) if m.group(4) is not None else 1
            body = []
            i += 1
            o = n = 0
            while i < len(lines) and (o < old_len or n < new_len):
                b = lines[i]
                if b.startswith("\\"):
                    body.append(b)
                    i += 1
                    continue
                tag = b[:1] if b else " "
                if tag == " ":
                    o += 1
                    n += 1
                elif tag == "-":
                    o += 1
                elif tag == "+":
                    n += 1
                else:
                    break
                body.append(b if b else " ")
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            cur.hunks.append((int(m.group(1)), body))
            continue
        i += 1
    return [f for f in files if f.old or f.new]


def apply_hunks(old_text, hunks):
    old = old_text.split("\n") if old_text else []
    # keep track of trailing newline
    out = []
    pos = 0  # index into old (0-based)
    no_eol_new = False
    for start, body in hunks:
        s = max(start - 1, 0) if start > 0 else 0
        out.extend(old[pos:s])
        pos = s
        prev = None
        for b in body:
            if b.startswith("\\"):
                if prev == "+" or prev == " ":
                    no_eol_new = True
                continue
            tag, rest = b[0], b[1:]
            if tag == " ":
                out.append(rest)
                pos += 1
            elif tag == "-":
                pos += 1
            elif tag == "+":
                out.append(rest)
            prev = tag
    out.extend(old[pos:])
    text = "\n".join(out)
    if no_eol_new and text.endswith("\n"):
        text = text[:-1]
    return text


def touched_old_lines(hunks):
    """Old-file line numbers removed, and insertion gaps (after old line g)."""
    removed, gaps = set(), set()
    for start, body in hunks:
        o = start
        for b in body:
            if b.startswith("\\"):
                continue
            tag = b[0]
            if tag == " ":
                o += 1
            elif tag == "-":
                removed.add(o)
                o += 1
            elif tag == "+":
                gaps.add(o - 1)
    return removed, gaps


def change_blocks(hunks):
    """Runs of removed/added lines: [(old line numbers, new line numbers)], plus
    a map from unchanged new-file lines to their old-file numbers."""
    blocks, new_to_old = [], {}
    for start, body in hunks:
        o = start
        # new-file start: hunks apply in order, track drift
        n = start + sum(len(a) - len(r) for r, a in blocks)
        rem, add = [], []
        for b in body:
            if b.startswith("\\"):
                continue
            tag = b[0]
            if tag == " ":
                if rem or add:
                    blocks.append((rem, add))
                    rem, add = [], []
                new_to_old[n] = o
                o += 1
                n += 1
            elif tag == "-":
                rem.append(o)
                o += 1
            elif tag == "+":
                add.append(n)
                n += 1
        if rem or add:
            blocks.append((rem, add))
    return blocks, new_to_old


_NONCODE = {}


def noncode_lines(text):
    """Lines holding only comments / whitespace, according to the tokenizer."""
    key = hash(text)
    if key in _NONCODE:
        return _NONCODE[key]
    import io
    import tokenize

    code = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            for ln in range(tok.start[0], tok.end[0] + 1):
                code.add(ln)
    except Exception:
        return set()
    n = text.count("\n") + 1
    res = {ln for ln in range(1, n + 1) if ln not in code}
    _NONCODE[key] = res
    return res


# --------------------------------------------------------------------------- map


class Map:
    def __init__(self, tests):
        raw = json.loads(MAP.read_text())
        byid = {t["id"]: t["number"] for t in tests}
        self.secs = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
        self.file_of = {t["number"]: t["file"] for t in tests}
        self.id_of = {t["number"]: t["id"] for t in tests}
        self.all = set(self.secs)
        idx = [byid.get(i) for i in raw["ids"]]

        def conv(lst):
            return frozenset(idx[j] for j in lst if idx[j] is not None)

        self._raw_lines = raw["lines"]
        self._sets = raw["sets"]
        self._set_cache = {}
        self._lines_cache = {}
        self._conv = conv
        self.import_lines = {f: set(v) for f, v in raw["import_lines"].items()}
        self.files = {k: conv(v) for k, v in raw["files"].items()}
        self.dialects = {k: conv(v) for k, v in raw["dialects"].items()}
        self.exprs = {k: conv(v) for k, v in raw["exprs"].items()}
        self.by_file = {}
        for n, f in self.file_of.items():
            self.by_file.setdefault(f, set()).add(n)

    def lines(self, f):
        d = self._lines_cache.get(f)
        if d is None:
            d = {}
            for k, v in self._raw_lines.get(f, {}).items():
                s = self._set_cache.get(v)
                if s is None:
                    s = self._set_cache[v] = self._conv(self._sets[v])
                d[int(k)] = s
            self._lines_cache[f] = d
        return d

    def cover(self, f, lo, hi):
        d = self.lines(f)
        out = set()
        for ln in range(lo, hi + 1):
            s = d.get(ln)
            if s:
                out |= s
        return out

    def any_import_line(self, f, lo, hi):
        il = self.import_lines.get(f, ())
        return any(ln in il for ln in range(lo, hi + 1))

    def file_cover(self, f):
        out = set()
        for s in self.lines(f).values():
            out |= s
        return out


# --------------------------------------------------------------------------- source analysis

_SRC_CACHE = {}
_AST_CACHE = {}


def src_text(rel):
    if rel not in _SRC_CACHE:
        try:
            _SRC_CACHE[rel] = (SRC / rel).read_text(encoding="utf-8")
        except Exception:
            _SRC_CACHE[rel] = None
    return _SRC_CACHE[rel]


def src_ast(rel):
    if rel not in _AST_CACHE:
        t = src_text(rel)
        try:
            _AST_CACHE[rel] = ast.parse(t) if t is not None else None
        except SyntaxError:
            _AST_CACHE[rel] = None
    return _AST_CACHE[rel]


def module_name(rel):
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


_ALL_PY = None


def all_sqlglot_py():
    global _ALL_PY
    if _ALL_PY is None:
        _ALL_PY = sorted(
            str(p.relative_to(SRC)) for p in (SRC / "sqlglot").rglob("*.py")
        )
    return _ALL_PY


def _node_start(n):
    decs = getattr(n, "decorator_list", None)
    if decs:
        return min(d.lineno for d in decs)
    return n.lineno


class Scope:
    """Where a line sits: chain of enclosing class/def nodes plus the innermost stmt."""

    def __init__(self, chain, stmt):
        self.chain = chain  # [(node, in_body)] outermost first
        self.stmt = stmt


def locate(tree, line):
    chain = []
    stmt = None
    body_owner = tree
    while True:
        found = None
        for fld in ("body", "orelse", "finalbody", "handlers", "cases"):
            for ch in getattr(body_owner, fld, []) or []:
                if isinstance(ch, (ast.excepthandler,)) or (hasattr(ast, "match_case") and isinstance(ch, ast.match_case)):
                    lo = getattr(ch, "lineno", None) or (ch.pattern.lineno if hasattr(ch, "pattern") else None)
                    hi = getattr(ch, "end_lineno", None) or (ch.body[-1].end_lineno if ch.body else lo)
                    if lo and lo <= line <= hi:
                        found = ch
                        break
                    continue
                if not isinstance(ch, ast.stmt):
                    continue
                if _node_start(ch) <= line <= ch.end_lineno:
                    found = ch
                    break
            if found:
                break
        if found is None:
            return chain, stmt
        if isinstance(found, ast.stmt):
            stmt = found
        if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body_lo = found.body[0].lineno if found.body else found.end_lineno + 1
            if line < body_lo or (found.body and found.body[0].lineno == found.lineno and line == found.lineno and not isinstance(found, ast.ClassDef)):
                chain.append((found, False))
                return chain, found
            chain.append((found, True))
        body_owner = found


def header_range(stmt):
    lo = _node_start(stmt)
    body = getattr(stmt, "body", None)
    if body and isinstance(body, list) and body and isinstance(body[0], ast.AST) and hasattr(body[0], "lineno"):
        hi = max(lo, body[0].lineno - 1)
        return lo, hi
    return lo, stmt.end_lineno


_POS = ("lineno", "col_offset", "end_lineno", "end_col_offset", "ctx", "type_comment")


def _diff_in_lambdas(a, b, lam, out):
    """Compare two ASTs; True if every difference sits inside some lambda's body
    (those lambdas are appended to `out`)."""
    if type(a) is not type(b):
        if lam is None:
            return False
        out.append(lam)
        return True
    if isinstance(a, ast.AST):
        for f in a._fields:
            if f in _POS:
                continue
            inner = lam
            if isinstance(a, ast.Lambda) and f == "body":
                inner = a
            if not _diff_in_lambdas(getattr(a, f, None), getattr(b, f, None), inner, out):
                return False
        return True
    if isinstance(a, list):
        if len(a) != len(b):
            if lam is None:
                return False
            out.append(lam)
            return True
        return all(_diff_in_lambdas(x, y, lam, out) for x, y in zip(a, b))
    if a != b:
        if lam is None:
            return False
        out.append(lam)
    return True


def _diffs(a, b, parent, out):
    """Collect (old, new, parent) for every differing leaf between two ASTs."""
    if type(a) is not type(b):
        out.append((a, b, parent))
        return
    if isinstance(a, ast.AST):
        if isinstance(a, ast.cmpop):
            return
        for f in a._fields:
            if f in _POS:
                continue
            _diffs(getattr(a, f, None), getattr(b, f, None), a, out)
        return
    if isinstance(a, list):
        if len(a) != len(b):
            out.append((a, b, parent))
            return
        for x, y in zip(a, b):
            _diffs(x, y, parent, out)
        return
    if a != b:
        out.append((a, b, parent))


_SAFE_SWAPS = ({ast.Is, ast.Eq}, {ast.IsNot, ast.NotEq})


def is_identity_swap(old_stmt, new_stmt, module):
    """`x is C` <-> `x == C` (or the negated pair) where C is None/True/False or an
    Enum member: the two spellings agree for every object sqlglot compares."""
    diffs = []
    _diffs(old_stmt, new_stmt, None, diffs)
    if len(diffs) != 1:
        return False
    a, b, parent = diffs[0]
    if not (isinstance(a, ast.cmpop) and isinstance(b, ast.cmpop) and isinstance(parent, ast.Compare)):
        return False
    if {type(a), type(b)} not in _SAFE_SWAPS or len(parent.ops) != 1:
        return False
    for side in (parent.left, parent.comparators[0]):
        if isinstance(side, ast.Constant) and (side.value is None or side.value is True or side.value is False):
            return True
        if isinstance(side, ast.Attribute) and isinstance(side.value, ast.Name) and module is not None:
            import enum

            base = getattr(module, side.value.id, None)
            if isinstance(base, type) and issubclass(base, enum.Enum) and "__eq__" not in base.__dict__:
                return True
    return False


def strip_docstrings(tree):
    import copy

    t = copy.deepcopy(tree)
    for n in ast.walk(t):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
                b[0] = ast.Pass()
    return t


_CLASS_RANGES = {}


def mod_rel(mod):
    base = mod.replace(".", "/")
    for cand in (base + ".py", base + "/__init__.py"):
        if (SRC / cand).exists():
            return cand
    return None


def class_range(mod, qualname):
    """(rel, first line, last line) of a class given its module and qualname."""
    rel = mod_rel(mod) if mod else None
    if rel is None:
        return None
    idx = _CLASS_RANGES.get(rel)
    if idx is None:
        idx = {}
        tree = src_ast(rel)

        def walk(body, prefix):
            for n in body:
                if isinstance(n, ast.ClassDef):
                    q = prefix + n.name
                    idx[q] = (n.lineno, n.end_lineno)
                    walk(n.body, q + ".")
                elif isinstance(n, (ast.If, ast.Try)):
                    walk(getattr(n, "body", []) + getattr(n, "orelse", []), prefix)

        if tree is not None:
            walk(tree.body, "")
        _CLASS_RANGES[rel] = idx
    r = idx.get(qualname)
    return (rel, r[0], r[1]) if r else None


def line_in_header(stmt, line):
    lo, hi = header_range(stmt)
    return lo <= line <= hi


def defined_names(stmt):
    names = set()
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(stmt.name)
    elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        for a in stmt.names:
            names.add((a.asname or a.name).split(".")[0])
    return names


# --------------------------------------------------------------------------- selection


class Selector:
    def __init__(self, tests):
        self.m = Map(tests)
        self.tests = tests
        self._sg = None
        self._refs_cache = {}
        self._users_cache = {}

    # -- live sqlglot objects (original source), for class relationships
    def sg(self):
        if self._sg is None:
            import importlib
            import pkgutil

            import sqlglot
            from sqlglot.dialects.dialect import Dialect

            self._core = {m for m in sys.modules if m == "sqlglot" or m.startswith("sqlglot.")}
            for mi in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
                if mi.name.endswith("__main__"):
                    continue
                try:
                    importlib.import_module(mi.name)
                except Exception:
                    pass
            from sqlglot import expressions as exp

            base = getattr(exp, "Expr", None) or exp.Expression
            dialects = set()
            stack = [Dialect]
            while stack:
                c = stack.pop()
                if c in dialects:
                    continue
                dialects.add(c)
                stack.extend(c.__subclasses__())
            closure = {}
            for d in dialects:
                cl = set()
                for c in d.__mro__:
                    cl.add(c)
                    cl.add(type(c))
                for name in dir(d):
                    try:
                        v = getattr(d, name)
                    except Exception:
                        continue
                    if isinstance(v, type):
                        for c in v.__mro__:
                            cl.add(c)
                            cl.add(type(c))
                closure[d] = cl
            self._sg = (sys.modules, base, closure)
        return self._sg

    def resolve_class(self, rel, chain_names):
        mods, base, closure = self.sg()
        mod = mods.get(module_name(rel))
        obj = mod
        for n in chain_names:
            obj = getattr(obj, n, None) if obj is not None else None
        return obj if isinstance(obj, type) else None

    def class_users(self, rel, cls_chain):
        key = (rel, tuple(cls_chain))
        if key in self._users_cache:
            return self._users_cache[key]
        out = set()
        obj = self.resolve_class(rel, cls_chain)
        tree = src_ast(rel)
        # tests touching the class body's methods in this file
        node = None
        body = tree.body if tree else []
        for n in cls_chain:
            node = next((c for c in body if isinstance(c, ast.ClassDef) and c.name == n), None)
            if node is None:
                break
            body = node.body
        if node is not None:
            out |= self.m.cover(rel, node.lineno, node.end_lineno)
        special = False
        if obj is not None:
            mods, base, closure = self.sg()
            subs = set()
            stack = [obj]
            while stack:
                c = stack.pop()
                if c in subs:
                    continue
                subs.add(c)
                try:
                    stack.extend(type.__subclasses__(c))
                except Exception:
                    pass
            if isinstance(obj, type) and issubclass(obj, base):
                special = True
                for c in subs:
                    out |= self.m.exprs.get(c.__name__, set())
            hit = [d for d, cl in closure.items() if obj in cl]
            if hit:
                special = True
                for d in hit:
                    out |= self.m.dialects.get(d.__name__, set())
            # subclasses defined elsewhere: their own method coverage
            for c in subs:
                if c is obj:
                    continue
                rng = class_range(getattr(c, "__module__", ""), getattr(c, "__qualname__", ""))
                if rng:
                    out |= self.m.cover(*rng)
        res = (out, special)
        self._users_cache[key] = res
        return res

    def refs(self, names, skip=None, depth=0, seen=None):
        """Tests affected through references to `names` anywhere in sqlglot."""
        seen = seen if seen is not None else set()
        out = set()
        names = {n for n in names if n and n not in seen and len(n) > 1}
        if not names or depth > 3:
            return out
        seen |= names
        rx = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
        for rel in all_sqlglot_py():
            text = src_text(rel)
            if not text or not rx.search(text):
                continue
            tree = src_ast(rel)
            for i, line in enumerate(text.split("\n"), 1):
                if skip and (rel, i) in skip:
                    continue
                if not rx.search(line):
                    continue
                out |= self.line_effect(rel, tree, i, depth + 1, seen, via_ref=True)
        return out

    def line_effect(self, rel, tree, line, depth=0, seen=None, via_ref=False):
        """Tests that could see a change on old line `line` of sqlglot file `rel`."""
        if tree is None:
            return self.module_users(rel)
        chain, stmt = locate(tree, line)
        out = set()
        if stmt is None:
            # blank/comment at module level or something we cannot place
            return out if via_ref else set()
        func_idx = None
        for k in range(len(chain) - 1, -1, -1):
            node, in_body = chain[k]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and in_body:
                func_idx = k
                break
        if func_idx is not None:
            # runtime code inside a function body
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and chain[-1][1] is False:
                lo, hi = header_range(stmt)
            elif getattr(stmt, "body", None) and isinstance(stmt.body, list) and stmt.body and hasattr(stmt.body[0], "lineno") and line < stmt.body[0].lineno:
                lo, hi = header_range(stmt)
            elif getattr(stmt, "body", None) and isinstance(stmt.body, list) and not isinstance(stmt, ast.ClassDef):
                # e.g. `else:` line of a compound stmt: header plus following stmts
                lo, hi = header_range(stmt)
                out |= self.m.cover(rel, line, min(line + 3, stmt.end_lineno))
            else:
                lo, hi = _node_start(stmt), stmt.end_lineno
            out |= self.m.cover(rel, lo, hi)
            if via_ref:
                return out
            fnode = chain[func_idx][0]
            if self.m.any_import_line(rel, lo, hi) or self.m.any_import_line(rel, fnode.body[0].lineno, fnode.end_lineno) and not self.m.lines(rel).get(fnode.body[0].lineno):
                # function runs while modules import (builds class tables etc.)
                out |= self.def_effect(rel, tree, chain[: func_idx + 1], fnode, depth, seen)
            return out
        # definition level: class body or module level
        if via_ref:
            # a reference at definition level: whoever uses what it defines
            lo, hi = _node_start(stmt), stmt.end_lineno
            out |= self.m.cover(rel, lo, hi)
            return out | self.def_effect(rel, tree, chain, stmt, depth, seen)
        return self.def_effect(rel, tree, chain, stmt, depth, seen)

    def def_effect(self, rel, tree, chain, stmt, depth, seen, new_stmt=False):
        self.deflevel = True  # something that runs at import changed: import everything to check
        out = set()
        cover = (lambda r, lo, hi: set()) if new_stmt else self.m.cover
        cls_chain = []
        for node, in_body in chain:
            if isinstance(node, ast.ClassDef):
                cls_chain.append(node.name)
            else:
                break
        # the enclosing class (excluding stmt itself if it is a class header)
        owner = list(cls_chain)
        if isinstance(stmt, ast.ClassDef) and owner and owner[-1] == stmt.name and chain and chain[len(owner) - 1][1] is False:
            owner = owner[:-1]
            header_cls = cls_chain
        else:
            header_cls = None
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out |= cover(rel, stmt.lineno, stmt.end_lineno)
        if header_cls:
            u, special = self.class_users(rel, header_cls)
            out |= u
            out |= self.refs({stmt.name}, depth=depth, seen=seen)
            return out
        if owner:
            u, special = self.class_users(rel, owner)
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                refined = self.attr_refine(defined_names(stmt) - {"self"}, None if new_stmt else rel, stmt)
                if refined is not None:
                    # only tests that both use the class and run a line reading the attribute
                    return out | (u & refined) | cover(rel, _node_start(stmt), stmt.end_lineno)
            out |= u
            if not special:
                out |= self.refs(defined_names(stmt) - {"self"}, depth=depth, seen=seen)
            return out
        names = defined_names(stmt)
        if not names:
            return out | self.module_users(rel)
        out |= cover(rel, _node_start(stmt), stmt.end_lineno)
        out |= self.refs(names, depth=depth, seen=seen)
        if isinstance(stmt, (ast.Import, ast.ImportFrom)) and not out:
            pass
        return out

    def attr_refine(self, names, def_rel, stmt):
        """Tests running a line that reads one of `names`, or None when some read
        happens while modules import (metaclasses, derived tables) or outside a
        plain function body, where line coverage cannot follow it."""
        names = {n for n in names if n and len(n) > 1}
        if not names:
            return None
        rx = re.compile(r"\b(" + "|".join(re.escape(n) for n in sorted(names)) + r")\b")
        out = set()
        own = set(range(_node_start(stmt), stmt.end_lineno + 1))
        for rel in all_sqlglot_py():
            text = src_text(rel)
            if not text or not rx.search(text):
                continue
            tree = src_ast(rel)
            if tree is None:
                return None
            il = self.m.import_lines.get(rel, set())
            for i, line in enumerate(text.split("\n"), 1):
                if rel == def_rel and i in own:
                    continue
                code = line.split("#", 1)[0]
                if not rx.search(code):
                    continue
                chain, st = locate(tree, i)
                if st is None:
                    continue
                in_func = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and b for n, b in chain)
                if not in_func:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)) and chain and isinstance(chain[-1][0], ast.ClassDef):
                        # another class setting the same attribute: an override, not a read,
                        # as long as its value does not read the attribute itself
                        val = st.value
                        if val is None or not rx.search(ast.unparse(val)):
                            continue
                    return None
                lo, hi = _node_start(st), st.end_lineno
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return None
                if getattr(st, "body", None) and isinstance(st.body, list) and line_in_header(st, i):
                    lo, hi = header_range(st)
                elif getattr(st, "body", None) and isinstance(st.body, list):
                    lo, hi = i, i
                if any(x in il for x in range(lo, hi + 1)):
                    return None
                # the function holding the read must not run during import either
                fn = [n for n, b in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and b][-1]
                if fn.body and fn.body[0].lineno in il:
                    return None
                out |= self.m.cover(rel, lo, hi)
        # tests that read the attribute themselves
        for f, ts in self.m.by_file.items():
            t = src_text(f)
            if t and rx.search(t):
                tree = src_ast(f)
                for i, line in enumerate(t.split("\n"), 1):
                    if rx.search(line):
                        chain, st = locate(tree, i) if tree else ([], None)
                        fn = next((n for n, b in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
                        if fn is not None and fn.name.startswith("test"):
                            out |= {n for n in ts if self.m.id_of[n].endswith("::" + fn.name)}
                        else:
                            out |= ts
        return out

    def module_users(self, rel):
        out = self.m.file_cover(rel)
        tree = src_ast(rel)
        if tree:
            for n in tree.body:
                if isinstance(n, ast.ClassDef):
                    out |= self.class_users(rel, [n.name])[0]
        out |= self.test_importers(module_name(rel))
        return out

    def test_importers(self, mod):
        out = set()
        short = mod.split(".")[-1]
        rx = re.compile(r"\b" + re.escape(mod) + r"\b|import .*\b" + re.escape(short) + r"\b")
        for f in self.m.by_file:
            t = src_text(f)
            if t and rx.search(t):
                out |= self.m.by_file[f]
        return out

    def is_core_module(self, rel):
        mod = module_name(rel)
        return mod in self._core_modules()

    def _core_modules(self):
        self.sg()
        return self._core

    # -- top level per patch
    def analyse(self, files):
        """Return (candidates:set, must:set, notes:list, touches_sqlglot_py:bool)."""
        cand, must, notes = set(), set(), []
        touches_py = False
        self.deflevel = False
        for fp in files:
            path = fp.path
            old_rel = fp.old if fp.old != "/dev/null" else None
            new_rel = fp.new if fp.new != "/dev/null" else None
            for rel in {r for r in (old_rel, new_rel) if r}:
                cand |= self.m.files.get(rel, set())
            if fp.binary:
                notes.append(f"binary {path}")
                ts = set(self.m.files.get(path, set()))
                if not ts:
                    ts = self.grep_tests(Path(path).name)
                must |= ts
                continue
            if path.startswith("sqlglot/") and path.endswith(".py"):
                nn = len(notes)
                c = self.analyse_py(fp, notes)
                if not any(x.startswith("no-op") for x in notes[nn:]):
                    touches_py = True
                cand |= c
            elif path.startswith("tests/") and path.endswith(".py"):
                cand |= self.analyse_test_py(fp, notes)
            elif path.startswith("tests/"):
                ts = set(self.m.files.get(path, set()))
                if not ts:
                    ts = self.grep_tests(Path(path).name)
                    notes.append(f"unmapped fixture {path} -> {len(ts)}")
                cand |= ts
            elif path.startswith("sqlglotc/") or path in ("setup.py", "pyproject.toml", "setup.cfg"):
                cand |= self.m.by_file.get("tests/test_sqlglotc.py", set())
                if path.endswith(".py"):
                    touches_py = True
                if path in ("setup.cfg", "pyproject.toml"):
                    notes.append(f"config {path}")
            elif path.startswith("sqlglot/"):
                # non-python file inside the package (e.g. data file)
                ts = set(self.m.files.get(path, set())) or self.grep_tests(Path(path).name)
                cand |= ts
        return cand, must, notes, touches_py

    def grep_tests(self, needle):
        out = set()
        stem = needle.split(".")[0]
        for f in self.m.by_file:
            t = src_text(f)
            if t and (needle in t or (len(stem) > 3 and stem in t)):
                out |= self.m.by_file[f]
        return out

    def analyse_py(self, fp, notes):
        rel_old = fp.old if fp.old != "/dev/null" else None
        rel_new = fp.new if fp.new != "/dev/null" else None
        if rel_old is None:
            # brand new module: whoever imports it (nobody yet, unless the patch also edits importers)
            notes.append(f"new module {rel_new}")
            return set()
        old = src_text(rel_old)
        if old is None:
            notes.append(f"missing {rel_old}")
            return self.module_users(rel_old)
        if rel_new is None or rel_new != rel_old:
            notes.append(f"deleted/renamed {rel_old}")
            return self.module_users(rel_old) | (self.m.all if self.is_core_module(rel_old) else set())
        new = apply_hunks(old, fp.hunks)
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            notes.append(f"syntax error {rel_old}")
            if self.is_core_module(rel_old):
                return set(self.m.all)
            return self.module_users(rel_old)
        old_tree = src_ast(rel_old)
        if old_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
            notes.append(f"no-op {rel_old}")
            return set()
        if old_tree is not None and ast.dump(strip_docstrings(old_tree)) == ast.dump(strip_docstrings(new_tree)):
            # only docstrings changed; nothing in the suite reads them
            notes.append(f"no-op docstring {rel_old}")
            return set()
        nc_old, nc_new = noncode_lines(old), noncode_lines(new)
        blocks, new_to_old = change_blocks(fp.hunks)
        out = set()
        equiv_blocks = 0
        code_blocks = 0
        for rem, add in blocks:
            rem = [ln for ln in rem if ln not in nc_old]
            add = [ln for ln in add if ln not in nc_new]
            if rem or add:
                code_blocks += 1
            if rem and len(rem) == len(add) == 1:
                _, so = locate(old_tree, rem[0])
                _, sn = locate(new_tree, add[0])
                if so is not None and sn is not None and is_identity_swap(so, sn, sys.modules.get(module_name(rel_old))):
                    equiv_blocks += 1
            if rem:
                lam = self.lambda_only(rel_old, old_tree, new_tree, rem, add)
                if lam is not None:
                    out |= lam
                    continue
            for ln in rem:
                out |= self.line_effect(rel_old, old_tree, ln)
            for ln in add:
                out |= self.added_effect(rel_old, old_tree, new_tree, ln, new_to_old, replaced=bool(rem))
        if code_blocks and equiv_blocks == code_blocks:
            notes.append(f"equiv {rel_old}")
        return out

    def lambda_only(self, rel, old_tree, new_tree, rem, add):
        """If a change to a class/module-level statement only touches lambda
        bodies (e.g. a `FUNCTIONS = {"X": lambda args: ...}` entry), those bodies
        run when called, not at import: return the tests that ran them."""
        stmts = []
        for ln in rem:
            chain, st = locate(old_tree, ln)
            if st is None or any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n, _ in chain):
                return None
            if not isinstance(st, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)):
                return None
            if st not in stmts:
                stmts.append((st, [n.name for n, _ in chain if isinstance(n, ast.ClassDef)]))
        out = set()
        for st, cls in {id(s): (s, c) for s, c in stmts}.values():
            names = defined_names(st)
            if not names:
                return None
            body = new_tree.body
            for c in cls:
                node = next((x for x in body if isinstance(x, ast.ClassDef) and x.name == c), None)
                if node is None:
                    return None
                body = node.body
            cands = [x for x in body if type(x) is type(st) and defined_names(x) == names]
            if len(cands) != 1:
                return None
            lams = []
            if not _diff_in_lambdas(st, cands[0], None, lams):
                return None
            for lam in lams:
                out |= self.m.cover(rel, lam.body.lineno, lam.body.end_lineno)
        return out

    def added_effect(self, rel, old_tree, new_tree, n, new_to_old, replaced):
        """Tests that could see a line added at new-file line `n`."""
        chain, stmt = locate(new_tree, n)
        if stmt is None:
            return set()
        # a statement that existed before and was only edited
        start = _node_start(stmt)
        if not (isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n > header_range(stmt)[1]):
            for ln in range(start, stmt.end_lineno + 1):
                if ln in new_to_old:
                    return self.line_effect(rel, old_tree, new_to_old[ln])
        in_func = [k for k, (node, b) in enumerate(chain) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and b]
        if in_func:
            if replaced:
                return set()  # the removed lines it replaces stand for it
            # a new statement inside a function: whoever runs its block
            parent = chain[-1][0] if chain[-1][0] is not stmt else (chain[-2][0] if len(chain) > 1 else None)
            out = set()
            for fld in ("body", "orelse", "finalbody", "handlers"):
                blk = getattr(parent, fld, None) or []
                if stmt in blk:
                    for sib in blk:
                        if sib is stmt:
                            continue
                        for ln in range(_node_start(sib), (header_range(sib)[1] if getattr(sib, "body", None) else sib.end_lineno) + 1):
                            if ln in new_to_old:
                                out |= self.m.cover(rel, new_to_old[ln], new_to_old[ln])
            if isinstance(parent, ast.stmt):
                lo, hi = header_range(parent)
                for ln in range(lo, hi + 1):
                    if ln in new_to_old:
                        out |= self.m.cover(rel, new_to_old[ln], new_to_old[ln])
            if not out:
                fn = chain[in_func[-1]][0]
                out |= self.m.cover(rel, *[new_to_old.get(fn.body[0].lineno, 0)] * 2)
                if not out:
                    out |= self.m.cover(rel, *self._old_range_of(old_tree, chain[: in_func[-1] + 1]))
            return out
        # a new definition at class or module level
        return self.def_effect(rel, old_tree, chain, stmt, 0, None, new_stmt=True)

    def _old_range_of(self, old_tree, chain):
        body = old_tree.body
        node = None
        for n, _ in chain:
            node = next((c for c in body if isinstance(c, type(n)) and getattr(c, "name", None) == getattr(n, "name", None)), None)
            if node is None:
                return (0, 0)
            body = node.body
        return (node.lineno, node.end_lineno) if node is not None else (0, 0)

    def analyse_helper(self, fp, rel, notes):
        """A shared test helper (tests/helpers.py): line coverage inside its
        functions, name references from test files for module-level edits."""
        old = src_text(rel)
        if old is None:
            return None
        new = apply_hunks(old, fp.hunks)
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            return None
        old_tree = src_ast(rel)
        if old_tree is None:
            return None
        if ast.dump(strip_docstrings(old_tree)) == ast.dump(strip_docstrings(new_tree)):
            notes.append(f"no-op {rel}")
            return set()
        nc_old, nc_new = noncode_lines(old), noncode_lines(new)
        blocks, new_to_old = change_blocks(fp.hunks)
        out = set()
        for rem, add in blocks:
            rem = [ln for ln in rem if ln not in nc_old]
            add = [ln for ln in add if ln not in nc_new]
            spots = [(old_tree, ln) for ln in rem] + [(new_tree, ln) for ln in add]
            for tree, ln in spots:
                chain, st = locate(tree, ln)
                if st is None:
                    continue
                fns = [n for n, b in chain if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and b]
                if fns and tree is old_tree:
                    out |= self.m.cover(rel, fns[-1].lineno, fns[-1].end_lineno)
                    continue
                if fns:
                    # new code inside a function: whoever calls that function
                    names = {fns[0].name}
                else:
                    names = defined_names(st)
                if not names:
                    return None
                rx = re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b")
                for f, ts in self.m.by_file.items():
                    t = src_text(f)
                    if t and rx.search(t):
                        out |= ts
                # helper functions using the name
                for node in old_tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and rx.search(ast.unparse(node)):
                        out |= self.m.cover(rel, node.lineno, node.end_lineno)
        notes.append(f"helper {rel} -> {len(out)}")
        return out

    def analyse_test_py(self, fp, notes):
        rel = fp.old if fp.old != "/dev/null" else fp.new
        tests_here = self.m.by_file.get(rel, set())
        if not tests_here and self.m.lines(rel) and fp.old != "/dev/null" and fp.new == fp.old:
            res = self.analyse_helper(fp, rel, notes)
            if res is not None:
                return res
        if not tests_here:
            # helper module: every test file importing it
            stem = Path(rel).stem
            out = set()
            for f, ts in self.m.by_file.items():
                t = src_text(f)
                if t and re.search(r"\b" + re.escape(stem) + r"\b", t):
                    out |= ts
            notes.append(f"test helper {rel} -> {len(out)}")
            return out
        old = src_text(rel) if fp.old != "/dev/null" else None
        if old is None:
            return set(tests_here)
        new = apply_hunks(old, fp.hunks)
        try:
            new_tree = ast.parse(new)
        except SyntaxError:
            return set(tests_here)
        old_tree = src_ast(rel)
        if old_tree is not None and ast.dump(old_tree) == ast.dump(new_tree):
            return set()
        removed, gaps = touched_old_lines(fp.hunks)
        lines = set(removed) | {g for g in gaps} | {g + 1 for g in gaps}
        out = set()
        name_to_num = {}
        for n in tests_here:
            name_to_num.setdefault(self.m.id_of[n].split("::")[-1], set()).add(n)
        for ln in lines:
            chain, stmt = locate(old_tree, ln) if old_tree else ([], None)
            fn = None
            for node, in_body in chain:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = node
                    break
            if fn is not None and fn.name.startswith("test") and len(chain) >= 1:
                out |= name_to_num.get(fn.name, set()) or tests_here
            else:
                out |= tests_here
        return out


# --------------------------------------------------------------------------- partition / cost


def partition(nums, secs):
    """Best split of `nums` over 1..8 workers; returns (cost_ms, workers)."""
    if not nums:
        return 0, []
    items = sorted(nums, key=lambda n: -secs[n])
    best = None
    for k in range(1, min(MAX_WORKERS, len(items)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for n in items:
            j = loads.index(min(loads))
            ws[j].append(n)
            loads[j] += secs[n]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return best


def plan(cand, must, secs, purge):
    """Choose which candidates to run here (R) and which to hand to CI (H)."""
    cand = set(cand) - set(must)
    items = sorted(cand, key=lambda n: -secs[n])
    best = None
    total = sum(secs[n] for n in items)
    run_sum = total
    for j in range(0, min(len(items), 60) + 1):
        if j > 0:
            run_sum -= secs[items[j - 1]]
        H = set(items[:j]) | set(must)
        R = items[j:]
        run_cost = (CPU_PER_MS * run_sum + CHILD_MS + (CHILD_PURGE_MS if purge else 0)) if R else 0
        c = run_cost + partition(H, secs)[0]
        if best is None or c < best[0]:
            best = (c, set(items[j:]), H)
    if best is None:
        return set(), set(must)
    return best[1], best[2]


# --------------------------------------------------------------------------- running tests


class Tree:
    """A writable mirror of SRC under TMP made of symlinks; patched files are real."""

    def __init__(self, root):
        self.root = Path(root)
        self.changed = []  # (rel, kind)
        if not self.root.exists():
            for d, dirs, files in os.walk(SRC):
                rd = os.path.relpath(d, SRC)
                if rd.startswith(".git"):
                    dirs[:] = []
                    continue
                dirs[:] = [x for x in dirs if x != ".git"]
                td = self.root / rd if rd != "." else self.root
                td.mkdir(parents=True, exist_ok=True)
                for f in files:
                    os.symlink(os.path.join(d, f), td / f)

    def apply(self, files):
        for fp in files:
            if fp.binary:
                continue
            old_rel = fp.old if fp.old != "/dev/null" else None
            new_rel = fp.new if fp.new != "/dev/null" else None
            old_text = ""
            if old_rel:
                try:
                    old_text = (SRC / old_rel).read_text(encoding="utf-8")
                except Exception:
                    old_text = ""
            new_text = apply_hunks(old_text, fp.hunks) if fp.hunks else old_text
            if old_rel and old_rel != new_rel:
                p = self.root / old_rel
                if p.is_symlink() or p.exists():
                    p.unlink()
                self.changed.append((old_rel, "removed"))
            if new_rel:
                p = self.root / new_rel
                p.parent.mkdir(parents=True, exist_ok=True)
                existed = p.is_symlink() or p.exists()
                if existed:
                    p.unlink()
                p.write_text(new_text, encoding="utf-8")
                self.changed.append((new_rel, "replaced" if (SRC / new_rel).exists() else "added"))

    def revert(self):
        for rel, kind in reversed(self.changed):
            p = self.root / rel
            if p.is_symlink() or p.exists():
                p.unlink()
            if (SRC / rel).exists():
                os.symlink(SRC / rel, p)
        self.changed = []


def _serial_pool_factory():
    import pickle
    from concurrent.futures import Future

    class SerialPool:
        """Stands in for ProcessPoolExecutor: same pickling boundary, no processes."""

        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **k):
            self._init = (initializer, initargs)
            self._ran_init = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def shutdown(self, *a, **k):
            pass

        def submit(self, fn, *args, **kwargs):
            f = Future()
            try:
                if not self._ran_init:
                    self._ran_init = True
                    ini, ia = self._init
                    if ini:
                        ini(*pickle.loads(pickle.dumps(ia)))
                fn = pickle.loads(pickle.dumps(fn))
                args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
                r = fn(*args, **kwargs)
                f.set_result(pickle.loads(pickle.dumps(r)))
            except BaseException as e:  # noqa
                try:
                    e = pickle.loads(pickle.dumps(e))
                except Exception:
                    pass
                f.set_exception(e)
            return f

        def map(self, fn, *its, **k):
            return [self.submit(fn, *a).result() for a in zip(*its)]

    return SerialPool


class _Timeout(BaseException):
    pass


def child_main(tree, nodeids, limits, purge, wfd, check_mods):
    import pytest

    out = os.fdopen(wfd, "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    sys.stdout = open(os.devnull, "w")
    os.chdir(tree)
    sys.dont_write_bytecode = True
    os.environ["PYTHONPATH"] = tree
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path[:] = [tree] + [p for p in sys.path if p and os.path.abspath(p) != str(SRC)]
    purge_sg, purge_tests = purge
    for m in list(sys.modules):
        if (purge_sg or purge_tests) and (m == "tests" or m.startswith("tests.")):
            del sys.modules[m]
        elif purge_sg and (m == "sqlglot" or m.startswith("sqlglot.")):
            del sys.modules[m]
    purge = purge_sg
    if purge:
        import importlib

        try:
            importlib.import_module("sqlglot")
        except BaseException as e:  # noqa
            out.write("I sqlglot\n")
            out.flush()
            os._exit(0)
        mods = [x for x in check_mods if x != "*"]
        if "*" in check_mods:
            import pkgutil

            import sqlglot as _sg

            mods += [mi.name for mi in pkgutil.walk_packages(_sg.__path__, "sqlglot.") if not mi.name.endswith("__main__")]
        for mname in mods:
            try:
                importlib.import_module(mname)
            except BaseException:  # noqa
                out.write("I " + mname + "\n")
                out.flush()
    if not nodeids:
        out.flush()
        os._exit(0)
    SerialPool = _serial_pool_factory()
    try:
        import duckdb

        _connect = duckdb.connect

        def connect(database=":memory:", read_only=False, config=None, **kw):
            config = dict(config or {})
            config.setdefault("threads", 1)
            return _connect(database, read_only, config=config, **kw)

        duckdb.connect = connect
    except Exception:
        pass

    state = {"node": None, "phase": False, "timed_out": set()}

    def on_alarm(signum, frame):
        node = state["node"]
        if node is None or not state["phase"]:
            return  # between phases pytest is reporting; try again on the next tick
        if node not in state["timed_out"]:
            state["timed_out"].add(node)
            out.write("F " + node + "\n")
            out.flush()
        raise _Timeout("timeout")

    signal.signal(signal.SIGALRM, on_alarm)

    class P:
        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, session, config, items):
            for mod in list(sys.modules.values()):
                name = getattr(mod, "__name__", "") or ""
                if name.startswith("tests") and hasattr(mod, "ProcessPoolExecutor"):
                    mod.ProcessPoolExecutor = SerialPool

        def pytest_collectreport(self, report):
            if report.failed:
                out.write("C " + report.nodeid + "\n")

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            lim = limits.get(item.nodeid, 5.0)
            state["node"] = item.nodeid
            # keeps firing until the test gives up, in case the code swallows it
            signal.setitimer(signal.ITIMER_REAL, lim, 0.25)
            outcome = yield
            signal.setitimer(signal.ITIMER_REAL, 0)
            state["node"] = None
            if outcome.excinfo is None:
                out.write("D " + item.nodeid + "\n")
            if item.nodeid in state["timed_out"]:
                P.timeouts += 1
                if P.timeouts >= 2 and P.session is not None:
                    # code that hangs once hangs everywhere: stop, the rest goes to CI
                    P.session.shouldstop = "repeated timeouts"

        def _phase(self):
            state["phase"] = True
            try:
                yield
            finally:
                state["phase"] = False

        pytest_runtest_setup = pytest.hookimpl(hookwrapper=True)(_phase)
        pytest_runtest_call = pytest.hookimpl(hookwrapper=True)(_phase)
        pytest_runtest_teardown = pytest.hookimpl(hookwrapper=True)(_phase)

        timeouts = 0

        def pytest_runtest_logreport(self, report):
            if report.failed:
                out.write("F " + report.nodeid + "\n")

        session = None

        def pytest_sessionstart(self, session):
            P.session = session

    args = [
        "-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:timeout",
        "-p", "no:randomly", "-o", "addopts=", "--rootdir", tree,
        "-o", "console_output_style=classic", "--no-header", "-W", "ignore", "--assert=plain",
    ] + list(nodeids)
    try:
        if os.environ.get("BR_PROFILE"):
            before = set(sys.modules)
            import cProfile
            import pstats
            pr = cProfile.Profile()
            pr.enable()
            pytest.main(args, plugins=[P()])
            pr.disable()
            st = pstats.Stats(pr, stream=sys.stderr); st.sort_stats("tottime").print_stats(18); st.print_callers("builtins.compile")
            new = sorted({m.split(".")[0] for m in set(sys.modules) - before})
            print("NEWMODS", new, file=sys.stderr)
        else:
            pytest.main(args, plugins=[P()])
    except BaseException as e:  # noqa
        out.write("E " + repr(e)[:200].replace("\n", " ") + "\n")
    out.flush()
    os._exit(0)


PRELOAD = (
    "pytest", "_pytest.unittest", "_pytest.python", "_pytest.subtests", "_pytest.terminal",
    "_pytest.runner", "_pytest.main", "_pytest.fixtures", "_pytest.capture", "_pytest.logging",
    "_pytest.junitxml", "_pytest.doctest", "_pytest.skipping", "_pytest.warnings", "_pytest.pastebin",
    "_pytest.recwarn", "_pytest.tmpdir", "_pytest.monkeypatch", "_pytest.faulthandler", "_pytest.debugging",
    "_pytest.legacypath", "_pytest.stepwise", "_pytest.nose", "_pytest.setuponly", "_pytest.setupplan",
    "_pytest.helpconfig", "_pytest.mark", "_pytest.assertion", "_pytest.threadexception",
    "_pytest.unraisableexception", "_pytest.reports", "_pytest.python_path", "_pytest.raises",
    "unittest", "unittest.mock", "duckdb", "numpy", "pandas", "pickle", "concurrent.futures",
    "multiprocessing", "multiprocessing.context", "gzip", "csv", "sqlite3", "zoneinfo", "dateutil",
    "pytz", "tomllib", "doctest", "tracemalloc", "pdb", "decimal", "datetime", "json", "tempfile",
    "subprocess", "runpy", "setuptools", "setuptools.command.build_ext",
)


def preload_tests(jobs, sel):
    """Import the test modules that fixture/test-data jobs need, once, in the parent."""
    import importlib

    files = set()
    for j in jobs:
        if j.run and not j.purge and not any(f.path.startswith("tests/") and f.path.endswith(".py") for f in j.files):
            files |= {sel.m.file_of[n] for n in j.run}
    for f in sorted(files):
        try:
            importlib.import_module(f[:-3].replace("/", "."))
        except Exception as e:
            log("preload failed", f, repr(e)[:100])
    log("preloaded", len(files), "test modules")


class Job:
    def __init__(self, cid, files, run, hand, purge, check_mods):
        self.cid = cid
        self.files = files
        self.run = run
        self.hand = hand
        self.purge = purge
        self.check_mods = check_mods
        self.failed = set()
        self.done = set()
        self.import_broken = []
        self.collect_failed = []
        self.error = None
        self.killed = False
        self.equiv = False


def _touches_test_py(job):
    return any(f.path.startswith("tests/") and f.path.endswith(".py") for f in job.files)


def make_batches(jobs, sel, max_jobs=12, max_ms=15000):
    """Group jobs that cannot see each other's change into one test run: their
    edited files differ, their candidate tests differ, and none of their
    candidates opens a file another member edits. One child then pays pytest
    start-up, sqlglot import and test-module import once for the group."""
    m = sel.m
    singles, batches = [], []
    for purge in (False, True):
        cur = None
        for j in [j for j in jobs if j.run and j.purge == purge]:
            if _touches_test_py(j) or any(f.binary for f in j.files):
                singles.append(j)
                continue
            paths = {f.path for f in j.files} | {f.old for f in j.files if f.old}
            readers = set()
            for pth in paths:
                readers |= m.files.get(pth, set())
            est = sum(m.secs[n] for n in j.run)
            ok = (
                cur is not None
                and len(cur["jobs"]) < max_jobs
                and cur["ms"] + est <= max_ms
                and not (paths & cur["paths"])
                and not (j.run & cur["run"])
                and not (readers & cur["run"])
                and not (j.run & cur["readers"])
            )
            if not ok:
                cur = {"jobs": [], "paths": set(), "run": set(), "readers": set(), "ms": 0}
                batches.append(cur)
            cur["jobs"].append(j)
            cur["paths"] |= paths
            cur["run"] |= j.run
            cur["readers"] |= readers
            cur["ms"] += est
    out = []
    for b in batches:
        if len(b["jobs"]) == 1:
            singles.append(b["jobs"][0])
            continue
        files = [f for j in b["jobs"] for f in j.files]
        mods = []
        for j in b["jobs"]:
            mods += [x for x in j.check_mods if x not in mods]
        bj = Job("+".join(j.cid for j in b["jobs"]), files, set(b["run"]), set(), any(j.purge for j in b["jobs"]), mods)
        bj.members = b["jobs"]
        out.append(bj)
    return out, singles


def run_all(jobs, sel, tree):
    batches, singles = make_batches(jobs, sel)
    others = [j for j in jobs if not j.run and j.purge and j.check_mods]
    if len(others) > 1:
        # import-only checks: one child imports every edited module (patches are file-disjoint)
        paths = [f.path for j in others for f in j.files]
        if len(paths) == len(set(paths)):
            mods = []
            for j in others:
                mods += [x for x in j.check_mods if x not in mods]
            bj = Job("+".join(j.cid for j in others), [f for j in others for f in j.files], set(), set(), True, mods)
            bj.members = others
            batches.append(bj)
            others = []
    log(f"runs: {len(batches)} batches ({sum(len(b.members) for b in batches)} commits), {len(singles) + len(others)} single")
    run_jobs(batches + singles + others, sel, tree)
    for b in batches + singles + others:
        log(f"run {b.cid}: tests={len(b.run)} est={sum(sel.m.secs[n] for n in b.run)/1000:.2f}s cpu={getattr(b, 'cpu', 0):.2f}s purge={b.purge}")
    retry = []
    for b in batches:
        trouble = b.killed or b.error or b.import_broken or b.collect_failed or (b.run - b.done)
        if trouble:
            log("batch had trouble, rerunning alone:", b.cid)
            retry += b.members
            continue
        for j in b.members:
            j.failed = b.failed & j.run
            j.done = b.done & j.run
            j.cpu = getattr(b, "cpu", 0) / len(b.members)
    if retry:
        run_jobs(retry, sel, tree)


def run_jobs(jobs, sel, tree):
    m = sel.m
    nodeid_to_num = {v: k for k, v in m.id_of.items()}
    trees = [tree]
    free = [0]
    queue = list(jobs)
    # longest first for better packing on 2 cores
    # fixture/test-only jobs first: they reuse the parent's imports
    queue.sort(key=lambda j: j.purge)
    active = {}  # fd -> (job, pid, slot, buf, deadline)
    while queue or active:
        while queue and free:
            job = queue.pop(0)
            if time.time() - T0 > DEADLINE:
                log("deadline: not running", job.cid)
                job.killed = True
                continue
            slot = free.pop()
            tree = trees[slot]
            try:
                tree.apply(job.files)
            except Exception as e:
                log("apply failed", job.cid, e)
                job.error = "apply"
                tree.revert()
                free.append(slot)
                continue
            nodeids = [m.id_of[n] for n in sorted(job.run)]
            limits = {m.id_of[n]: 3.0 * m.secs[n] / 1000 + 2.0 for n in job.run}
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(r)
                try:
                    purge_tests = any(f.path.startswith("tests/") and f.path.endswith(".py") for f in job.files)
                    child_main(str(tree.root), nodeids, limits, (job.purge, purge_tests), w, job.check_mods)
                finally:
                    os._exit(1)
            os.close(w)
            budget = min(sum(limits.values()) + 10 + (5 if job.purge else 0), 45)
            active[r] = [job, pid, slot, b"", time.time() + budget]
        if not active:
            continue
        timeout = max(0.05, min(a[4] for a in active.values()) - time.time())
        ready, _, _ = select.select(list(active), [], [], timeout)
        for fd in ready:
            a = active[fd]
            chunk = os.read(fd, 65536)
            if chunk:
                a[3] += chunk
                continue
            _finish(fd, a, active, trees, free, nodeid_to_num)
        now = time.time()
        for fd, a in list(active.items()):
            if now > a[4]:
                log("killing", a[0].cid)
                try:
                    os.kill(a[1], signal.SIGKILL)
                except Exception:
                    pass
                a[0].killed = True
                _finish(fd, a, active, trees, free, nodeid_to_num)


def _finish(fd, a, active, trees, free, nodeid_to_num):
    job, pid, slot, buf = a[0], a[1], a[2], a[3]
    try:
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
    except Exception:
        pass
    os.close(fd)
    try:
        _, _, ru = os.wait4(pid, 0)
        job.cpu = ru.ru_utime + ru.ru_stime
    except Exception:
        job.cpu = -1
    if os.environ.get("BR_DEBUG"):
        log("child said:", buf.decode("utf-8", "replace")[:3000])
    for line in buf.decode("utf-8", "replace").split("\n"):
        if not line:
            continue
        tag, rest = line[0], line[2:]
        if tag == "F":
            base = rest.split(" ")[0]
            n = nodeid_to_num.get(base)
            if n is None:
                # subtest report ids may carry a suffix
                for k in (rest, rest.split("[")[0]):
                    if k in nodeid_to_num:
                        n = nodeid_to_num[k]
                        break
            if n is not None:
                job.failed.add(n)
            else:
                job.collect_failed.append(rest)
        elif tag == "D":
            n = nodeid_to_num.get(rest)
            if n is not None:
                job.done.add(n)
        elif tag == "C":
            job.collect_failed.append(rest)
        elif tag == "I":
            job.import_broken.append(rest)
        elif tag == "E":
            job.error = rest
    trees[slot].revert()
    free.append(slot)
    del active[fd]


# --------------------------------------------------------------------------- main


def main(emit):
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    sel = Selector(tests)
    secs = sel.m.secs
    patches = sorted((DATA / "commits").glob("*.patch"))
    log(f"{len(patches)} commits, {len(tests)} tests")
    # one writable mirror of the source; the parent imports sqlglot from it so
    # children that do not touch sqlglot's code can reuse those imports
    tree = Tree(TMP / "w")
    sys.path.insert(0, str(tree.root))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONPATH"] = str(tree.root)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.chdir(tree.root)
    import importlib

    for mod in PRELOAD:
        try:
            importlib.import_module(mod)
        except Exception:
            pass
    try:
        sel.sg()
    except Exception as e:
        log("sqlglot import failed in parent", e)
    log("warm")

    jobs = []
    answers = {}
    for p in patches:
        cid = p.stem
        try:
            files = parse_patch(p.read_text(encoding="utf-8", errors="replace"))
            cand, must, notes, touches_py = sel.analyse(files)
        except Exception as e:
            log("analysis failed", cid, repr(e))
            files, cand, must, notes, touches_py = [], set(sel.m.all), set(), ["fallback"], True
        cand = {n for n in cand if n in secs}
        must = {n for n in must if n in secs}
        check_mods = [module_name(f.path) for f in files if f.path.startswith("sqlglot/") and f.path.endswith(".py") and f.new != "/dev/null" and not f.binary]
        run, hand = plan(cand, must, secs, touches_py)
        log(f"{cid}: cand={len(cand)} ({sum(secs[n] for n in cand)/1000:.1f}s) run={len(run)} hand={len(hand)} {notes}")
        if touches_py and getattr(sel, "deflevel", False):
            check_mods = check_mods + ["*"]
        job = Job(cid, files, run, hand, touches_py, check_mods)
        n_eq = sum(1 for x in notes if x.startswith("equiv "))
        n_noop = sum(1 for x in notes if x.startswith("no-op "))
        job.equiv = bool(files) and n_eq > 0 and n_eq + n_noop == len(files) and all(
            f.path.startswith("sqlglot/") and f.path.endswith(".py") for f in files
        )
        if job.equiv:
            # run the quick candidates as a check; the slow ones are dropped if they pass
            job.run = {n for n in cand if secs[n] <= 300}
            job.hand = (set(cand) - job.run) | set(must)
            log(f"{cid}: equivalent swap, run={len(job.run)} hand={len(job.hand)}")
        jobs.append(job)
    try:
        preload_tests(jobs, sel)
        run_all(jobs, sel, tree)
    except Exception as e:
        log("runner crashed", repr(e))
        for j in jobs:
            if j.run and not j.done:
                j.killed = True
    for j in jobs:
        out = set(j.hand) | set(j.failed)
        if j.equiv and j.run and not j.failed and not (j.killed or j.error or j.import_broken or j.collect_failed):
            # an is/== swap that every test we ran agrees with: the slow tests we
            # handed over would see the same values
            log(j.cid, "equivalent swap: dropping", len(j.hand), "handed tests")
            out = set()
        if j.run or j.import_broken:
            # anything we could not prove passing goes to CI
            undone = set(j.run) - j.done
            if undone:
                log(j.cid, "not finished:", len(undone))
            out |= undone
            if j.killed or j.error or j.import_broken or j.collect_failed:
                if j.import_broken or j.collect_failed:
                    log(j.cid, "import/collect problems", j.import_broken[:3], j.collect_failed[:3])
                    if j.import_broken and any(x == "sqlglot" for x in j.import_broken):
                        out |= sel.m.all
                    elif j.import_broken:
                        for mname in j.import_broken:
                            rel = mname.replace(".", "/") + ".py"
                            if not (SRC / rel).exists():
                                rel = mname.replace(".", "/") + "/__init__.py"
                            out |= sel.module_users(rel)
                    for cf in j.collect_failed:
                        f = cf.split("::")[0]
                        out |= sel.m.by_file.get(f, set())
        cost, workers = partition(out, secs)
        log(f"{j.cid}: failed={len(j.failed)} out={len(out)} cost={cost/1000:.1f}s cpu={getattr(j, 'cpu', 0):.2f} est={sum(secs[n] for n in j.run)/1000:.2f} purge={j.purge}")
        answers[j.cid] = workers
    emit([json.dumps({"commit": p.stem, "workers": answers.get(p.stem, [])}) for p in patches])
    t = os.times()
    log(f"done: cpu self={t.user + t.system:.2f}s children={t.children_user + t.children_system:.2f}s")


def _emit(fd, lines):
    data = "".join(line + "\n" for line in lines).encode("utf-8")
    while data:
        n = os.write(fd, data)
        data = data[n:]


if __name__ == "__main__":
    # answers go to a private copy of stdout; everything else (stray prints from
    # libraries, children) lands on stderr
    _OUT = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        main(lambda lines: _emit(_OUT, lines))
    except BaseException as e:  # noqa
        log("fatal", repr(e))
        try:
            tests = json.loads((DATA / "tests.json").read_text())["tests"]
            secs = {t["number"]: int(round(t["seconds"] * 1000)) for t in tests}
            _, ws = partition(set(secs), secs)
            _emit(_OUT, [json.dumps({"commit": p.stem, "workers": ws}) for p in sorted((DATA / "commits").glob("*.patch"))])
        except BaseException as e2:  # noqa
            log("fatal fallback failed", repr(e2))
