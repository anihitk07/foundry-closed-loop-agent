# Copyright (c) Microsoft. All rights reserved.
"""Contoso refund agent: optimization-ready hosted agent (Responses protocol).

Runs a function-calling loop over sandboxed refund tools and emits every tool
call/result as Responses output items, so traces, evaluators and the CI
release gate can check refund state, not just the final text.
"""

import asyncio
import json
import logging
import os

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

from azure.ai.agentserver.responses import (
    CreateResponse,
    ResponseContext,
    ResponsesAgentServerHost,
    ResponsesServerOptions,
    ResponseEventStream,
)
from azure.ai.agentserver.responses.models import (
    ResponseUsage,
    ResponseUsageInputTokensDetails,
    ResponseUsageOutputTokensDetails,
)
from azure.ai.agentserver.optimization import OptimizationConfig, load_config

from refund_tools import TOOL_DEFINITIONS, RefundLedger, dumps

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("refund-agent")

MAX_TOOL_ROUNDS = 6

config = load_config() or OptimizationConfig(
    instructions="You are a refund support agent for Contoso. Use the tools to help customers with refunds.",
    model=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME") or "gpt-5.4-mini",
    source="defaults",
)
MODEL = config.model or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-5.4-mini")
INSTRUCTIONS = config.compose_instructions()

# Optimized tool descriptions (tools.json) override the code defaults by name.
_optimized = {t["function"]["name"]: t["function"] for t in (config.tool_definitions or []) if "function" in t}
TOOLS = [
    {"type": "function", **{**t["function"], **_optimized.get(t["function"]["name"], {})}}
    for t in TOOL_DEFINITIONS
]
logger.info("Config loaded (source=%s, model=%s, tools=%d)", config.source, MODEL, len(TOOLS))

_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("AZURE_AI_PROJECT_ENDPOINT") or ""
if not _endpoint:
    raise EnvironmentError("FOUNDRY_PROJECT_ENDPOINT or AZURE_AI_PROJECT_ENDPOINT must be set.")
_responses = AIProjectClient(endpoint=_endpoint, credential=DefaultAzureCredential()).get_openai_client().responses

app = ResponsesAgentServerHost(options=ResponsesServerOptions(default_fetch_history_count=20))


def _history_items(current_input: str, history: list) -> list[dict]:
    items = []
    for item in history:
        for content in item.get("content") or []:
            text = content.get("text")
            if content.get("type") == "output_text" and text:
                items.append({"role": "assistant", "content": text})
            elif content.get("type") == "input_text" and text:
                items.append({"role": "user", "content": text})
    items.append({"role": "user", "content": current_input})
    return items


def _run_agent(input_items: list[dict]):
    """Function-calling loop. Returns (final_text, ledger, usage_totals)."""
    ledger = RefundLedger()
    usage = {"input": 0, "output": 0}
    for _ in range(MAX_TOOL_ROUNDS):
        resp = _responses.create(model=MODEL, instructions=INSTRUCTIONS, input=input_items,
                                 tools=TOOLS, store=False)
        if resp.usage:
            usage["input"] += resp.usage.input_tokens or 0
            usage["output"] += resp.usage.output_tokens or 0
        calls = [o for o in resp.output if o.type == "function_call"]
        if not calls:
            return resp.output_text or "", ledger, usage
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = ledger.call(c.name, args)
            input_items.append({"type": "function_call", "call_id": c.call_id, "name": c.name,
                                "arguments": c.arguments})
            input_items.append({"type": "function_call_output", "call_id": c.call_id, "output": dumps(result)})
    return "I'm sorry, I couldn't complete this request. A specialist will follow up.", ledger, usage


@app.response_handler
async def handler(request: CreateResponse, context: ResponseContext, _cancel: asyncio.Event):
    user_input = await context.get_input_text() or "Hello!"
    history = await context.get_history()
    text, ledger, tok = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _run_agent(_history_items(user_input, history)))
    logger.info("response %s tool_calls=%s", context.response_id, [c["name"] for c in ledger.calls])

    usage = ResponseUsage(
        input_tokens=tok["input"], output_tokens=tok["output"], total_tokens=tok["input"] + tok["output"],
        input_tokens_details=ResponseUsageInputTokensDetails(cached_tokens=0),
        output_tokens_details=ResponseUsageOutputTokensDetails(reasoning_tokens=0),
    )
    stream = ResponseEventStream(response_id=context.response_id, request=request)

    async def _events():
        yield stream.emit_created()
        yield stream.emit_in_progress()
        for i, call in enumerate(ledger.calls):
            call_id = f"call_{i}"
            args = dumps(call["arguments"])
            fc = stream.add_output_item_function_call(call["name"], call_id)
            yield fc.emit_added()
            yield fc.emit_arguments_delta(args)
            yield fc.emit_arguments_done(args)
            yield fc.emit_done()
            fo = stream.add_output_item_function_call_output(call_id)
            yield fo.emit_added(dumps(call["result"]))
            yield fo.emit_done(dumps(call["result"]))
        message = stream.add_output_item_message()
        yield message.emit_added()
        part = message.add_text_content()
        yield part.emit_added()
        yield part.emit_delta(text)
        yield part.emit_text_done(text)
        yield part.emit_done()
        yield message.emit_done()
        yield stream.emit_completed(usage=usage)

    return _events()


app.run()
