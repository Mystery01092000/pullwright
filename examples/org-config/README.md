# Sample shared config (Claude Code layout)

A starting point for a shared skills, agents and hooks repository ([docs/org-config.md](../../docs/org-config.md)). Copy this folder into its own repository, or point Pullwright at it directly while you try it out:

```bash
PULLWRIGHT_ORG_CONFIG_DIR=/path/to/pullwright/examples/org-config uv run pullwright-serve
```

| File | What it does in every run |
|---|---|
| `CLAUDE.md` | Organisation instructions added to every agent's prompt |
| `.claude/agents/security-reviewer.md` | A specialist the supervisor can hand security reviews to (`spawn_specialist`) |
| `.claude/skills/commit-messages/SKILL.md` | A skill any agent can load |
| `.claude/settings.json` + `.claude/hooks/block_secrets.py` | A PreToolUse hook that blocks shell commands that would print secrets (exit code 2) |
| `.claude/guardrails.json` | Deny patterns added to every repository's list: pushing to main, piping curl into a shell |

The same folder works in Claude Code.
