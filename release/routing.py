"""Version routing helpers for a Foundry hosted agent (data-plane REST, v1).

  python release/routing.py show
  python release/routing.py pin <version>           # 100% of new traffic -> version
  python release/routing.py wait-active <version>
  python release/routing.py invoke <version|endpoint> "<prompt>"
"""

import json
import os
import sys
import time

import requests
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, ChainedTokenCredential, DefaultAzureCredential

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT",
                          "https://admin-0456-resource.services.ai.azure.com/api/projects/admin-0456").rstrip("/")
AGENT = os.environ.get("AGENT_NAME", "refund-release-loop")
_cred = ChainedTokenCredential(AzureCliCredential(), AzureDeveloperCliCredential(), DefaultAzureCredential())


def _h(extra=None):
    h = {"Authorization": "Bearer " + _cred.get_token("https://ai.azure.com/.default").token,
         "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _req(method, path, body=None, extra=None, ok=(200, 201, 202)):
    url = f"{ENDPOINT}{path}{'&' if '?' in path else '?'}api-version=v1"
    r = requests.request(method, url, headers=_h(extra), data=json.dumps(body) if body is not None else None, timeout=300)
    if r.status_code not in ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:600]}")
    return r.json() if r.text else {}


def get_agent():
    return _req("GET", f"/agents/{AGENT}")


def routed_version():
    rules = (get_agent().get("agent_endpoint") or {}).get("version_selector", {}).get("version_selection_rules") or []
    return rules[0]["agent_version"] if rules else "latest"


def pin(version: str):
    body = {"agent_endpoint": {"version_selector": {"version_selection_rules": [
        {"agent_version": str(version), "traffic_percentage": 100, "type": "FixedRatio"}]},
        "protocol_configuration": {"responses": {}}}}
    return _req("PATCH", f"/agents/{AGENT}", body, {"Content-Type": "application/merge-patch+json"})


def version_status(version: str) -> str:
    return _req("GET", f"/agents/{AGENT}/versions/{version}").get("status", "unknown")


def wait_active(version: str, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = version_status(version)
        if s == "active":
            return s
        if s == "failed":
            raise RuntimeError(f"version {version} failed")
        time.sleep(15)
    raise TimeoutError(f"version {version} not active after {timeout}s")


def create_session(version: str) -> str:
    body = {"version_indicator": {"type": "version_ref", "agent_version": str(version)}}
    return _req("POST", f"/agents/{AGENT}/endpoint/sessions", body)["agent_session_id"]


def session_version(session_id: str) -> str:
    s = _req("GET", f"/agents/{AGENT}/endpoint/sessions/{session_id}")
    return (s.get("version_indicator") or {}).get("agent_version")


def stop_session(session_id: str):
    try:
        _req("POST", f"/agents/{AGENT}/endpoint/sessions/{session_id}:stop", {}, ok=(200, 202, 204))
    except RuntimeError:
        pass


def invoke(prompt: str, session_id: str | None = None) -> dict:
    body = {"input": prompt, "stream": False}
    if session_id:
        body["agent_session_id"] = session_id
    t0 = time.time()
    out = _req("POST", f"/agents/{AGENT}/endpoint/protocols/openai/responses", body)
    out["_latency_s"] = round(time.time() - t0, 2)
    return out


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["show"]
    if cmd == "show":
        a = get_agent()
        print(json.dumps({"name": a.get("name"), "latest": a.get("versions", {}).get("latest", {}).get("version"),
                          "routed_version": routed_version()}, indent=2))
    elif cmd == "pin":
        pin(args[0]); print("routed_version ->", routed_version())
    elif cmd == "wait-active":
        print(args[0], wait_active(args[0]))
    elif cmd == "invoke":
        sid = None if args[0] == "endpoint" else create_session(args[0])
        r = invoke(args[1], sid)
        print(json.dumps({"session": sid, "latency_s": r["_latency_s"], "output": r.get("output")}, indent=1)[:3000])
        if sid:
            stop_session(sid)
