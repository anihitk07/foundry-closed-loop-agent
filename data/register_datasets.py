"""Step 2-3 of the loop: turn production evidence into versioned datasets.

* Curated splits (train / validation / held-out release) -> Foundry datasets.
* v1 production traces -> Foundry data generation job -> dataset (trace-mined cases).
"""

import datetime as dt
import os
import pathlib
import sys

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (EvaluationDataGenerationJobInputs, EvaluationDataGenerationJobOutputConfiguration,
                                      TracesDataGenerationJobConfiguration, TracesDataGenerationJobSource)
from azure.identity import AzureCliCredential

ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT = os.environ.get("AGENT_NAME", "refund-release-loop")
VERSION = os.environ.get("DATASET_VERSION", "1")
HERE = pathlib.Path(__file__).parent

project = AIProjectClient(endpoint=ENDPOINT, credential=AzureCliCredential())

for split in ("train", "validation", "release"):
    ds = project.datasets.upload_file(name=f"{AGENT}-{split}", version=VERSION, file_path=str(HERE / f"{split}.jsonl"))
    print("dataset:", ds.name, ds.version, ds.id)

if "--from-traces" in sys.argv:
    poller = project.datasets.begin_create_generation_job(
        EvaluationDataGenerationJobInputs(
            name=f"{AGENT}-v1-traces",
            sources=[TracesDataGenerationJobSource(agent_name=AGENT, agent_version="1",
                                                   start_time=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6))],
            generation_configuration=TracesDataGenerationJobConfiguration(max_samples=30, redact_private_content=True),
            output_configuration=EvaluationDataGenerationJobOutputConfiguration(
                name=f"{AGENT}-traces", description="Cases mined from v1 production traces"),
        ),
        headers={"Foundry-Features": "DataGenerationJobs=V1Preview"},
    )
    print("generation job submitted; waiting ...")
    res = poller.result()
    print("generated samples:", res.generated_samples, [o.as_dict() for o in res.outputs or []])
