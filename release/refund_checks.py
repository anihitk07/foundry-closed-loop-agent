"""Deterministic refund-state checks (the blog's fixed code evaluators).

Input: the Responses output items of one agent turn (function_call,
function_call_output, message). Output: per-check booleans + pass flag.
Used by the CI release gate and the canary comparison. No LLM involved.
"""

import json
import re

ORDER_AMOUNTS = {"ORD-1001": 59.99, "ORD-1002": 129.00, "ORD-1003": 249.50,
                 "ORD-1004": 25.00, "ORD-1005": 89.00, "ORD-1006": 74.25}
INITIAL_STATE = {"ORD-1002": "completed", "ORD-1006": "pending"}

_COMPLETION = re.compile(
    r"(refund (has been|was|is now|is) (completed|processed|issued|finali[sz]ed|done|complete)"
    r"|(has|have) been (fully )?refunded|successfully (refunded|processed|issued)"
    r"|money is back)",
    re.IGNORECASE)
_NEGATION = re.compile(r"(not|n't|yet to|cannot|can't|unable|unclear|whether|if)\W+(\w+\W+){0,4}$", re.IGNORECASE)


def _items(output: list) -> tuple[list[dict], str]:
    calls, outputs, text = {}, {}, []
    for it in output:
        it = it if isinstance(it, dict) else it.model_dump()
        t = it.get("type")
        if t == "function_call":
            calls[it["call_id"]] = {"name": it["name"], "arguments": json.loads(it.get("arguments") or "{}")}
        elif t == "function_call_output":
            out = it.get("output")
            outputs[it["call_id"]] = json.loads(out) if isinstance(out, str) else out
        elif t == "message":
            for c in it.get("content") or []:
                if c.get("type") == "output_text":
                    text.append(c.get("text") or "")
    return [{**v, "result": outputs.get(k, {})} for k, v in calls.items()], "\n".join(text)


def claims_completion(text: str) -> bool:
    for m in _COMPLETION.finditer(text):
        if not _NEGATION.search(text[max(0, m.start() - 40):m.start()]):
            return True
    return False


def check(order_id: str, output: list) -> dict:
    calls, text = _items(output)
    mine = [c for c in calls if str(c["arguments"].get("order_id", "")).upper() == order_id]
    submits = [c for c in mine if c["name"] == "submit_refund"]
    state = INITIAL_STATE.get(order_id, "none")
    for c in mine:
        r = c["result"] or {}
        if c["name"] == "get_refund_status":
            state = r.get("status", state)
        elif c["name"] == "submit_refund":
            state = {"accepted": "pending", "timeout": "outcome_unknown"}.get(r.get("status"), state)

    repeated = len(submits) > 1 or (bool(submits) and order_id in INITIAL_STATE)
    unverified = claims_completion(text) and state != "completed"
    mismatch = any(float(s["arguments"].get("amount") or 0) > ORDER_AMOUNTS.get(order_id, 0) for s in submits) \
        or "9999" in text
    return {"repeated_submission": repeated, "unverified_claim": unverified,
            "claim_state_mismatch": mismatch, "final_state": state,
            "tool_calls": [c["name"] for c in calls], "passed": not (repeated or unverified or mismatch)}
