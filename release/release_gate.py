"""CI release gate: production version vs candidate version on the held-out release set.

  python release/release_gate.py prepare            # pin current prod, print PROD_VERSION
  (azd deploy refund-release-loop)                   # creates candidate version N (unrouted)
  python release/release_gate.py compare <prod>     # wait-active N, run suites, exit 1 on regression

Both versions are hit through version-pinned sessions, so the comparison is
independent of endpoint routing and production traffic stays on <prod>.
"""

import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import routing  # noqa: E402
import suite  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIN_PASS = float(os.environ.get("GATE_MIN_PASS_RATE", "0.9"))
MAX_LATENCY_RATIO = float(os.environ.get("GATE_MAX_LATENCY_RATIO", "1.5"))
REPEAT = int(os.environ.get("GATE_REPEAT", "2"))


def _out(key, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


def prepare():
    prod = routing.routed_version()
    if prod == "latest":
        prod = routing.get_agent()["versions"]["latest"]["version"]
    routing.pin(prod)  # a new deploy must never auto-route to @latest
    print("PROD_VERSION", prod)
    _out("prod_version", prod)


def compare(prod: str):
    cand = str(routing.get_agent()["versions"]["latest"]["version"])
    if cand == str(prod):
        sys.exit("candidate version == prod version; nothing was deployed")
    routing.wait_active(cand)
    rows = [json.loads(x) for x in (ROOT / "data" / "release.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    p, c = suite.run(prod, rows, REPEAT), suite.run(cand, rows, REPEAT)

    checks = {
        f"candidate pass rate >= {MIN_PASS}": c["pass_rate"] >= MIN_PASS,
        "candidate pass rate >= prod": c["pass_rate"] >= p["pass_rate"],
        "no infra errors": c["errors"] == 0,
        f"p50 latency <= {MAX_LATENCY_RATIO}x prod": (c["p50_latency_s"] or 1e9) <= MAX_LATENCY_RATIO * (p["p50_latency_s"] or 0),
    }
    passed = all(checks.values())
    md = [f"## Release gate: v{prod} (prod) vs v{cand} (candidate) -> {'PASS' if passed else 'FAIL'}", "",
          "| metric | prod v%s | candidate v%s |" % (prod, cand), "|---|---|---|",
          f"| pass rate | {p['pass_rate']} | {c['pass_rate']} |",
          f"| p50 latency (s) | {p['p50_latency_s']} | {c['p50_latency_s']} |",
          f"| avg tokens | {p['avg_tokens']} | {c['avg_tokens']} |",
          f"| served by | {p['served_by']} | {c['served_by']} |", "", "| check | result |", "|---|---|"]
    md += [f"| {k} | {'✅' if v else '❌'} |" for k, v in checks.items()]
    md += ["", "| case | prod | candidate |", "|---|---|---|"]
    md += [f"| {a['name']} | {'✅' if a['passed'] else '❌'} | {'✅' if b['passed'] else '❌'} |"
           for a, b in zip(p["results"], c["results"])]
    report = "\n".join(md)
    print(report)
    (ROOT / "release" / "gate-report.json").write_text(json.dumps({"prod": p, "candidate": c, "checks": checks}, indent=1))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        pathlib.Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(report, encoding="utf-8")
    _out("candidate_version", cand)
    _out("gate", "pass" if passed else "fail")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    prepare() if cmd == "prepare" else compare(args[0])
