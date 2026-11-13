# Runbook: deploy and test the closed loop from scratch

This runbook reproduces **evaluate → optimize → gate → canary → promote** in **your own**
subscription and GitHub repo. Every command is PowerShell, run from the repo root unless noted.

Placeholders: `<sub-id>`, `<tenant-id>`, `<rg>` (the RG that holds the Foundry account),
`<account>`, `<project>`, `<owner>/<repo>`. Where a real value is shown, it is from the reference run:
account `admin-0456-resource`, project `admin-0456`, RG `rg-admin-0456`, Sweden Central,
observability RG `rg-closed-loop-agent`, repo `anihitk07/foundry-closed-loop-agent`.

| step | what | where |
|---|---|---|
| 0 | prerequisites + session preamble | laptop |
| 1 | fork/clone, local smoke test | laptop |
| 2 | Foundry project, App Insights, connection, RBAC | Azure |
| 3 | deploy agent v1 (weak baseline) + pin | Foundry |
| 4 | production monitoring rule + traffic | Foundry |
| 5 | datasets (curated + trace-mined) | Foundry |
| 6 | baseline evaluation | Foundry |
| 7 | optimizer + candidates | Foundry |
| 8 | apply winner, open PR | GitHub |
| 9 | CI identity + repo setup (do before 8) | Entra + GitHub |
| 10 | release gate | GitHub Actions |
| 11–13 | canary, promote, rollback drill | GitHub Actions |
| 14–16 | verify, teardown, troubleshooting | — |

---

## 0. Prerequisites

**Tools** (tested versions in brackets):

| tool | min | install / check |
|---|---|---|
| Azure CLI | 2.73+ (2.92.0) | `az version` |
| Azure Developer CLI | 1.27.1+ (`azure.yaml` `requiredVersions`) (1.37.1) | `winget install Microsoft.Azd`; `azd version` |
| azd extensions | `azure.ai.agents` ≥ 1.0.0-beta.9 (1.1.0), `azure.ai.evaluations` (1.0.0-beta.2) | installed in step 3 |
| Python | 3.12+ (hosted runtime is `python_3_13`) | `python --version` |
| uv | any recent | `pip install uv` |
| GitHub CLI | any recent, token with `repo` + `workflow` scopes | `gh auth status` |
| git | any | `git --version` |

**Azure permissions you need:**

* **Owner** or **User Access Administrator** on the Foundry RG and on the new observability RG (you create role assignments).
* **Foundry User** (`53ca6127-db72-4b80-b1b0-d745d6d5456d`; older tenants call it *Azure AI User*) on the project. Owner does **not** include data actions, so assign it explicitly (step 2.4).
* Permission to create **Entra app registrations** (Application Developer or tenant default).
* A region with hosted agents + Agent Optimizer (reference run: **Sweden Central**).
* Model deployments in the project: **`gpt-5.4-mini`** (agent + judge) and **`gpt-5.4`** (optimizer). These names are hard-coded in `azure.yaml`-driven env, `evals/azure.eval.yaml`, `.agent_configs/baseline/metadata.yaml`, `release-gate.yml` and the scripts' defaults. If you use other names, change all of them.

**GitHub:** admin on `<owner>/<repo>` (variables, environments, Actions).

### Session preamble (paste at the start of EVERY PowerShell session)

Isolate config per tenant so a stray login never deploys to the wrong tenant:

```powershell
$env:AZURE_CONFIG_DIR = "$env:USERPROFILE\.azure-tenants\<tenant-alias>"
$env:AZD_CONFIG_DIR   = "$env:USERPROFILE\.azd-tenants\<tenant-alias>"
$SUB = "<sub-id>"; $TENANT = "<tenant-id>"; $LOC = "swedencentral"
$RG = "<rg>"; $ACCOUNT = "<account>"; $PROJECT = "<project>"        # e.g. rg-admin-0456 / admin-0456-resource / admin-0456
$OBS_RG = "rg-closed-loop-agent"; $LAW = "log-closed-loop"; $APPI = "appi-closed-loop"
$REPO = "<owner>/<repo>"                                             # e.g. anihitk07/foundry-closed-loop-agent
$ACCOUNT_ID = "/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$ACCOUNT"
$PROJECT_ID = "$ACCOUNT_ID/projects/$PROJECT"
$env:FOUNDRY_PROJECT_ENDPOINT = "https://$ACCOUNT.services.ai.azure.com/api/projects/$PROJECT"
$env:AGENT_NAME = "refund-release-loop"
$env:PYTHONIOENCODING = "utf-8"

function Assert-Tenant {
  $a = az account show --query "{s:id,t:tenantId,u:user.name}" -o json | ConvertFrom-Json
  if ($a.s -ne $SUB -or $a.t -ne $TENANT) { throw "WRONG CONTEXT: sub=$($a.s) tenant=$($a.t)" }
  "OK: $($a.u) on $($a.s)"
}
```

> ⚠️ All scripts in `release/` and `data/` default to the reference project (`admin-0456`) when
> `FOUNDRY_PROJECT_ENDPOINT` is unset. Always run the preamble.

First login (once per tenant alias):

```powershell
az login --tenant $TENANT
az account set --subscription $SUB
azd auth login --tenant-id $TENANT        # azd has its own token cache, separate from az
Assert-Tenant
```

Expected: `OK: you@contoso.com on <sub-id>`.

---

## 1. Fork/clone + local smoke test

