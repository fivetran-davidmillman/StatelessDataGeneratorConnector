#!/usr/bin/env python3
"""
Self-test for this connector. It needs nothing but this folder, the Fivetran Connector SDK
and Python: no dbt, no warehouse, no Fivetran account.

    pip install fivetran-connector-sdk
    python tests/test_connector.py            # from the connector/ folder (any folder works)

It copies connector.py and configuration.json into a temporary folder, runs real syncs with
`fivetran debug` (the SDK's local DuckDB runner), and checks the data against your
configuration. Your project folder is never modified. Exit code 0 = all checks passed.

Checks
  history load   reference row count, PK uniqueness, per-row min/max for every behaviour table,
                 no orphaned foreign keys
  later syncs    each appends to ONE or TWO behaviour tables only, never to the reference table,
                 within incremental_min..incremental_max rows per table; the reference table's
                 _fivetran_synced never moves
  determinism    a second run with the same seed makes the same table choices
"""
import sys
sys.dont_write_bytecode = True  # never leave __pycache__ in the project folder
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent if HERE.name == "tests" else HERE
LATER_SYNCS = 3
SCHEMA = "tester"  # schema `fivetran debug` writes to

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


def load_connector():
    spec = importlib.util.spec_from_file_location("connector_under_test", PROJECT / "connector.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_sync(workdir):
    exe = shutil.which("fivetran")
    if not exe:
        sys.exit("`fivetran` command not found. Install it with: pip install fivetran-connector-sdk")
    env = dict(os.environ, DO_NOT_TRACK="1")
    p = subprocess.run([exe, "debug", "--configuration", "configuration.json"],
                       cwd=workdir, env=env, capture_output=True, text=True)
    return p.returncode == 0, p.stdout + p.stderr


def make_workdir():
    d = Path(tempfile.mkdtemp(prefix="connector_test_"))
    shutil.copy(PROJECT / "connector.py", d)
    shutil.copy(PROJECT / "configuration.json", d)
    return d


def run_scenario(syncs):
    """Run `syncs` consecutive syncs; return (workdir, [state after each sync], ok_flags)."""
    d = make_workdir()
    states, oks = [], []
    for _ in range(syncs):
        ok, _out = run_sync(d)
        oks.append(ok)
        states.append(json.loads((d / "files" / "state.json").read_text()) if ok else {})
    return d, states, oks


def counts(con, tables):
    return {t: con.execute(f"select count(*) from {SCHEMA}.{t}").fetchone()[0] for t in tables}


def main():
    mod = load_connector()
    cfg = json.loads((PROJECT / "configuration.json").read_text())
    ref, kids = mod.REF, mod.CHILDREN
    ref_t, ref_pk = ref["table"], ref["pk"]
    kid_ts = [k["table"] for k in kids]
    parsed = mod.parse_config(cfg)

    print(f"Testing {PROJECT} ({1 + LATER_SYNCS} syncs in a temporary copy)...")
    work, states, oks = run_scenario(1 + LATER_SYNCS)
    try:
        check("all syncs completed", all(oks), f"results: {oks}")
        if not all(oks):
            return
        db = work / "files" / "warehouse.db"

        # snapshot after sync 1 needs its own run: re-run only the first sync in a second copy
        w1, s1, ok1 = run_scenario(1)
        con1 = duckdb.connect(str(w1 / "files" / "warehouse.db"), read_only=True)

        print("History load")
        n_ref = con1.execute(f"select count(*) from {SCHEMA}.{ref_t}").fetchone()[0]
        check(f"{ref_t}: {parsed['reference_count']} rows created", n_ref == parsed["reference_count"], f"got {n_ref}")
        distinct, lo, hi = con1.execute(f"select count(distinct {ref_pk}), min({ref_pk}), max({ref_pk}) from {SCHEMA}.{ref_t}").fetchone()
        check(f"{ref_t}.{ref_pk} unique and numbered 1..N", distinct == n_ref and lo == 1 and hi == n_ref)
        for k in kids:
            t, (lo_cfg, hi_cfg) = k["table"], parsed["history"][k["table"]]
            mn, mx = con1.execute(f"""select min(n), max(n) from (
                select r.{ref_pk}, count(c.{ref_pk}) n from {SCHEMA}.{ref_t} r
                left join {SCHEMA}.{t} c using ({ref_pk}) group by 1)""").fetchone()
            check(f"{t}: rows per {ref_t} within {lo_cfg}..{hi_cfg}", lo_cfg <= mn and mx <= hi_cfg, f"observed {mn}..{mx}")
            dup = con1.execute(f"select count(*) - count(distinct {k['pk']}) from {SCHEMA}.{t}").fetchone()[0]
            check(f"{t}.{k['pk']} unique", dup == 0)
        con1.close()

        print("Later syncs")
        con = duckdb.connect(str(db), read_only=True)
        for t in kid_ts:
            orphans = con.execute(f"select count(*) from {SCHEMA}.{t} c left join {SCHEMA}.{ref_t} r using ({ref_pk}) where r.{ref_pk} is null").fetchone()[0]
            check(f"{t}: no orphaned {ref_pk}", orphans == 0, f"{orphans} orphans")
        check(f"{ref_t}: still {parsed['reference_count']} rows after {LATER_SYNCS} more syncs", con.execute(f"select count(*) from {SCHEMA}.{ref_t}").fetchone()[0] == parsed["reference_count"])
        ref_last = con.execute(f"select max(_fivetran_synced) from {SCHEMA}.{ref_t}").fetchone()[0]
        later_firsts = []
        for t, k in zip(kid_ts, kids):
            first_later_id = states[0]["next_ids"][t]  # first row id created after the history load
            m = con.execute(f"select min(_fivetran_synced) from {SCHEMA}.{t} where {k['pk']} >= {first_later_id}").fetchone()[0]
            if m is not None:
                later_firsts.append(m)
        con.close()
        check(f"{ref_t}: never rewritten (newest sync stamp is older than every later-sync row)",
              bool(later_firsts) and ref_last < min(later_firsts), f"ref newest={ref_last}, earliest later row={min(later_firsts) if later_firsts else None}")

        # per-sync analysis from saved state (next_ids = rows created so far per child table)
        prev = states[0]["next_ids"]
        for i, st in enumerate(states[1:], start=2):
            added = {t: st["next_ids"][t] - prev[t] for t in kid_ts}
            touched = [t for t, n in added.items() if n > 0]
            check(f"sync {i}: touched 1 or 2 behaviour tables", len(touched) in (1, 2), f"touched {touched}")
            check(f"sync {i}: state agrees on which tables changed", sorted(st["last_tables_updated"]) == sorted(touched), f"state={st['last_tables_updated']} data={touched}")
            ok_rng = all(parsed["incremental_min"] <= added[t] <= parsed["incremental_max"] for t in touched)
            check(f"sync {i}: {parsed['incremental_min']}..{parsed['incremental_max']} rows per touched table", ok_rng, f"added {added}")
            prev = st["next_ids"]

        print("Determinism")
        w2, s2, ok2 = run_scenario(1 + LATER_SYNCS)
        seq1 = [sorted(s["last_tables_updated"]) for s in states[1:]]
        seq2 = [sorted(s["last_tables_updated"]) for s in s2[1:]]
        check("same seed -> same table choices on a second run", ok2 and seq1 == seq2, f"{seq1} vs {seq2}")
        shutil.rmtree(w1, ignore_errors=True)
        shutil.rmtree(w2, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
