"""Step 1 of the loop: scheduled evaluation of production traces.

Hosted agents are not supported by responseCompleted (continuous) rules, so
we use a timerTrigger rule with a traceEvaluation action: every hour Foundry
pulls the agent's traces from App Insights and scores them with built-in judges.
"""

import os

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (CronTrigger, EvaluationRule, EvaluationRuleEventType,
                                      TraceEvaluationRuleAction, TracesEvalRunDataSource)
from azure.identity import AzureCliCredential

ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT",
                          "https://admin-0456-resource.services.ai.azure.com/api/projects/admin-0456")
AGENT = os.environ.get("AGENT_NAME", "refund-release-loop")
JUDGE = os.environ.get("JUDGE_MODEL", "gpt-5.4-mini")
SCENARIO = os.environ.get("EVAL_SCENARIO", "traces")
RULE_ID = f"{AGENT}-trace-eval"

project = AIProjectClient(endpoint=ENDPOINT, credential=AzureCliCredential())
oai = project.get_openai_client()

EVAL_NAME = f"{AGENT}-production-traces"
eval_obj = next((e for e in oai.evals.list(limit=100) if e.name == EVAL_NAME), None) or oai.evals.create(
    name=EVAL_NAME,
    data_source_config={"type": "azure_ai_source", "scenario": SCENARIO},
    testing_criteria=[
        {"type": "azure_ai_evaluator", "name": n, "evaluator_name": f"builtin.{n}",
         "initialization_parameters": {"deployment_name": JUDGE}}
        for n in ("task_adherence", "tool_call_accuracy", "intent_resolution")
    ],
)
rule = project.evaluation_rules.create_or_update(
    id=RULE_ID,
    evaluation_rule=EvaluationRule(
        display_name=f"{AGENT} hourly trace eval",
        description="Scores the last hour of production traces; regressions feed the optimization loop.",
        event_type=EvaluationRuleEventType.TIMER_TRIGGER,
        schedule=CronTrigger(expression="0 * * * *"),
        action=TraceEvaluationRuleAction(
            eval_id=eval_obj.id,
            source=TracesEvalRunDataSource(agent_name=AGENT, lookback_hours=1, max_traces=50),
        ),
        enabled=True,
    ),
)
print("eval:", eval_obj.id)
print("rule:", rule.id, rule.event_type, rule.provisioning_status)

if "--run-now" in __import__("sys").argv:
    run = oai.evals.runs.create(
        eval_id=eval_obj.id, name="on-demand-production-traces",
        data_source={"type": "azure_ai_traces", "agent_name": AGENT, "lookback_hours": 2, "max_traces": 50},
    )
    print("run:", run.id, run.status, getattr(run, "report_url", ""))