**Goal:** get the code into your repo and prove the agent runs locally against your model.

> Needs a Foundry project with a `gpt-5.4-mini` deployment and **Foundry User** for you. If you
> don't have one yet, do **2.1** and **2.4 (your user)** first, then come back.

### 1.1 Fork (or copy) the repo

```powershell
$NAME = $REPO.Split('/')[1]
gh repo fork anihitk07/foundry-closed-loop-agent --clone --fork-name $NAME
cd $NAME
gh repo set-default $REPO        # otherwise gh pr create targets the upstream repo
gh api -X PUT "repos/$REPO/actions/permissions" -F enabled=true   # Actions are off on new forks
```

Alternative (standalone repo, no fork link):
`git clone https://github.com/anihitk07/foundry-closed-loop-agent <repo>; cd <repo>; git remote remove origin; gh repo create $REPO --public --source . --push`
(pushing `.github/workflows/*` needs the `workflow` scope: `gh auth refresh -h github.com -s workflow`).

### 1.2 Point the repo at your project

`azure.yaml` hard-codes the reference project endpoint. Replace it:

```powershell
(Get-Content azure.yaml -Raw) -replace 'https://admin-0456-resource\.services\.ai\.azure\.com/api/projects/admin-0456', $env:FOUNDRY_PROJECT_ENDPOINT |
  Set-Content azure.yaml -NoNewline
Select-String -Path azure.yaml -Pattern 'endpoint:'
```

Expected: `endpoint: https://<account>.services.ai.azure.com/api/projects/<project>`.

### 1.3 Reset to the weak baseline (to reproduce the loop)

`main` already contains the optimized + review-fixed instructions (the result of the reference run).
To reproduce the whole loop, start from the original one-line baseline (commit `2bfb5d9`):

```powershell
git show 2bfb5d9:src/refund-agent/.agent_configs/baseline/instructions.md |
  Set-Content src\refund-agent\.agent_configs\baseline\instructions.md -Encoding ascii
Get-Content src\refund-agent\.agent_configs\baseline\instructions.md
git add azure.yaml src/refund-agent/.agent_configs/baseline/instructions.md
git commit -m "Point at my Foundry project; reset to weak baseline"
git push
```

Expected instructions: `You are a refund support agent for Contoso. Use the tools to help customers get their refunds quickly.`
(Pushing to `main` does not trigger the gate; it only runs on `pull_request` or manual dispatch.)

### 1.4 Python env for the release/data scripts (repo root)

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -r src\refund-agent\pyproject.toml requests
.\.venv\Scripts\Activate.ps1      # if blocked by execution policy, call .venv\Scripts\python.exe directly
python -c "import azure.ai.projects, requests; print(azure.ai.projects.__version__)"
```

Expected: `2.11.0`.

### 1.5 Run the agent locally

Terminal A:

```powershell
cd src\refund-agent
$env:AZURE_AI_MODEL_DEPLOYMENT_NAME = "gpt-5.4-mini"
$env:OPTIMIZATION_LOCAL_DIR = ".agent_configs"
uv sync
uv run python main.py
```

Expected log: `Config loaded (source=..., model=gpt-5.4-mini, tools=3)` and a server on `http://localhost:8088`.

Terminal B (run the preamble first):

```powershell
$body = @{ input = "Where is my ORD-1006 refund? Just resubmit it if it's stuck."; stream = $false } | ConvertTo-Json
$r = Invoke-RestMethod http://localhost:8088/responses -Method Post -ContentType "application/json" -Body $body
$r.output | Select-Object type, name, arguments | Format-Table -AutoSize
($r.output | Where-Object type -eq message).content.text
# score it with the same deterministic checks the gate uses
$r | ConvertTo-Json -Depth 20 | Set-Content local-resp.json -Encoding utf8
python -c "import json,sys; sys.path.insert(0,'release'); from refund_checks import check; print(check('ORD-1006', json.load(open('local-resp.json',encoding='utf-8-sig'))['output']))"
Remove-Item local-resp.json
```

Expected: `function_call` / `function_call_output` items for `get_refund_status` (and, with the weak
baseline, often `submit_refund` → `repeated_submission: True, passed: False` — that is the bug the loop fixes).
Stop the server with Ctrl+C.

---

## 2. Azure foundation

**Goal:** Foundry project with models, App Insights connected to the project (so hosted-agent traces land
there), and the RBAC the trace evaluator needs.

### 2.1 (Only if you have no project) Foundry account, project, models

```powershell
Assert-Tenant
az group create -n $RG -l $LOC -o none
az cognitiveservices account create -n $ACCOUNT -g $RG -l $LOC --kind AIServices --sku S0 `
  --custom-domain $ACCOUNT --allow-project-management --assign-identity --yes -o none
az cognitiveservices account project create -n $ACCOUNT -g $RG -l $LOC --project-name $PROJECT -o none   # system-assigned identity by default
az cognitiveservices account list-models -n $ACCOUNT -g $RG --query "[?name=='gpt-5.4-mini' || name=='gpt-5.4'].{name:name,version:version}" -o table
$VER_MINI = "<gpt-5.4-mini version from the table>"; $VER_54 = "<gpt-5.4 version from the table>"
az cognitiveservices account deployment create -n $ACCOUNT -g $RG --deployment-name gpt-5.4-mini `
  --model-name gpt-5.4-mini --model-version $VER_MINI --model-format OpenAI --sku-name GlobalStandard --sku-capacity 100 -o none
az cognitiveservices account deployment create -n $ACCOUNT -g $RG --deployment-name gpt-5.4 `
  --model-name gpt-5.4 --model-version $VER_54 --model-format OpenAI --sku-name GlobalStandard --sku-capacity 100 -o none
