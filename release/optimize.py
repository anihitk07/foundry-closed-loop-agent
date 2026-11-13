"""Step 4 of the loop: Agent Optimizer.

Submits an agent_optimization job against the live agent (baseline = current
production version) using the train/validation datasets and the same judges as
the eval config. Candidates come back as *draft* versions; nothing is routed.

  python release/optimize.py submit [--base-version 1]
  python release/optimize.py status <job_id>
  python release/optimize.py candidates <job_id>
"""

import json
import os
import pathlib
import re
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import AgentOptimizationJob
from azure.identity import AzureCliCredential

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT",
                          "https://admin-0456-resource.services.ai.azure.com/api/projects/admin-0456")
AGENT = os.environ.get("AGENT_NAME", "refund-release-loop")
EVAL_MODEL = os.environ.get("JUDGE_MODEL", "gpt-5.4-mini")
OPT_MODEL = os.environ.get("OPTIMIZE_MODEL", "gpt-5.4")
project = AIProjectClient(endpoint=ENDPOINT, credential=AzureCliCredential())


AGENT_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "refund-agent"


def baseline_config() -> dict:
    """Hosted agents are opaque to the optimizer: ship the editable surface explicitly."""
    base = AGENT_DIR / ".agent_configs" / "baseline"
    skills = []
    for f in (AGENT_DIR / "skills").glob("*/SKILL.md"):
        text = f.read_text(encoding="utf-8")
        desc = re.search(r"^description:\s*(.+)$", text, re.M)
        body = re.sub(r"^---.*?---\s*", "", text, flags=re.S)
        skills.append({"name": f.parent.name, "description": desc.group(1).strip() if desc else f.parent.name,
                       "body": body})
    return {"system_prompt": (base / "instructions.md").read_text(encoding="utf-8"),
            "current_model": EVAL_MODEL,
            "tools": json.loads((base / "tools.json").read_text(encoding="utf-8")),
            "skills": skills}


def _ds(split):
    return {"type": "target_completion",
            "source": {"type": "dataset_reference", "name": f"{AGENT}-{split}", "version": "1"}}


def submit(base_version: str):
    job = AgentOptimizationJob({
        "display_name": f"{AGENT}-v{base_version}-optimize",
        "target_configuration": {"type": "foundry_agent", "name": AGENT, "version": base_version},
        "optimization_model_configuration": {"model": OPT_MODEL},
        "optimization_configuration": {
            "type": "agent_optimization",
            "goal": "improve_quality",
            "evaluation_configuration": {
                "training_set": _ds("train"),
                "validation_set": _ds("validation"),
                "evaluators": [{"name": "builtin.task_adherence"}, {"name": "builtin.task_completion"}],
                "evaluation_model": {"model": EVAL_MODEL},
            },
            "candidate_search_configuration": {"max_candidates": 2},
            "baseline_agent_configuration": baseline_config(),
            "agent_optimization_space": {"target_attributes": ["instructions", "tools", "skills"]},
        },
    })
    poller = project.agents.begin_create_optimization_job(job)
    jid = poller.polling_method()._initial_response.http_response.json().get("id")  # noqa: SLF001
    print("job:", jid)
    return jid


if __name__ == "__main__":
    cmd, *args = sys.argv[1:] or ["submit"]
    if cmd == "submit":
        submit(args[args.index("--base-version") + 1] if "--base-version" in args else "1")
    elif cmd == "status":
        j = project.agents.get_optimization_job(args[0])
        print(j.status, j.error or "", j.warnings or "")
    elif cmd == "candidates":
        for c in project.agents.list_optimization_candidates(args[0]):
            print(json.dumps(c.as_dict(), default=str)[:1500])
