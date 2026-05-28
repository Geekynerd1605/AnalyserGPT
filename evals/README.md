# Evaluation Layer (Phase A)

This folder contains a lightweight local evaluation runner for AnalyserGPT.

## What Phase A includes

- Fixed benchmark cases (`evals/cases/phase_a_cases.json`)
- Per-case run artifacts (`eval_runs/<timestamp>/cases/*.json`)
- Basic metrics:
  - `task_completion`
  - `execution_success`
  - `code_generated`
  - `chart_generated`
- Aggregated reports:
  - `summary.json`
  - `summary.md`

## Run evaluations

From the repository root:

```bash
python3 evals/runner.py
```

Run only first N cases:

```bash
python3 evals/runner.py --max-cases 1
```

Use a different cases file:

```bash
python3 evals/runner.py --cases-file evals/cases/phase_a_cases.json
```

## Notes

- Cases automatically copy the specified CSV into `tmp/data.csv` before each run.
- Each case is run with a fresh AutoGen team but shared Docker executor for speed.
- You need a valid `OPENAI_API_KEY` and Docker running.