```

Verify: `az cognitiveservices account deployment list -n $ACCOUNT -g $RG --query "[].name" -o tsv` lists both.

### 2.2 Observability RG: Log Analytics + App Insights

```powershell
Assert-Tenant
az extension add -n application-insights --upgrade -o none
az group create -n $OBS_RG -l $LOC -o none
az monitor log-analytics workspace create -g $OBS_RG -n $LAW -l $LOC -o none
$LAW_ID = az monitor log-analytics workspace show -g $OBS_RG -n $LAW --query id -o tsv
az monitor app-insights component create --app $APPI -g $OBS_RG -l $LOC --workspace $LAW_ID --kind web --application-type web -o none
$APPI_ID = az monitor app-insights component show --app $APPI -g $OBS_RG --query id -o tsv
$APPI_CS = az monitor app-insights component show --app $APPI -g $OBS_RG --query connectionString -o tsv
```

### 2.3 Connect App Insights to the project

Do this **before** deploying the agent (step 3): the hosted container picks up the connection at deploy time.

```powershell
@{ properties = @{ category = "AppInsights"; target = $APPI_ID; authType = "ApiKey"; isSharedToAll = $false
   credentials = @{ key = $APPI_CS }; metadata = @{ ApiType = "Azure"; ResourceId = $APPI_ID } } } |
  ConvertTo-Json -Depth 5 | Set-Content appi-conn.json -Encoding ascii
az rest --method put --url "https://management.azure.com$PROJECT_ID/connections/${APPI}?api-version=2025-06-01" --body "@appi-conn.json" -o none
Remove-Item appi-conn.json
az rest --method get --url "https://management.azure.com$PROJECT_ID/connections?api-version=2025-06-01" `
  --query "value[].{name:name,category:properties.category}" -o table
```

Expected: a row `appi-closed-loop  AppInsights`.

### 2.4 RBAC

```powershell
Assert-Tenant
$FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"; $READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
$LA_READER = "73c42c96-874c-492b-b04d-ab87d138a893";   $MON_READER = "43d0d8ad-25c7-4714-9337-8ba259a9fe05"
$OBS_SCOPE = az group show -n $OBS_RG --query id -o tsv
$ME      = az ad signed-in-user show --query id -o tsv
$PROJ_MI = az cognitiveservices account project show -n $ACCOUNT -g $RG --project-name $PROJECT --query identity.principalId -o tsv
$ACCT_MI = az cognitiveservices account show -n $ACCOUNT -g $RG --query identity.principalId -o tsv

# you: deploy, invoke, eval, optimize
az role assignment create --assignee-object-id $ME --assignee-principal-type User --role $FOUNDRY_USER --scope $PROJECT_ID -o none
# project MI: read traces for scheduled trace evaluation + run evals in the project
foreach ($r in $READER, $LA_READER, $MON_READER) {
  az role assignment create --assignee-object-id $PROJ_MI --assignee-principal-type ServicePrincipal --role $r --scope $OBS_SCOPE -o none }
az role assignment create --assignee-object-id $PROJ_MI --assignee-principal-type ServicePrincipal --role $FOUNDRY_USER --scope $PROJECT_ID -o none
# account MI: read traces
foreach ($r in $READER, $LA_READER) {
  az role assignment create --assignee-object-id $ACCT_MI --assignee-principal-type ServicePrincipal --role $r --scope $OBS_SCOPE -o none }
```

Verify (allow 2–3 min for propagation):

```powershell
az role assignment list --assignee $PROJ_MI --all --query "[].{role:roleDefinitionName,scope:scope}" -o table
```

Expected: Reader, Log Analytics Reader, Monitoring Reader on `rg-closed-loop-agent`; Foundry User on the project.

---

## 3. Deploy agent v1 + pin production

**Goal:** immutable version `refund-release-loop:1` (weak baseline) serving 100% of the endpoint.

```powershell
Assert-Tenant
azd extension install azure.ai.agents azure.ai.evaluations
azd env new closedloop --subscription $SUB --location $LOC --no-prompt
azd env set AZURE_TENANT_ID $TENANT
azd env set AZURE_RESOURCE_GROUP $RG
azd env set AZURE_AI_ACCOUNT_NAME $ACCOUNT
azd env set AZURE_AI_PROJECT_NAME $PROJECT
azd env set AZURE_AI_PROJECT_ID $PROJECT_ID
azd env set FOUNDRY_PROJECT_ENDPOINT $env:FOUNDRY_PROJECT_ENDPOINT
azd env set AZURE_AI_PROJECT_CONNECTIONS_PROJECT_ENDPOINT $env:FOUNDRY_PROJECT_ENDPOINT
azd env set AZURE_OPENAI_ENDPOINT "https://$ACCOUNT.openai.azure.com/"
azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME gpt-5.4-mini
azd env set USE_EXISTING_AI_PROJECT true
azd env set ENABLE_HOSTED_AGENTS true
azd env set ENABLE_CAPABILITY_HOST false
azd env set AZD_AGENT_SKIP_ACR true
azd env set AZD_FOUNDRY_ACR_MODE none

azd deploy refund-release-loop
python release/routing.py wait-active 1
python release/routing.py pin 1
python release/routing.py show
```

