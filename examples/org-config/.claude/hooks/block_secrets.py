"""PreToolUse hook: refuse shell commands that would print secrets. Exit code 2 blocks the call (Claude Code's
convention, which Pullwright follows for hooks from settings.json); the message on stderr goes back to the agent."""
import json
import re
import sys

PATTERNS = [
    r"\bcat\s+[^|;&]*\.env\b",  # cat .env, cat server/.env
    r"\b(printenv|env)\s*$",  # dumping the whole environment
    r"\becho\s+\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD)\w*",  # echo $API_KEY
    r"\.pem\b",  # private keys
]

event = json.load(sys.stdin)
command = (event.get("tool_input") or {}).get("command", "")
if event.get("tool_name") == "bash" or event.get("tool_name") == "Bash":
    for p in PATTERNS:
        if re.search(p, command, re.IGNORECASE):
            print(f"Blocked by the organisation's secrets policy: `{command[:120]}` would print a secret.", file=sys.stderr)
            sys.exit(2)
sys.exit(0)
