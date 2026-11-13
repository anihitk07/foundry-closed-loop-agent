# Foundry closed-loop agent: evaluate → optimize → gate → canary → promote

A working implementation of
[Improve production agents with closed-loop evaluations in Microsoft Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/improve-production-agents-with-closed-loop-evaluations-in-microsoft-foundry/4569762).

**The app**: a Contoso refund agent, deployed as a Foundry hosted agent. It uses sandboxed
tools (`lookup_order`, `get_refund_status`, `submit_refund`) and ships with deliberately
weak instructions. Production failures (for example, re-submitting a refund that is
already *pending*) are caught by evaluation and fixed by the Foundry Agent Optimizer. Each
fix is then released through an eval-gated GitHub Actions pipeline, with version-pinned
canary and one-command promote or rollback.

```
 production traffic ──► traces (App Insights) ──► hourly trace-eval rule (monitor.py)
        ▲                                  │
        │                                  ▼
 promote / rollback            trace-mined + curated datasets (register_datasets.py)
 (pin endpoint)                            │
        ▲                                  ▼
 canary via version_indicator   eval config (evals/azure.eval.yaml) ─► baseline run
 sessions (canary.py)                      │
        ▲                                  ▼
 CI release gate  ◄── PR ◄── apply  ◄── Agent Optimizer (optimize.py) → draft candidates
 prod vs candidate on held-out release set (release_gate.py + refund_checks.py)
```

## Versions and routing

* **Version** = an immutable Foundry agent version (`refund-release-loop:N`). Every `azd deploy`
  creates a new one. Git holds the config lineage (`src/refund-agent/.agent_configs`).
* **Routing**: the endpoint's `version_selector` is pinned to the production version
  (`routing.py pin N`), so a new deploy never receives traffic automatically.
* **Canary**: Foundry routes an endpoint to a single version, so the canary sends a share of
  *sessions* to the candidate with `version_indicator`. Every response carries
  `agent_reference.version`, so each trace is attributable to its version.
* **Promote / rollback**: re-pin the endpoint (`canary.py promote N` or `rollback P`).

## Layout

| path | step |
|---|---|
| `src/refund-agent/` | hosted agent (Responses protocol) + optimizer baseline in `.agent_configs/` |
| `release/monitor.py` | 1 - hourly `traceEvaluation` rule over production traces (+ `--run-now`) |
| `data/register_datasets.py` | 2-3 - train / validation / held-out release datasets + traces → dataset job |
| `evals/azure.eval.yaml` | 4 - eval config (`azd ai eval init`) used for baseline + optimizer |
| `release/optimize.py` | 5 - Agent Optimizer job (instructions, tools, skills) |
| `release/refund_checks.py` | deterministic refund-state checks (repeated submission, unverified claim, state mismatch) |
| `release/suite.py` | run a split against a version-pinned session and score it |
| `release/release_gate.py` + `.github/workflows/release-gate.yml` | 6 - PR gate: deploy candidate unrouted, compare against prod |
| `release/canary.py` + `.github/workflows/promote.yml` | 7-8 - canary → promote / rollback (`production` environment) |

## Run it

```powershell
# local
cd src/refund-agent; uv sync; python main.py            # http://localhost:8088/responses
# Foundry
azd deploy refund-release-loop
python release/routing.py pin 1
python release/monitor.py --run-now
python data/register_datasets.py --from-traces
azd ai eval run start --eval refund-train-eval --name baseline --wait
python release/optimize.py submit --base-version 1
python release/optimize.py candidates <job-id>          # then apply the winner to .agent_configs and open a PR
```

CI needs these repo variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`AZURE_LOCATION`, `AZURE_RESOURCE_GROUP`, `AZURE_AI_ACCOUNT_NAME`, `AZURE_AI_PROJECT_NAME`
and `FOUNDRY_PROJECT_ENDPOINT`. It also needs an Entra app with GitHub OIDC federated credentials
(`pull_request`, `ref:refs/heads/main`, `environment:production`), with **Foundry User** on the
project and **Reader** on the account.

## Platform notes (found while building)

* Continuous `responseCompleted` evaluation rules don't support **hosted** agents. Use a
  `timerTrigger` rule with a `traceEvaluation` action instead. The project managed identity needs
  Reader + Log Analytics Reader on the App Insights / workspace and **Foundry User** on the project.
* The optimizer can't see inside a hosted container. Send `baseline_agent_configuration`
  (system prompt, tools, skills) and `agent_optimization_space.target_attributes`.
* `builtin.tool_call_accuracy` needs tool definitions, which hosted agents don't expose to the
  evaluator. The gate uses deterministic `refund_checks.py` for tool-state correctness.