(Same `azd env set` list as `release-gate.yml`. Every `azd deploy` creates a **new immutable version**.)

Expected:

```
1 active
routed_version -> 1
{ "name": "refund-release-loop", "latest": "1", "routed_version": "1" }
```

> If the agent name already existed in your project, `latest` won't be `1`. Use the number `show`
> prints everywhere this runbook says `1` (also `optimize.py --base-version`, and see the
> `register_datasets.py` note in step 5).

Verify an endpoint call and the release-set baseline:

```powershell
python release/routing.py invoke endpoint "Is my refund for ORD-1006 done yet?"
python release/suite.py 1 data/release.jsonl --out release/baseline-v1.json
```

Expected suite output (reference run): `"pass_rate": 0.833`, `"served_by": ["1"]`, and
`FAIL pending_status_release ... ['repeated_submission']`.

---

## 4. Production monitoring rule + traffic

**Goal:** hourly trace evaluation of production traffic, and real traces in App Insights.

Continuous `responseCompleted` rules don't support hosted agents, so `monitor.py` creates a
`timerTrigger` rule (cron `0 * * * *`) with a `traceEvaluation` action (last 1 h, ≤ 50 traces;
judges `task_adherence`, `tool_call_accuracy`, `intent_resolution`). Env overrides: `JUDGE_MODEL`
(default `gpt-5.4-mini`), `EVAL_SCENARIO` (default `traces`).

```powershell
# production traffic through the endpoint (6 cases x 2 repeats per split = 24 requests)
python release/suite.py endpoint data/train.jsonl --repeat 2 --out release/traffic-train.json
python release/suite.py endpoint data/validation.jsonl --repeat 2 --out release/traffic-val.json
# rule + one on-demand run over the last 2 h
python release/monitor.py --run-now
```

Expected:

```
eval: eval_...                       (name refund-release-loop-production-traces, reused if it exists)
rule: refund-release-loop-trace-eval <timerTrigger> ...
run: evalrun_... queued ...
```

Verify traces landed (spans are named `invoke_agent refund-release-loop:<version>`; can take ~5 min):

```powershell
az monitor app-insights query --app $APPI -g $OBS_RG --analytics-query `
  "union requests, dependencies | where timestamp > ago(2h) and name has 'invoke_agent' | summarize count() by name" -o table
```

> **Preview caveat:** scheduled/on-demand trace-eval runs may finish `failed` with 0 traces collected
> (no error text). Production feedback still reaches the loop via the trace data-generation job in step 5.

---

## 5. Datasets (curated + trace-mined)

**Goal:** `refund-release-loop-train` / `-validation` / `-release` (held out, never shown to the optimizer)
and `refund-release-loop-traces` (mined from v1 production traces).

```powershell
python data/make_datasets.py
python data/register_datasets.py --from-traces
azd ai eval dataset list
```

Expected:

```
wrote train.jsonl, validation.jsonl, release.jsonl
dataset: refund-release-loop-train 1 ...
dataset: refund-release-loop-validation 1 ...
dataset: refund-release-loop-release 1 ...
generation job submitted; waiting ...
generated samples: 30 [...]                (reference run)
```

Notes:
* `make_datasets.py` regenerates the committed JSONL files deterministically (no diff expected).
* `--from-traces` mines **agent_version `1`** traces from the **last 6 hours** (hard-coded). Run it within
  6 h of step 4's traffic. Without `--from-traces` only the three curated splits are registered.
* Dataset version comes from `DATASET_VERSION` (default `1`); `optimize.py` always reads version `1`.

---

## 6. Baseline evaluation

**Goal:** LLM-judge baseline for v1 on the train split, using `evals/azure.eval.yaml`
(eval `refund-train-eval`: `builtin.task_adherence` + `builtin.task_completion`, judge `gpt-5.4-mini`,
target agent `refund-release-loop`, dataset `refund-release-loop-train`).

```powershell
azd ai eval create refund-train-eval          # deploys the eval declared in the yaml (needed once)
azd ai eval run start --eval refund-train-eval --name baseline --wait
azd ai eval run list --eval refund-train-eval
```

Expected (reference run): 6 rows, **4/6 passed (66.7 %)** — `task_adherence` 6/6, `task_completion` 4/6.
Optional hard threshold: add `--fail-on pass-rate=0.9` (exits 1).

Inspect rows: `azd ai eval run output --help` (per-sample results) or `azd ai eval run show --eval refund-train-eval`.

---

## 7. Run the optimizer + inspect candidates

**Goal:** Agent Optimizer proposes draft candidates (nothing is routed). Hosted agents are opaque to the
optimizer, so `optimize.py` sends `baseline_agent_configuration` (instructions, `tools.json`, skills) and
`agent_optimization_space.target_attributes = [instructions, tools, skills]`.
Uses train/validation datasets v1, optimize model `OPTIMIZE_MODEL` (default `gpt-5.4`), judge `JUDGE_MODEL`
(default `gpt-5.4-mini`), `max_candidates: 2`.

```powershell
python release/optimize.py submit --base-version 1
$JOB = "<job-id printed above>"
python release/optimize.py status $JOB          # repeat until it succeeds (~15-20 min)
python release/optimize.py candidates $JOB
```

Expected: `job: opt_...`; `status` moves from in-progress to succeeded; `candidates` prints one
JSON line per candidate (truncated to 1500 chars) with `candidate_id`, `name`, `status`, `evaluation`.
Reference run:

| candidate | score | mutations |
|---|---|---|
| baseline | 0.829 | — |
| candidate_1 | **0.9125** | instructions |
| candidate_2 | 0.867 | instructions + tools |

Note the winner's **`candidate_id`** for step 8.

---

## 8. Apply the winning candidate + open a PR

> Do **step 9 first** if you want the PR to be gated immediately (the gate needs the CI identity and
> repo variables). Otherwise re-run it later with `gh workflow run release-gate.yml --ref optimize/candidate-1`.

```powershell
git switch -c optimize/candidate-1
$CAND = "<winning candidate_id>"
@'
import os, sys, pathlib
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
p = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=AzureCliCredential())
c = p.agents.get_optimization_candidate(sys.argv[1], sys.argv[2]).as_dict()
muts = (c.get("output") or {}).get("mutations") or []
print(c.get("name"), c.get("status"), "mutations:", [m.get("type") for m in muts])
ins = next((m.get("value") for m in muts if m.get("type") == "instructions"), None)
if not ins:
    sys.exit("no instructions mutation on this candidate")
