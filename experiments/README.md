# AgentCommons Experiments

This directory contains the service-composition experiments for the AgentCommons
paper. The runner supports both deterministic offline simulation and a real
OpenAI-compatible LLM selector. The current paper tables use the LLM selector
with weak-gold API-selection labels.

## Main Experiment

The main experiment evaluates AgentCommons in an API-based service composition
setting. This is a representative tool-using agent scenario: an agent receives a
user request, decomposes it into subtasks, selects APIs for each subtask, binds
inputs, and recovers from failures such as wrong tool selection, missing inputs,
interface mismatch, unavailable capabilities, and unreliable historical traces.

Although AgentCommons is a general experience-sharing layer, this dataset gives
us a concrete service-composition testbed.

## Methods

The runner compares four methods:

| Method | Description |
| --- | --- |
| `no_experience` | Selects from candidates without shared experience. |
| `raw_trace` | Retrieves prior raw execution traces and uses them as noisy guidance. |
| `summary` | Retrieves concise natural-language summaries of prior experiences. |
| `agentcommons` | Publishes and retrieves structured Agent Experience Posts. |

## Metrics

The runner reports:

| Metric | Meaning |
| --- | --- |
| `step_success_rate` | Fraction of subtasks where the selected API matches the gold API. |
| `workflow_success_rate` | Fraction of user tasks where all subtasks succeed. |
| `repair_success_rate` | Fraction of initial top-1 failures repaired by the method. |
| `repeated_failure_rate` | Fraction of initial failures repeated despite relevant prior experience. |
| `experience_hit_rate` | Fraction of subtasks with retrieved prior experience that is applicable to the current candidate APIs. |
| `avg_cost_proxy` | Lightweight context/candidate/experience cost proxy. |

## Run

From the repository root:

```powershell
python experiments\run_service_composition_experiment.py
```

For the full dataset:

```powershell
python experiments\run_service_composition_experiment.py --limit 0
```

For a failure-focused evaluation split that only evaluates tasks where the
initial top-1 candidate makes at least one mistake:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 500 `
  --output-dir experiments\runs\service_composition_failure_focus `
  --eval-filter initial_failure_tasks
```

Useful options:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 500 `
  --build-ratio 0.30 `
  --seed 13 `
  --max-memory 8 `
  --eval-filter all
```

## Outputs

By default, outputs are written to:

```text
experiments/runs/service_composition_main/
```

The directory contains:

| File | Description |
| --- | --- |
| `summary.csv` | Main table metrics for each method. |
| `summary.json` | JSON version of the same metrics. |
| `step_results.csv` | Per-step predictions and outcomes. |
| `main_results_table.tex` | LaTeX tabular snippet for the paper. |
| `metadata.json` | Dataset split and run configuration. |
| `agentcommons_store.json` | AgentCommons experience store produced from the build split. |

## LLM Configuration

The default runner can be used in deterministic offline mode without an LLM API
key. To reproduce the real-LLM experiments used in the paper, copy
`.env.example` to `.env` and fill the model settings:

```powershell
Copy-Item .env.example .env
```

The expected `.env` format is:

```text
EXPERIMENT_AGENT=offline
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
```

To run a tiny LLM smoke test without changing `.env`:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 6 `
  --output-dir experiments\runs\llm_smoke `
  --agent-mode llm `
  --llm-max-eval-tasks 1 `
  --methods no_experience
```

To compare all methods with an LLM selector, keep the evaluation cap small at
first because each method calls the model for each evaluated subtask:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 60 `
  --output-dir experiments\runs\llm_small `
  --agent-mode llm `
  --llm-max-eval-tasks 5
```

The LLM mode writes `llm_cache.json` into the output directory so repeated runs
reuse previous model responses.

The 211-task extended real-LLM run used in the current main table was produced
with:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 0 `
  --output-dir experiments\runs\llm_211_main `
  --agent-mode llm `
  --llm-max-eval-tasks 211 `
  --methods no_experience,raw_trace,summary,agentcommons `
  --seed 13 `
  --build-ratio 0.30 `
  --build-source-limit 100 `
  --preserve-eval-step-results experiments\runs\llm_50_main\step_results.csv `
  --eval-filter all
```

The earlier 50-task real-LLM runs and failure-focused analysis were produced
with:

```powershell
python experiments\run_service_composition_experiment.py `
  --limit 100 `
  --output-dir experiments\runs\llm_50_main `
  --agent-mode llm `
  --llm-max-eval-tasks 50 `
  --seed 13 `
  --build-ratio 0.30 `
  --eval-filter all

python experiments\run_service_composition_experiment.py `
  --limit 0 `
  --output-dir experiments\runs\llm_50_failure_focus `
  --agent-mode llm `
  --llm-max-eval-tasks 50 `
  --seed 13 `
  --build-ratio 0.30 `
  --eval-filter initial_failure_tasks
```
