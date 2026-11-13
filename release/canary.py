"""Canary, promote and rollback for a hosted agent.

Foundry routes an endpoint to ONE version (no weighted split), so canary is done
by sending a share of sessions to the candidate with version_indicator, while
the endpoint keeps serving prod. Promotion = re-pin the endpoint.

  python release/canary.py canary <prod> <candidate> [--stages 5,25,50] [--requests 20]
  python release/canary.py promote <candidate>
  python release/canary.py rollback <prod>
"""

import json
import os
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import routing  # noqa: E402
import suite  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def canary(prod: str, cand: str, stages: list[int], n: int):
    rows = [json.loads(x) for x in (ROOT / "data" / "validation.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    rng = random.Random(7)
    for pct in stages:
        batch = [rng.choice(rows) for _ in range(n)]
        to_cand = [r for r in batch if rng.random() * 100 < pct] or batch[:1]
        to_prod = [r for r in batch if r not in to_cand]
        c = suite.run(cand, to_cand)
        p = suite.run("endpoint", to_prod) if to_prod else {"pass_rate": None, "n": 0}
        ok = c["pass_rate"] >= (p["pass_rate"] or 0) and c["errors"] == 0
        print(f"stage {pct:>3}%: candidate v{cand} {c['n']} req pass={c['pass_rate']} | "
              f"prod(endpoint) {p['n']} req pass={p['pass_rate']} -> {'HOLD' if ok else 'ABORT'}")
        if not ok:
            print("canary aborted; endpoint still pinned to", routing.routed_version())
            sys.exit(1)
    print("canary healthy at all stages; ready to promote")


if __name__ == "__main__":
    cmd, *a = sys.argv[1:]
    if cmd == "canary":
        stages = [int(x) for x in (a[a.index("--stages") + 1] if "--stages" in a else "5,25,50").split(",")]
        n = int(a[a.index("--requests") + 1]) if "--requests" in a else int(os.environ.get("CANARY_REQUESTS", "12"))
        canary(a[0], a[1], stages, n)
    elif cmd in ("promote", "rollback"):
        routing.wait_active(a[0])
        routing.pin(a[0])
        print(f"{cmd}: endpoint now routes 100% to v{routing.routed_version()}")