pathlib.Path("src/refund-agent/.agent_configs/baseline/instructions.md").write_text(ins.strip() + "\n", encoding="utf-8")
print("wrote instructions.md")
'@ | python - $JOB $CAND
git diff --stat
```

If the winner also mutates `tools` or `skills`, copy those values into
`.agent_configs/baseline/tools.json` / `skills/<name>/SKILL.md` by hand (inspect with the `candidates` output).

Review the diff (a human review is part of the loop), then:

```powershell
git add src/refund-agent/.agent_configs/baseline/instructions.md
git commit -m "Optimizer candidate_1: check refund status before submitting"
git push -u origin optimize/candidate-1
gh pr create -R $REPO --base main --head optimize/candidate-1 `
  --title "Optimizer candidate_1" --body "Job $JOB, candidate $CAND. Gate compares against prod on the held-out release set."
```

Expected: PR URL; the **release-gate** check starts (path filter `src/**`, `release/**`,
`data/release.jsonl`, the workflow file).

---

## 9. CI identity + repo setup (can be done any time before step 8)

**Goal:** GitHub Actions logs in to Azure with OIDC (no secrets) and can deploy/route the agent.

### 9.1 App registration + federated credentials

GitHub now issues **immutable-ID subjects**: `repo:<owner>@<ownerId>/<repo>@<repoId>:...`.
The old `repo:<owner>/<repo>:...` form fails with **AADSTS700213**.

```powershell
Assert-Tenant
$OWNER, $NAME = $REPO.Split('/')
$ids = gh api "repos/$REPO" --jq '.owner.id,.id'; $OWNER_ID = $ids[0]; $REPO_ID = $ids[1]
$PREFIX = "repo:$OWNER@$OWNER_ID/$NAME@$REPO_ID"          # e.g. repo:anihitk07@4090037/foundry-closed-loop-agent@1456702956

$APP_ID = az ad app create --display-name "gh-$NAME" --query appId -o tsv
az ad sp create --id $APP_ID -o none
$SP_OID = az ad sp show --id $APP_ID --query id -o tsv

foreach ($fc in @(@("gh-pull-request", "${PREFIX}:pull_request"),
                  @("gh-main",         "${PREFIX}:ref:refs/heads/main"),
                  @("gh-production",   "${PREFIX}:environment:production"))) {
  @{ name = $fc[0]; issuer = "https://token.actions.githubusercontent.com"; subject = $fc[1]
     audiences = @("api://AzureADTokenExchange") } | ConvertTo-Json | Set-Content fic.json -Encoding ascii
  az ad app federated-credential create --id $APP_ID --parameters "@fic.json" -o none
}
Remove-Item fic.json
az ad app federated-credential list --id $APP_ID --query "[].subject" -o tsv
```

Expected: three subjects starting with `repo:<owner>@<ownerId>/<repo>@<repoId>:`.

### 9.2 Roles for the CI service principal

```powershell
az role assignment create --assignee-object-id $SP_OID --assignee-principal-type ServicePrincipal `
  --role 53ca6127-db72-4b80-b1b0-d745d6d5456d --scope $PROJECT_ID -o none     # Foundry User on the project
az role assignment create --assignee-object-id $SP_OID --assignee-principal-type ServicePrincipal `
  --role acdd72a7-3385-48ef-bd42-f606fba81ae7 --scope $ACCOUNT_ID -o none     # Reader on the account
```

### 9.3 Repo variables, `production` environment, token scope

```powershell
$vars = @{ AZURE_CLIENT_ID = $APP_ID; AZURE_TENANT_ID = $TENANT; AZURE_SUBSCRIPTION_ID = $SUB
           AZURE_LOCATION = $LOC; AZURE_RESOURCE_GROUP = $RG          # the Foundry account RG, NOT $OBS_RG
           AZURE_AI_ACCOUNT_NAME = $ACCOUNT; AZURE_AI_PROJECT_NAME = $PROJECT
           FOUNDRY_PROJECT_ENDPOINT = $env:FOUNDRY_PROJECT_ENDPOINT }
$vars.GetEnumerator() | ForEach-Object { gh variable set $_.Key -b $_.Value -R $REPO }
gh variable list -R $REPO

# production environment with you as required reviewer (recommended; promote.yml runs in it)
$GH_UID = gh api user --jq .id
"{`"reviewers`":[{`"type`":`"User`",`"id`":$GH_UID}]}" | gh api -X PUT "repos/$REPO/environments/production" --input -

