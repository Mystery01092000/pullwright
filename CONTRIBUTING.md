# Contributing to Pullwright

Thanks for helping. Pullwright is built so that most work fits in one small pull request: a channel, a runner backend, a check detector, an agent prompt, a doc. Issues labeled [`good first issue`](https://github.com/Mystery01092000/pullwright/labels/good%20first%20issue) are scoped for a first contribution.

## Before you start

- For anything larger than a bug fix, open an issue first and describe the change. It saves you building something that does not fit.
- Comment on an issue to say you are taking it, so two people do not do the same work.
- Read [docs/architecture.md](docs/architecture.md). It explains how chat, planning, the queue and runners fit together.

## Development setup

```bash
# backend (Python 3.13, uv)
cd server
uv sync
uv run ruff check .
uv run pytest -q          # no API key or network needed: tests use a scripted fake model and a fake GitHub

# web UI (Node 22)
cd web
npm ci
npm run dev               # http://localhost:5173, proxies /api to :8765
npm run build
```

Running the whole control plane locally is described in [docs/getting-started.md](docs/getting-started.md).

## What a good pull request looks like

- **One concern.** A fix, a feature or a refactor; not all three.
- **Tests that pin behaviour.** Add a test that fails without your change. The scripted model (`tests/fake_client.py`) and fake GitHub (`tests/fake_github.py`) let you test whole flows without network access.
- **Docs in the same PR.** A new setting goes in `.env.example` and `docs/configuration.md`; a behaviour change goes in the relevant doc.
- **Measured prompt and model changes.** If you change an agent prompt in `server/pullwright/defaults/agents/` or a model default, run the evals (`cd server && uv run python -m evals.run`, which spends real API money) and put the before/after pass rate and cost in the PR.
- **Conventional commit titles**: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- `ruff check` and `pytest` pass, and `npm run build` passes if you touched the UI. CI runs all three, plus a build of the runner image.

## Design rules worth knowing

- **A model never decides whether checks pass.** Commands do (`server/pullwright/checks.py`). Agents decide what to change.
- **Planning is read-only.** The describer and verifier get `read_file`, `glob` and `grep` only, and nothing from the repository executes in the control plane.
- **Runner agents cannot push or call `gh`.** The pipeline pushes and opens the PR itself.
- **Every model call is budgeted and traced.** New agent work goes through `Session` (`server/pullwright/session.py`), so the dollar caps and the event log apply.
- **Secrets never go on a command line or into a log.** Tokens are passed by environment variable name, and clone URLs are redacted in errors.

## Reporting security issues

Do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).
