# Evals

`server/evals/` runs real tasks on throwaway repositories against the real API and grades them with commands. Use it whenever you change a prompt or a model, so the change is measured rather than guessed at.

```bash
cd server
uv run python -m evals.run                                   # all cases
uv run python -m evals.run python-bugfix                     # one case
uv run python -m evals.run --worker-model claude-haiku-5-5 --max-cost 1
uv run python -m evals.run --pipeline local                   # the local-mode lifecycle instead
```

The default `--pipeline team` measures what an approved issue goes through: describer + verifier plan (auto-approved), then the runner loop (Sonnet supervisor, Haiku workers, the case's checks as the project checks, independent review), with plan and work cost reported separately.

**Every eval run spends real money.** Set a cap with `--max-cost`. For scale: `python-subtract` on the team pipeline passed for $0.072 ($0.042 plan, $0.030 work) in 60 seconds on 2026-11-11.

## A case

`evals/cases/<name>.json` holds:
- the task;
- fixture files that make up the starting repository;
- shell checks (an exit code and, optionally, expected stdout);
- a dollar cap.

The output is a markdown table of pass/fail, checks, cost and time per case, plus a JSON file under `evals/results/`.

The shipped cases:

| Case | What it checks |
|---|---|
| `python-subtract` | a small Python feature with tests |
| `python-bugfix` | a bug fix that must not modify the existing test |
| `go-todo-api` | a Go project built from scratch, with tests and a Makefile |

## Planned

- **Planning evals:** did the describer name the files that matter, and does the verifier catch a planted error in a plan?
- **Issue evals:** run the work pipeline from a fixture issue and score pass rate, dollars and minutes per issue.

See the [roadmap](roadmap.md).
