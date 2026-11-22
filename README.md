# Blast Radius selector

Entry for the RWX *Blast Radius* track: for each patch to sqlglot (pinned at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`), print the tests CI should run.

## How it decides

1. **Map, built once from the pinned commit** (`map.json`, made by
   `tools/covplugin.py` + `tools/mkmap.py`). The full suite was run under
   coverage with one context per test and records, for every test:
   - which sqlglot source lines it executes (setup lines count for the whole class),
   - which files it opens (fixtures),
   - which dialect classes it instantiates and which expression classes it builds.
   Process pools in the tests are swapped for an in-process pool with the same
   pickling boundary so their work is attributed to the test.

2. **Candidates for a patch** come from what the patch touches:
   - code inside a function or lambda body: tests that executed those lines;
   - class-level attributes: tests that use the class (dialect / expression
     users), narrowed to those executing a line that reads the attribute when
     every read happens in plain runtime code;
   - module-level names: follow references to the name across sqlglot;
   - fixtures and other data files: tests that opened them;
   - test files: the edited tests (or the whole file for module-level edits);
   - comment-only or whitespace-only edits (identical AST): nothing;
   - unparsable or unimportable modules: every test that imports them.

3. **Run the candidates for real** inside the container: the patch is applied
   to a symlinked mirror of sqlglot in `/tmp`, and a forked child (pytest and,
   when possible, sqlglot already imported in the parent) runs the selected
   tests with per-test time limits. Only tests that fail, error, time out or
   cannot be collected are reported.

4. **Cost model**: very slow candidates (for example the TPC-DS executor
   test) are cheaper to hand to CI than to run here, so the plan picks which
   candidates to run locally and which to pass through, then packs the output
   onto 1–8 workers to minimise `max worker load + 2 s × workers`.

An `x is C` ↔ `x == C` swap with `C` a constant or plain enum member is treated
as behaviour-preserving once the quick candidate tests pass.

## Layout

| Path | What |
| --- | --- |
| `Dockerfile` | Installs `requirements.lock`, fetches sqlglot at the pinned commit, precompiles bytecode |
| `selector.py` | The program the scorer runs |
| `map.json` | Coverage / fixture / dialect map of the pinned commit |
| `tools/` | Offline scripts that build the map and check recall on random mutants |

## Rebuilding the map

```sh
COVERAGE_CORE=ctrace COVMAP_OUT=/tmp/cov SKIP_INTEGRATION=0 PYTHONPATH=.:../tools \
  python -m pytest -n 8 -p covplugin -p no:cacheprovider tests      # inside a sqlglot checkout
python tools/mkmap.py /tmp/cov path/to/sqlglot sample/tests.json map.json
```

`COVERAGE_CORE=ctrace` matters: the `sysmon` core records each line only once,
not once per test.

## Local check

```sh
make sample   # needs Docker and the starter kit's sample/ folder next to the Makefile
```
