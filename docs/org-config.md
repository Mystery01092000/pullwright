# Organisation config: shared agents, skills and hooks

An organisation can keep its agents, skills, hooks, instructions and guardrails in one repository. Every Pullwright run then uses them, on every repository it works on.

```bash
PULLWRIGHT_ORG_CONFIG=acme/agent-config                  # its default branch
PULLWRIGHT_ORG_CONFIG=acme/agent-config@v3               # a tag, branch or commit (recommended: a tag)
PULLWRIGHT_ORG_CONFIG=acme/agent-config@main:pullwright  # a folder inside the repository
```

Install the Pullwright GitHub App on that repository too. Pullwright reads it with a token limited to `contents: read`.

## What the repository can hold

Either Pullwright's layout or Claude Code's. A repository you already use with Claude Code works as it is.

| Pullwright layout | Claude Code layout | Becomes |
|---|---|---|
| `.pullwright/agents/<name>.md` | `.claude/agents/<name>.md` | A role override (`describer`, `verifier`, `supervisor`, `worker`, `diagnosis`, `summarizer`, `intake`) or, under any other name, a **specialist** |
| `.pullwright/skills/<name>/SKILL.md` | `.claude/skills/<name>/SKILL.md` | A skill every agent can load |
| `.pullwright/hooks.json` | `.claude/settings.json` (`hooks.PreToolUse` / `PostToolUse`) | Hooks around every tool call in the runner |
| `instructions.md` | `CLAUDE.md` or `AGENTS.md` | "Organisation instructions" in every agent's prompt |
| `guardrails.json` | `guardrails.json` | Deny / ask / allow command lists |

Claude Code tool names (`Read`, `Edit`, `Bash`, `Grep`, ...) and model aliases (`sonnet`, `haiku`, `opus`, `inherit`) are mapped to Pullwright's tools and models. Claude Code hooks keep their convention: exit code 2 blocks the tool call, and any other exit code is a non-blocking error. Hook input carries both Pullwright's fields (`tool`, `input`) and Claude Code's (`tool_name`, `tool_input`, `hook_event_name`, `cwd`). Hooks get `PULLWRIGHT_CONFIG_DIR` (the config folder, so a hook can call `$PULLWRIGHT_CONFIG_DIR/hooks/check.sh`) and `CLAUDE_PROJECT_DIR` (the checkout). Claude Code events with no Pullwright equivalent (`SessionStart`, `Notification`, ...) are ignored.

## Layering

| | Built-in defaults | Organisation | Repository (`.pullwright/`) |
|---|---|---|---|
| Agents, skills | base | override defaults | override the organisation |
| Instructions | — | first | after the organisation's |
| Deny / ask / allow lists | — | combined with the repository's | combined; cannot remove an organisation entry |
| Hooks | — | run first | run after |
| `bash_timeout` | 120 | the stricter of the two | the stricter of the two |

## Specialists

An agent whose name isn't a Pullwright role becomes a specialist, for example `security-reviewer`, `migration-writer` or `docs-writer`. The supervisor sees each specialist's description and can delegate a task with `spawn_specialist(agent, task, context)`. Specialists run inside the runner with their own tools and model, under the same budget, and their cost counts toward implementation.

## Versions and safety

- **One version per task.** The control plane fetches the repository on start and every `PULLWRIGHT_ORG_CONFIG_REFRESH` seconds (default 300), and records the commit. Each runner job fetches exactly that commit, even if the repository changes in the meantime.
- **Fails closed.** If the repository can't be fetched at startup, the control plane doesn't start, because the organisation's deny lists and hooks must be in force before any task runs. A failed refresh keeps the last good version.
- **No hooks on the control plane.** Planning runs read-only and executes no hooks, neither the organisation's nor the repository's. Hooks run only inside the isolated runner.
- **Review it like code.** Hooks are shell commands that run in every runner, so protect the repository with branch protection and required reviews, and pin a tag.

## On a personal account

The shared repository can be on any GitHub account, organisation or personal. If the Pullwright GitHub App is installed there, nothing else is needed. If it isn't (for example, the App belongs to an organisation and the repository is on your personal account), set `PULLWRIGHT_ORG_CONFIG_READ_TOKEN` to a fine-grained token limited to that one repository with **Contents: read-only**. The control plane and the runners then use it instead of an App token.

For local development, `PULLWRIGHT_ORG_CONFIG_DIR=/path/to/folder` uses a folder directly. A ready sample in Claude Code's layout is in [examples/org-config](../examples/org-config): a security-reviewer specialist, a commit-messages skill, a hook that blocks commands printing secrets, and two shared deny rules.