# pushing .github/workflows/* needs the workflow scope
gh auth refresh -h github.com -s workflow
```

Expected: 8 variables listed; `gh api repos/$REPO/environments --jq '.environments[].name'` prints `production`.

Smoke-test OIDC without a PR: `gh workflow run release-gate.yml -R $REPO --ref main` (this deploys a new
unrouted version and compares; on the weak baseline it should **FAIL** at < 0.9 — that's correct).

---

## 10. Watch the release gate

**What it does** (`release-gate.yml` → `release/release_gate.py`): OIDC login → install azd + extensions →
`azd env` bound to your project → `prepare` (pin current prod, output `prod_version`) →
`azd deploy refund-release-loop` (new **unrouted** version) → `compare <prod>`: both versions run the
held-out `data/release.jsonl` through version-pinned sessions, `GATE_REPEAT=2` (12 cases each).

**PASS requires all of:** candidate pass rate ≥ `0.9` (`GATE_MIN_PASS_RATE`), ≥ prod, 0 infra errors,
candidate p50 latency ≤ `1.5×` prod (`GATE_MAX_LATENCY_RATIO`).

```powershell
gh pr checks -R $REPO --watch
$RUN = gh run list -R $REPO --workflow release-gate.yml -L 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN -R $REPO --exit-status
gh run view $RUN -R $REPO --log | Select-String "Release gate:|pass rate|❌"
gh run download $RUN -R $REPO -n gate-report -D gate-art
(Get-Content gate-art\gate-report.json -Raw | ConvertFrom-Json).checks
gh run view $RUN -R $REPO --web          # the markdown table is the job's step summary
```

Expected (reference run, iteration 1 — **FAIL**, candidate v3):

```
## Release gate: v1 (prod) vs v3 (candidate) -> FAIL
| pass rate | 0.75 | 0.833 |
| candidate pass rate >= 0.9 | ❌ |
| happy_path_release        | ✅ | ❌ |     (unverified_claim: said "processed" while state was pending)
| processor_timeout_release | ✅ | ❌ |     (unverified_claim on outcome_unknown)
```

The LLM judge preferred candidate_1, but the deterministic `refund_checks.py` caught unverified claims.

**On FAIL:** look at the failing cases' flags in `gate-report.json`
(`candidate.results[] | repeated_submission / unverified_claim / claim_state_mismatch`), fix the
instructions on the same branch, push. Each push deploys a **new** candidate version and re-runs the gate.
The reference review fix appended:

```markdown
After submitting a refund:
- Report exactly the state that `submit_refund` returned. If it is `Pending`, say the refund was submitted and is pending; never say it was processed, completed, issued or refunded unless a tool returned `Completed`.
- If the result is `outcome_unknown` or a timeout, say the outcome is not yet confirmed, do not resubmit, and offer to check status again later.
```

Expected iteration 2 — **PASS**, candidate v4:

```
## Release gate: v1 (prod) vs v4 (candidate) -> PASS
| pass rate       | 0.833 | 1.0  |
| p50 latency (s) | 6.56  | 5.79 |
| served by       | ['1'] | ['4'] |
```

Record the **candidate version** (`v4` above; also `python release/routing.py show` → `latest`).
Merge the PR when green: `gh pr merge -R $REPO --squash --delete-branch`. Merging does **not** route traffic.

---

## 11. Canary

**Goal:** send a growing share of *sessions* to the candidate (Foundry routes an endpoint to one version;
there is no weighted split), while the endpoint keeps serving prod. Stages 5 % → 25 % → 50 % of
`CANARY_REQUESTS` (default 12) validation rows; ABORT (exit 1) if the candidate is worse than prod or errors.

```powershell
gh workflow run promote.yml -R $REPO -f action=canary -f prod_version=1 -f candidate_version=4
$RUN = gh run list -R $REPO --workflow promote.yml -L 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN -R $REPO --exit-status      # approve the 'production' deployment in the UI if reviewers are set
gh run view $RUN -R $REPO --log | Select-String "stage|canary|routed"
```

Expected (reference run):

```
stage   5%: candidate v4 1 req pass=1.0 | prod(endpoint) 10 req pass=0.9 -> HOLD
stage  25%: candidate v4 3 req pass=1.0 | prod(endpoint) 4 req pass=1.0 -> HOLD
stage  50%: candidate v4 6 req pass=1.0 | prod(endpoint) 3 req pass=1.0 -> HOLD
canary healthy at all stages; ready to promote
```

Local equivalent: `python release/canary.py canary 1 4 --stages 5,25,50 --requests 12`.

---

## 12. Promote

```powershell
gh workflow run promote.yml -R $REPO -f action=promote -f prod_version=1 -f candidate_version=4
# (prod_version is a required input even for promote)
$RUN = gh run list -R $REPO --workflow promote.yml -L 1 --json databaseId --jq '.[0].databaseId'
gh run watch $RUN -R $REPO --exit-status
python release/routing.py show
```

Expected: log line `promote: endpoint now routes 100% to v4`; `show` → `"latest": "4", "routed_version": "4"`.
Local equivalent: `python release/canary.py promote 4`.

---

## 13. Rollback drill

```powershell
gh workflow run promote.yml -R $REPO -f action=rollback -f prod_version=1
# wait for it, then:
python release/routing.py show                      # routed_version: "1"
python release/routing.py invoke endpoint "Is my refund for ORD-1006 done yet?"
# restore the promoted version
gh workflow run promote.yml -R $REPO -f action=promote -f prod_version=1 -f candidate_version=4
```

Expected: `rollback: endpoint now routes 100% to v1`, then `promote: endpoint now routes 100% to v4`.
Rollback is instant re-pinning; all versions stay deployed. Local: `python release/canary.py rollback 1`.

---

## 14. Verify (checklist)

```powershell
Assert-Tenant
python release/routing.py show                                         # latest = routed = promoted version
python release/suite.py endpoint data/release.jsonl                    # pass_rate 1.0 expected on v4
python release/routing.py invoke endpoint "Where is my ORD-1006 refund? Just resubmit it if it's stuck."
azd ai eval dataset list                                               # 4 refund-release-loop-* datasets
azd ai eval run list --eval refund-train-eval                          # baseline run present
az monitor app-insights query --app $APPI -g $OBS_RG --analytics-query `
  "union requests, dependencies | where timestamp > ago(1d) and name has 'invoke_agent' | summarize count() by name" -o table
@'
import os
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
p = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=AzureCliCredential())
r = p.evaluation_rules.get("refund-release-loop-trace-eval")
print(r.id, r.event_type, r.enabled)
'@ | python -
gh run list -R $REPO -L 10                                              # gate fail/pass, canary, promote, rollback
az ad app federated-credential list --id $APP_ID --query "[].subject" -o tsv
az role assignment list --assignee $SP_OID --all --query "[].{role:roleDefinitionName,scope:scope}" -o table
```

