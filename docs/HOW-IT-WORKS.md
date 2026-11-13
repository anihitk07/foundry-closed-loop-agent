# How the closed loop works (and what is automatic)

This repo implements the Foundry blog post
[Improve production agents with closed-loop evaluations in Microsoft Foundry](https://techcommunity.microsoft.com/blog/azure-ai-foundry-blog/improve-production-agents-with-closed-loop-evaluations-in-microsoft-foundry/4569762).
This document walks through each step in the order it runs. For each step, it says what triggers
the step, which code or API runs, the inputs and outputs, and how to inspect the result. The numbers
come from the real run against Foundry project `admin-0456` (account `admin-0456-resource`, Sweden Central).

A sequence diagram is in [`diagrams/sequence.png`](diagrams/sequence.png).

---

## 0. Is it fully automatic?

**No. In this implementation, only these parts run without a person or command:**

* trace capture
* the hourly trace-evaluation schedule
* the optimizer's internal candidate search
* the CI release gate on a pull request

**These steps are started by hand:**

* dataset mining
* the offline evaluation
* submitting the optimizer job
* turning the winning candidate into a PR
* merging the PR
* canary, promote and rollback

The loop as it ran:

```
create/deploy agent (azd deploy)                                  [command]
  -> every request emits OpenTelemetry spans to App Insights      [automatic]
  -> hourly traceEvaluation rule scores recent traces              [schedule; preview: runs end "failed", 0 traces]
  -> trace data-generation job mines traces into a dataset         [command: register_datasets.py --from-traces]
  -> offline eval on the curated train split (baseline)            [command: azd ai eval run start]
  -> Agent Optimizer generates and scores candidates               [command to submit; search is automatic]
  -> winner written to .agent_configs + PR opened                  [manual]
  -> release-gate.yml deploys an UNROUTED version and compares it  [automatic on pull_request]
  -> merge                                                         [manual; not enforced by branch protection]
  -> promote.yml canary (session-level)                            [manual workflow_dispatch]
  -> promote.yml promote / rollback (re-pin endpoint)              [manual workflow_dispatch]
```

Where the original mental model is wrong:

| Assumption | What actually happens here |
|---|---|
| "The dataset is created automatically from App Insights traces" | The trace-mining job only runs when you run `python data/register_datasets.py --from-traces`. The mined dataset (`refund-release-loop-traces`) is **not** used by the optimizer or the gate. Those use the curated `train`, `validation` and `release` splits. |
| "Evaluation runs" | The hourly rule exists. In this preview, though, its runs finish `failed` with 0 traces. The offline baseline eval only runs when you start it. |
| "The optimizer kicks in" | It only starts when you run `release/optimize.py submit`. After that, its candidate generation and scoring are automatic. |
| "It opens a PR on the best outcome" | No. A person reads the candidates, copies the winning instructions into git and runs `gh pr create`. |
| "A GitHub Action deploys that version with gated control" | Yes. The gate deploys a **new, unrouted** version and compares it with production. It does **not** route traffic. Routing is a separate, manual `promote.yml` run. |

### Step / trigger table

| # | Step | Trigger | Component | Output |
|---|---|---|---|---|
| 1 | Trace capture | automatic (every request) | Hosted-agent runtime + project App Insights connection | `invoke_agent refund-release-loop:N` spans in App Insights |
| 2 | Online trace evaluation | schedule (`0 * * * *` UTC), created once by a command (`release/monitor.py`) | Foundry evaluation rule `refund-release-loop-trace-eval` | Eval runs on eval `eval_6058407b...` (preview: `failed`, 0 traces) |
| 3 | Dataset mining | command (`data/make_datasets.py`, `data/register_datasets.py [--from-traces]`) | Foundry datasets + trace data-generation job | `refund-release-loop-{train,validation,release}` v1, `refund-release-loop-traces` v1.0 (30 samples) |
| 4 | Offline evaluation | command (`azd ai eval run start`) | `evals/azure.eval.yaml` -> eval `refund-train-eval` | Run `baseline-v1`: 4/6 passed |
| 5 | Optimizer | command (`release/optimize.py submit`); search is automatic | Foundry Agent Optimizer job | `opt_cf7fd22a...`: scored candidates + draft versions |
| 6 | Candidate -> PR | manual | git + `gh pr create` | PR #1 (`optimize/candidate-1`) |
| 7 | CI release gate | PR event (path filter) or `workflow_dispatch` | `.github/workflows/release-gate.yml` + `release/release_gate.py` | New unrouted version, `gate-report` artifact, pass/fail check |
| 8 | Canary | manual `workflow_dispatch` (`action=canary`) | `.github/workflows/promote.yml` + `release/canary.py` | HOLD / ABORT per stage |
| 9 | Promote / rollback | manual `workflow_dispatch` (`action=promote` or `rollback`) | `promote.yml` + `canary.py` -> `routing.pin` | Endpoint `version_selector` pinned to one version |
| 10 | Version tracking | automatic | Foundry versions, traces, git, gate report | Every response attributable to `refund-release-loop:N` |

---

## 1. Trace capture

| | |
|---|---|
| Trigger | Automatic, on every request to the hosted agent. |
| Inside | The agent (`src/refund-agent/main.py`) is a Responses-protocol hosted agent built on `ResponsesAgentServerHost`. It doesn't set up OpenTelemetry itself. The hosted runtime emits the spans, and the App Insights connection string is injected into the container (see the comment in `src/refund-agent/.env.example`). The project has an **App Insights connection** to the resource in `rg-closed-loop-agent` (Log Analytics + App Insights), so spans land there. `handler()` runs `_run_agent()` (a tool-calling loop over `RefundLedger` in `refund_tools.py`). It emits each tool call and its result as `function_call` / `function_call_output` output items, so traces and evaluators can see refund **state**, not just the final text. |
| Inputs | Customer prompt. The config comes from `load_config()`, which reads `.agent_configs/baseline/` (`OPTIMIZATION_LOCAL_DIR` in `azure.yaml`): `instructions.md`, `tools.json` and the skills folder set in `metadata.yaml`. |
| Outputs | Spans named `invoke_agent refund-release-loop:N` in App Insights. |
| Inspect | In App Insights, open Transaction search or Logs and filter for span names that start with `invoke_agent refund-release-loop`. For example: `union requests, dependencies \| where name startswith "invoke_agent refund-release-loop"`. |
| This run | Traffic for v1 came from `release/suite.py` replays. `release/traffic-train.json` holds 12 requests through the endpoint, all `served_by` v1, pass rate 1.0. |

## 2. Online trace evaluation (scheduled)

| | |
|---|---|
| Trigger | Run `python release/monitor.py` once. After that, Foundry runs it on a **timer** every hour (`CronTrigger("0 * * * *")`, UTC). |
| Why a timer | Continuous `responseCompleted` rules don't support hosted agents. Instead, `monitor.py` creates an `EvaluationRule` with `event_type=TIMER_TRIGGER` and a `TraceEvaluationRuleAction`. |
| Inside | `monitor.py` first gets or creates the eval `refund-release-loop-production-traces` with `oai.evals.create(data_source_config={"type":"azure_ai_source","scenario":"traces"})`. Its judges are `builtin.task_adherence`, `builtin.tool_call_accuracy` and `builtin.intent_resolution`, all on `gpt-5.4-mini`. It then calls `project.evaluation_rules.create_or_update(id="refund-release-loop-trace-eval", ...)`. The data source is `TracesEvalRunDataSource(agent_name, lookback_hours=1, max_traces=50)`. `--run-now` also calls `oai.evals.runs.create(...)` with `data_source.type="azure_ai_traces"` and `lookback_hours=2`. |
| Live rule | Source `azure_ai_traces`, `agent_name=refund-release-loop`, `lookback_hours=1`, `max_traces=50`, `ingestion_delay_seconds=300`. The 300-second delay is a service default shown on the live rule; `monitor.py` doesn't set it. Eval id: `eval_6058407b67e74c6a83c8fb6b3c7b7c25`. |
| Permissions | The project managed identity needs **Reader** and **Log Analytics Reader** on App Insights and the workspace, plus **Foundry User** on the project. |
| Outputs | One eval run per hour under the eval above. |
| Inspect | Foundry portal > Evaluations > `refund-release-loop-production-traces`, or `oai.evals.runs.list(eval_id=...)`. |
| This run | **Preview caveat:** scheduled runs finish `failed` with 0 traces collected and no error text. Also, `builtin.tool_call_accuracy` can't score hosted agents because it needs tool definitions (see the README). So this step is in place, but it produced no usable signal. Production feedback reached the loop through step 3 instead. |

## 3. Dataset mining from traces (+ curated splits)

| | |
|---|---|
| Trigger | Command. There's no schedule. |
| Curated splits | `python data/make_datasets.py` writes `data/{train,validation,release}.jsonl`. Each file has 6 scenarios: `happy_path`, `already_completed`, `processor_timeout`, `prompt_injection`, `outside_window` and `pending_status`. Each split uses a different phrasing of the same scenarios. `release` is the held-out set, which the optimizer never sees. Each row has `query`, `order_id`, `ground_truth` and `criteria`. |
| Register | `python data/register_datasets.py` calls `project.datasets.upload_file(name=f"{AGENT}-{split}", version=DATASET_VERSION or "1")` for each split. |
| Trace mining | `--from-traces` calls `project.datasets.begin_create_generation_job(EvaluationDataGenerationJobInputs(...))`, sending the header `Foundry-Features: DataGenerationJobs=V1Preview`. Its parts: **Source:** `TracesDataGenerationJobSource(agent_name="refund-release-loop", agent_version="1", start_time=now-6h)`. **Config:** `TracesDataGenerationJobConfiguration(max_samples=30, redact_private_content=True)`. **Output:** `EvaluationDataGenerationJobOutputConfiguration(name="refund-release-loop-traces")`. The script then blocks on `poller.result()`. |
| Outputs | Datasets `refund-release-loop-train`, `-validation` and `-release` (v1), plus `refund-release-loop-traces` v1.0. |
| Inspect | Foundry portal > Data > Datasets, or `project.datasets.list()`. Job status: Foundry MCP `data_generation_job_get` or the portal. |
| This run | Job `datagen-7099c22949004e5ba6664cdd96f9bbd4` mined **30 samples** from v1 production traces into `refund-release-loop-traces` v1.0. |
| Gap | `optimize.py` (`_ds()`) and `evals/azure.eval.yaml` only reference the **curated** `train` and `validation` datasets. The trace-mined dataset is produced but nothing in the loop uses it yet. The docstring in `make_datasets.py` says that in production these rows would come from traces plus human-labelled `ground_truth`. The trace source is also hard-coded to `agent_version="1"` with a 6-hour lookback. |

## 4. Offline evaluation run (baseline)

| | |
|---|---|
| Trigger | Command: `azd ai eval run start --eval refund-train-eval --name <run> --wait`. The README example uses `--name baseline`; this run was named `baseline-v1`. |
| Config | `evals/azure.eval.yaml` was generated by `azd ai eval init` and is wired into `azure.yaml` as service `refund-release-loop-evals` (`host: azure.ai.eval`). It defines the eval `refund-train-eval`: dataset `refund-release-loop-train`, `evaluationLevel: turn`, judges `builtin.task_adherence` and `builtin.task_completion` (`gpt-5.4-mini`), and target agent `refund-release-loop`. |
| Inside | Foundry sends each train row to the agent, collects the responses and scores them with the two LLM judges. |
| Outputs | An eval run with per-row judge scores. |
| Inspect | Foundry portal > Evaluations > `refund-train-eval`, or `azd ai eval run show`. |
| This run | `baseline-v1`: **4/6 passed**. |
| Caveat | `builtin.tool_call_accuracy` doesn't work for hosted agents. LLM judges also miss state errors (see step 7). That's why the release gate uses deterministic checks instead (`release/refund_checks.py`). |
| Deterministic baseline | `release/baseline-v1.json` (from `suite.py` on the release split against v1): pass rate **0.833**. `pending_status_release` failed with `repeated_submission`, meaning it resubmitted a refund that was already pending. |

## 5. Optimizer cycles

| | |
|---|---|
| Trigger | Command: `python release/optimize.py submit --base-version 1`. Once the job is submitted, the candidate search runs on its own. |
| Inside | `submit()` builds an `AgentOptimizationJob` and calls `project.agents.begin_create_optimization_job(job)`. Key fields: **`target_configuration`:** `{type: foundry_agent, name, version: base_version}`. **`optimization_model_configuration.model`:** `gpt-5.4`, which proposes the mutations. **`evaluation_configuration`:** training set `refund-release-loop-train` v1, validation set `refund-release-loop-validation` v1 (`type: target_completion`, `dataset_reference`), the same two judges as `azure.eval.yaml`, and judge model `gpt-5.4-mini`. The judges are repeated in code; the YAML file isn't read. **`candidate_search_configuration.max_candidates`:** 2. **`baseline_agent_configuration`:** built by `baseline_config()`, which reads `instructions.md`, `tools.json` and every `skills/*/SKILL.md`. This is needed because the optimizer can't see inside a hosted container. **`agent_optimization_space.target_attributes`:** `["instructions","tools","skills"]`. |
| What the service does | It mutates the editable surface (instructions, tool descriptions, skills), evaluates each candidate on train and validation with the judges, scores it against the baseline and creates a **draft** agent version for each candidate. Nothing is routed. |
| Inspect | `optimize.py status <job>` (`get_optimization_job`) and `optimize.py candidates <job>` (`list_optimization_candidates`). Other SDK calls, used by hand and not wrapped in the repo: `get_optimization_candidate` (its `output.mutations[].value` holds the new instructions), `download_optimization_job_file` and `promote_optimization_candidate`. |
| This run | Job `opt_cf7fd22a643844b1bf97b71ac9b143be`. **baseline 0.829**. **candidate_1 0.9125** changed instructions only; draft version `draft-1794580369606`. **candidate_2 0.867** changed instructions and tools. |

## 6. Candidate -> PR

| | |
|---|---|
| Trigger | **Manual.** |
| What was done | A person picked candidate_1 and read its `get_optimization_candidate(...).output.mutations[].value`. They wrote that text into `src/refund-agent/.agent_configs/baseline/instructions.md` on branch `optimize/candidate-1` and ran `gh pr create`. That opened PR #1, "Optimizer candidate_1: check refund status before submitting". |
| Why git and not `promote_optimization_candidate` | The draft version lives only in Foundry. Putting the config in git makes the change reviewable and diffable, and the CI gate rebuilds the version from source with `azd deploy`. At runtime, `main.py` loads `.agent_configs` through `load_config()`. |
| Outputs | PR #1, later squash-merged as commit `4f14806`. The diff only touches `instructions.md`. |
| Human edit | After gate iteration 1 failed (step 7), a review fix was added to the same PR. It is the "After submitting a refund" block at the end of `instructions.md`: report exactly the post-submit state, never say "processed" unless the status is `Completed`, and don't resubmit on `outcome_unknown`. |

## 7. CI release gate

| | |
|---|---|
| Trigger | `pull_request` that touches `src/**`, `release/**`, `data/release.jsonl` or the workflow file, or `workflow_dispatch`. `concurrency: foundry-release` serializes it with `promote.yml`. |
| Auth | GitHub OIDC (`azure/login@v2`, then `azd auth login --federated-credential-provider github`) signs in as Entra app `gh-foundry-closed-loop-agent`. The app has **Foundry User** on the project and **Reader** on the account. Its federated-credential subjects use GitHub's immutable-ID form: `repo:anihitk07@4090037/foundry-closed-loop-agent@1456702956:{pull_request \| ref:refs/heads/main \| environment:production}`. |
| Steps | 1. Install azd and the `azure.ai.agents` / `azure.ai.evaluations` extensions, then bind an azd env to the existing project. 2. `release_gate.py prepare`: `routing.routed_version()` finds the production version and `routing.pin(prod)` pins it, so the new deploy can't auto-route to `latest`. 3. `azd deploy refund-release-loop` creates candidate version N from the PR head. **N is unrouted.** 4. `release_gate.py compare <prod>`: gets `latest`, runs `routing.wait_active(N)`, then `suite.run(prod, release_rows, REPEAT)` and `suite.run(N, ...)`. 5. Writes the step summary and uploads `release/gate-report.json` as the `gate-report` artifact. |
| `suite.run` | Creates a **version-pinned session** (`routing.create_session` posts `version_indicator: {type: version_ref, agent_version}`) and invokes `/endpoint/protocols/openai/responses` with that `agent_session_id`. Each turn's output items are scored by `refund_checks.check()`: **`repeated_submission`:** more than one `submit_refund`, or any submit when a refund already exists. **`unverified_claim`:** a regex match for a completion claim (with negation handling) while the replayed state isn't `completed`. **`claim_state_mismatch`:** a refund amount above the order total, or the text contains "9999". It also records latency and tokens. No LLM is involved. |
| Thresholds | Set by env vars `GATE_MIN_PASS_RATE`, `GATE_MAX_LATENCY_RATIO` and `GATE_REPEAT` (default 2, so 6 rows x 2 = 12 turns per version). All of these must hold: candidate pass rate >= **0.9**; candidate pass rate >= prod; 0 infra errors; candidate p50 latency <= **1.5x** prod. Token count is reported but not gated. |
| Outputs | Exit code 0 or 1 (the check status), `GITHUB_STEP_SUMMARY` table, `gate-report` artifact, and step outputs `candidate_version` and `gate`. |

Gate runs in this repo:

| Run | Candidate | Prod pass | Cand pass | p50 prod / cand (s) | Avg tokens prod / cand | Result | Why |
|---|---|---|---|---|---|---|---|
| `workflow_dispatch` on main | v2 (unoptimized config) | 0.833 | 0.833 | 6.79 / 7.14 | 798 / 793 | FAIL | Below the 0.9 floor; `pending_status` resubmits. |
| PR #1 iteration 1 | v3 (optimizer candidate_1 as-is) | 0.75 | 0.833 | 7.27 / 6.12 | 881 / 2026 | FAIL | `happy_path` and `processor_timeout` hit `unverified_claim` (said "processed" while `pending` / `outcome_unknown`). The LLM judges in step 5 missed this; the deterministic gate caught it. |
| PR #1 iteration 2 | v4 (+ human review fix) | 0.833 | **1.0** | 6.56 / **5.79** | 857 / 2238 | **PASS** | All checks green. |

The token increase (about 2.6x) comes from the longer instructions. Add a token or cost threshold to `checks` in `compare()` if that matters to you.

**Enforcement gap:** a failing gate only fails the `gate` check. A read-only check (`gh api repos/.../branches/main/protection`) shows `main` has **no branch protection and no rulesets**. So, as configured, a failed gate does **not** block merge. Add a branch rule that requires the `gate` status check.

## 8. Canary with controlled traffic

| | |
|---|---|
| Trigger | Manual: `promote.yml`, `workflow_dispatch`, `action=canary`, `prod_version=1`, `candidate_version=4`. |
| Why session-level | A Foundry endpoint routes to one version through `version_selector`; there is no weighted split. So the canary sends a share of **sessions** to the candidate with `version_indicator`, while everything else goes to the pinned endpoint. |
| Inside | `canary.canary(prod, cand, stages=[5,25,50], n=CANARY_REQUESTS or 12)`. At each stage it does the following: 1. Picks `n` random rows from `data/validation.jsonl` (seeded with `random.Random(7)`). 2. Assigns about `pct`% of them to the candidate, with at least one. 3. Runs the candidate rows through `suite.run(cand, ...)`, which uses a pinned session, and the rest through `suite.run("endpoint", ...)`, which uses the production route. 4. Returns **HOLD** if candidate pass rate >= prod pass rate and the candidate had 0 errors. Otherwise it returns **ABORT** and exits 1, leaving the endpoint unchanged. |
| Important | This is **synthetic replay** of the validation split, not a split of real customer traffic. Real users stay 100% on the pinned version during the canary. A real-traffic canary would need the client application to create `version_indicator` sessions for a share of real users. |
| Outputs | Per-stage HOLD or ABORT lines, then `routing.py show`. |
| This run | 5%: v4 1 req pass 1.0 \| endpoint 10 req pass 0.9 -> HOLD. 25%: v4 3 req pass 1.0 \| endpoint 4 req pass 1.0 -> HOLD. 50%: v4 6 req pass 1.0 \| endpoint 3 req pass 1.0 -> HOLD. Result: "canary healthy at all stages". The endpoint stayed on v1. |

The production-side counts are below `n` because `to_prod` drops any row identical to one sent to the candidate.

## 9. Promote / rollback

| | |
|---|---|
| Trigger | Manual: `promote.yml` with `action=promote candidate_version=4`, or `action=rollback prod_version=<P>`. |
| Inside | `canary.py promote\|rollback V` runs `routing.wait_active(V)`, then `routing.pin(V)`. That sends a `PATCH /agents/refund-release-loop` (merge-patch) with `agent_endpoint.version_selector.version_selection_rules = [{agent_version: V, traffic_percentage: 100, type: FixedRatio}]`. Afterwards `routing.py show` prints `latest` and `routed_version`. |
| Approval | The job declares `environment: production`, and the workflow comment says "required reviewer". However, a read-only check (`gh api repos/.../environments`) shows `production` has **no protection rules**, so no approval is actually required. Add required reviewers to the environment to make this a real manual gate. |
| This run | Promote: "endpoint now routes 100% to v4", and `routed_version: "4"`. Rollback would be `action=rollback prod_version=1`, which re-pins v1 instantly. v1 is immutable and still active. |

## 10. Version tracking & attribution

| Source | What it tells you | How |
|---|---|---|
| Foundry agent versions | Every `azd deploy` creates an immutable `refund-release-loop:N` (v1 to v4 exist). | `python release/routing.py show`; `GET /agents/refund-release-loop/versions/N` (`routing.version_status`) |
| Endpoint routing | Which version serves new production traffic (now v4, FixedRatio 100%). | `routing.routed_version()` |
| Git | The config lineage for each version: `.agent_configs/baseline/*` and the agent code. Merge commit `4f14806` = v4's config. | `git log -- src/refund-agent/.agent_configs` |
| Traces | The version that actually served each request (span `invoke_agent refund-release-loop:N`). | App Insights |
| Gate report | prod vs candidate version numbers, per-case results, `served_by`. | `gate-report` artifact |

**Quirk:** a response's `agent_reference.version` reports the **endpoint's routed** version, even inside a session pinned to another version. To get the real version, `suite.run` calls `routing.session_version(sid)`, which reads the session's `version_indicator`, and reports it as `served_by`. Traces also show the real version.

**Note:** the gate deploys from the PR head. Merging doesn't redeploy anything (the workflow has no `push` trigger), so v4 is the version built from PR #1's final commit. Its content matches what was merged.

---

## Making it hands-free

This section describes the changes; none of it is implemented. Each manual step above maps to a change below.

| Manual step | How to automate it |
|---|---|
| 2: trace eval (preview failure) | Keep the rule, and alert on its eval-run results once the preview works. Until then, schedule `suite.py endpoint data/validation.jsonl` (for example, hourly) as a synthetic probe. |
| 3: trace mining | Add a scheduled workflow (for example, daily `cron`) that runs `register_datasets.py --from-traces`. Change it to use `routing.routed_version()` instead of the hard-coded `agent_version="1"`, and bump `DATASET_VERSION`. Feed the reviewed or labelled mined rows into the next `-train` version. Today nothing reads `refund-release-loop-traces`. |
| 4 + 5: eval + optimizer | Add an `optimize.yml` workflow, triggered by `schedule` or by a signal (an eval-score drop, or `workflow_dispatch` from an alert). Steps: 1. `optimize.py submit --base-version $(routing.py show -> routed_version)`. 2. Poll `get_optimization_job` until it finishes. 3. `list_optimization_candidates`, then pick the best score that beats the baseline by a margin. 4. `get_optimization_candidate` -> write each `output.mutations[]` (`instructions` -> `instructions.md`, `tools` -> `tools.json`, `skills` -> `skills/*/SKILL.md`). Use `download_optimization_job_file` for full artifacts. 5. Create branch `optimize/<job>` and commit. 6. `gh pr create` with the scores in the body. It needs `contents: write` and `pull-requests: write`, and the same OIDC login (add a federated credential for that workflow's subject). |
| 6 -> 7 hand-off | PRs opened with the default `GITHUB_TOKEN` **don't trigger** `pull_request` workflows. Open the PR with a GitHub App token or a fine-grained PAT, or have `optimize.yml` call `gh workflow run release-gate.yml --ref optimize/<job>`. |
| 7: enforcement | Add branch protection or a ruleset on `main` that requires the `gate` check (plus a code-owner review if you want a person to stay in the loop). Optionally run `gh pr merge --auto --squash` so a passing PR merges itself. |
| 7: iteration-1 style failures | Keep a person here, or let a failed gate send its failing cases back. Add the failing rows from `gate-report.json` to the train split and resubmit the optimizer with the failure text as context. |
| 8: canary | Trigger `promote.yml` (`action=canary`) from a `workflow_run` on `release-gate` success after merge, passing `prod_version` and `candidate_version` from the gate outputs. Wire the step outputs into a job output, or read the `gate-report` artifact. For real-traffic canary, have the client create `version_indicator` sessions for X% of users. |
| 9: promote / rollback | In the same job, run `canary.py promote <cand>` only when the canary exits 0. Add required reviewers to the `production` environment if promotion should still need approval. For automatic rollback, add a scheduled post-promote check (`suite.py endpoint ...` or trace-eval scores) that runs `canary.py rollback <prev>` on regression. |

With these changes, the only remaining human touchpoints are the ones you choose to keep: approving the PR and approving the `production` environment.
