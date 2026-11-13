"""Run a dataset split against one agent version and score it with refund_checks.

  python release/suite.py <version|endpoint> data/release.jsonl [--repeat N] [--out result.json]

`<version>` creates a version-pinned session (version_indicator), so traffic hits
exactly that version regardless of the endpoint's routing; `endpoint` uses the
production route. Used for traffic generation, the CI release gate and canary.
"""

import argparse
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import routing  # noqa: E402
from refund_checks import check  # noqa: E402


def run(target: str, rows: list[dict], repeat: int = 1) -> dict:
    results = []
    sid = None if target == "endpoint" else routing.create_session(target)
    try:
        for _ in range(repeat):
            for row in rows:
                try:
                    r = routing.invoke(row["query"], sid)
                    c = check(row["order_id"], r.get("output") or [])
                    u = r.get("usage") or {}
                    ver = next((i.get("agent_reference", {}).get("version") for i in r.get("output") or []
                                if i.get("agent_reference")), None)
                    results.append({"name": row["name"], **c, "latency_s": r["_latency_s"],
                                    "tokens": (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0),
                                    "served_by": ver})
                except Exception as e:  # count infra failures as failures
                    results.append({"name": row["name"], "passed": False, "error": str(e)[:300]})
    finally:
        if sid:
            routing.stop_session(sid)
    ok = [r for r in results if "error" not in r]
    return {
        "target": target,
        "n": len(results),
        "pass_rate": round(sum(r["passed"] for r in results) / max(len(results), 1), 3),
        "errors": len(results) - len(ok),
        "p50_latency_s": round(statistics.median([r["latency_s"] for r in ok]), 2) if ok else None,
        "avg_tokens": round(statistics.mean([r["tokens"] for r in ok]), 1) if ok else None,
        "served_by": sorted({str(r["served_by"]) for r in ok}),
        "results": results,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("dataset")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.dataset, encoding="utf-8") if line.strip()]
    s = run(a.target, rows, a.repeat)
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(s, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in s.items() if k != "results"}, indent=1))
    for r in s["results"]:
        flags = [k for k in ("repeated_submission", "unverified_claim", "claim_state_mismatch") if r.get(k)]
        print(f"  {'PASS' if r['passed'] else 'FAIL'} {r['name']:<28} {r.get('latency_s', '-')}s {flags or r.get('error', '')}")