| check | pass when |
|---|---|
| routing | `routed_version` = promoted version |
| quality | release set pass_rate ≥ 0.9, no `repeated_submission` on `pending_status_release` |
| traces | `invoke_agent refund-release-loop:<v>` rows for each version you exercised |
| monitoring | rule `refund-release-loop-trace-eval` has a timer-trigger event type and `enabled` = True |
| CI | one failing and one passing release-gate run; successful canary + promote runs |
| identity | 3 immutable-ID subjects; SP has Foundry User (project) + Reader (account) |

---

## 15. Teardown

Everything this runbook created, in dependency order. Run the preamble and `Assert-Tenant` first.

```powershell
Assert-Tenant

# 1. Foundry objects (rule, evals, datasets, optimizer job, agent with ALL versions incl. drafts)
@'
import os, sys
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
p = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=AzureCliCredential())
p.evaluation_rules.delete("refund-release-loop-trace-eval"); print("deleted rule")
oai = p.get_openai_client()
for e in oai.evals.list(limit=100):
    if e.name == "refund-release-loop-production-traces":
        oai.evals.delete(e.id); print("deleted eval", e.id)
for n in ("train", "validation", "release", "traces"):
    name = f"refund-release-loop-{n}"
    for d in p.datasets.list_versions(name=name):
        p.datasets.delete(name=name, version=d.version); print("deleted dataset", name, d.version)
for job in sys.argv[1:]:
    p.agents.delete_optimization_job(job); print("deleted optimization job", job)
'@ | python - $JOB
azd ai eval list                                            # delete every eval created by `azd ai eval create`
azd ai eval delete refund-train-eval --force --no-prompt
azd ai agent delete refund-release-loop --force --no-prompt

# 2. Project connection
az rest --method delete --url "https://management.azure.com$PROJECT_ID/connections/${APPI}?api-version=2025-06-01"

# 3. Role assignments at project/account scope (those on $OBS_RG go with the RG)
az role assignment delete --assignee $SP_OID  --scope $PROJECT_ID
az role assignment delete --assignee $SP_OID  --scope $ACCOUNT_ID
az role assignment delete --assignee $PROJ_MI --scope $PROJECT_ID --role 53ca6127-db72-4b80-b1b0-d745d6d5456d
az role assignment delete --assignee $ME      --scope $PROJECT_ID --role 53ca6127-db72-4b80-b1b0-d745d6d5456d   # only if you added it

# 4. CI identity (removes the SP and its federated credentials)
az ad app delete --id $APP_ID

# 5. Observability RG: Log Analytics, App Insights, MI role assignments on it
az group delete -n $OBS_RG --yes

# 6. GitHub
gh api -X DELETE "repos/$REPO/environments/production"
"AZURE_CLIENT_ID","AZURE_TENANT_ID","AZURE_SUBSCRIPTION_ID","AZURE_LOCATION","AZURE_RESOURCE_GROUP",
"AZURE_AI_ACCOUNT_NAME","AZURE_AI_PROJECT_NAME","FOUNDRY_PROJECT_ENDPOINT" | ForEach-Object { gh variable delete $_ -R $REPO }
# or drop the whole repo (needs: gh auth refresh -s delete_repo)
# gh repo delete $REPO --yes

# 7. Only if you created them in 2.1: models, project, account (+ purge the soft-deleted account)
# az cognitiveservices account deployment delete -n $ACCOUNT -g $RG --deployment-name gpt-5.4-mini
# az cognitiveservices account deployment delete -n $ACCOUNT -g $RG --deployment-name gpt-5.4
# az cognitiveservices account delete -n $ACCOUNT -g $RG
# az cognitiveservices account purge -n $ACCOUNT -g $RG -l $LOC
# az group delete -n $RG --yes

# 8. Local state
Remove-Item -Recurse -Force .azure, .venv, gate-art, src\refund-agent\.venv -ErrorAction SilentlyContinue
Remove-Item release\gate-report.json, release\traffic-*.json -ErrorAction SilentlyContinue
```

Verify: `az group exists -n $OBS_RG` → `false`; `az ad app show --id $APP_ID` → not found;
`python release/routing.py show` → 404 error for the agent.

---

## 16. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `azure/login` fails with **AADSTS700213** (no matching federated identity) | GitHub sends immutable-ID subjects `repo:<owner>@<ownerId>/<repo>@<repoId>:...` | Recreate the 3 credentials with step 9.1's `$PREFIX`; the error text shows the exact subject GitHub presented |
| `refusing to allow ... to create or update workflow ... without workflow scope` on `git push` | token lacks `workflow` scope, or a `GH_TOKEN` env var overrides the keyring token | `gh auth refresh -h github.com -s workflow`; `Remove-Item Env:GH_TOKEN`; last resort: upload the file with the Contents API (`gh api -X PUT repos/$REPO/contents/<path>`) |
| Continuous eval rule: *agent of kind 'hosted' not supported for evaluation rules* | `responseCompleted` rules don't support hosted agents | Use `release/monitor.py` (timerTrigger + traceEvaluation) |
| Scheduled trace-eval runs end `failed`, 0 traces, empty log | preview limitation (also: RBAC not yet propagated) | Check step 2.4 roles, wait 2–3 min; use `register_datasets.py --from-traces` to feed traces into the loop |
| No `invoke_agent` spans in App Insights | connection created after the deploy, or wrong App Insights | Create the connection (2.3), then `azd deploy` a new version |
| `--from-traces` yields 0 samples | no **v1** traces in the last **6 h** (hard-coded) | Re-run step 4 traffic against v1, then re-run step 5 |
| `register_datasets.py` conflict on re-run | dataset version `1` already exists | Delete the versions (teardown snippet) or set `DATASET_VERSION`; note `optimize.py` reads version `1` |
| Eval row error *Tool definitions input is required* | `builtin.tool_call_accuracy` can't see hosted-agent tools | Keep it out of `azure.eval.yaml`; the gate uses deterministic `refund_checks.py` |
| `azd ai eval run start`: eval not found | eval not deployed | `azd ai eval create refund-train-eval` (re-running creates a **new** eval id) |
| `azd ai agent optimize`: *agent_optimization_space is required* / *registered datasets require name+version* | CLI path doesn't send hosted-agent config | Use `python release/optimize.py submit` |
| Optimizer: *no_optimizable_target* | baseline config not sent | `optimize.py` sends it from `.agent_configs/baseline` + `skills/`; keep those files present and in sync with the deployed version |
| Scripts act on `admin-0456` / 401 on someone else's project | `FOUNDRY_PROJECT_ENDPOINT` unset (scripts default to the reference project) | Run the session preamble |
| `azd deploy` targets the wrong project | `azure.yaml` `ai-project.endpoint` still the reference value | Step 1.2 |
| `401`/`403` from `routing.py`, `suite.py`, evals | missing Foundry User on the project, or logged in to another tenant | Step 2.4 / 9.2; check `AZURE_CONFIG_DIR` and `Assert-Tenant` |
| CI `azd deploy` denied | SP lacks a data-plane write permission in your tenant | Confirm Foundry User on the **project** scope; if still denied add *Azure AI Developer* on the project |
| `version N failed` / `TimeoutError` in `wait-active` | container failed to start (e.g. missing env, bad deps) | `azd ai agent monitor` for logs; fix and redeploy (new version) |
| `candidate version == prod version; nothing was deployed` | deploy step skipped/failed, or another run deployed first | Check the deploy step log; runs are serialized by `concurrency: foundry-release` |
| Gate shows `served by ['1']` for the candidate | old `suite.py`: `agent_reference.version` echoes the **endpoint's** routed version | Use current `suite.py` (reports the session's `version_indicator`); traces show the true version |
| Gate FAIL: p50 latency check ❌ | candidate slower than 1.5× prod | Simplify instructions/tools, or set `GATE_MAX_LATENCY_RATIO` in the workflow env if justified |
| Canary prints `ABORT`, run fails | candidate worse than prod at a stage | Endpoint untouched (still on prod); inspect log, fix, new PR |
| promote/canary run stuck *Waiting* | `production` environment requires approval | Approve in the run page (`gh run view $RUN -R $REPO --web`) |
| PR shows no `release-gate` check | Actions disabled on the fork, or no file under the path filter changed | Step 1.1 Actions enable; or `gh workflow run release-gate.yml -R $REPO --ref <branch>` |
| `gh pr create` opened the PR on the upstream repo | fork default | `gh repo set-default $REPO`, always pass `-R $REPO` |
| `UnicodeEncodeError` printing ✅/❌ | console code page | `$env:PYTHONIOENCODING = "utf-8"` |
| Wrong tenant/subscription used | shared `~/.azure` across tenants | Per-tenant `AZURE_CONFIG_DIR` / `AZD_CONFIG_DIR`; `Assert-Tenant` before destructive ops |
