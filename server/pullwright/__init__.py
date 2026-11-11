"""Load server/.env into os.environ (no override) so keys never have to be exported by hand."""
import os
import re
from pathlib import Path

_INLINE_COMMENT = re.compile(r"\s+#.*$")


def parse_env(text: str) -> dict[str, str]:
    """KEY=value lines. Quoted values are taken verbatim; unquoted ones lose a trailing ` # comment`."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        else:
            v = _INLINE_COMMENT.sub("", v)
        out[k.strip()] = v
    return out


_env = Path(__file__).resolve().parent.parent / ".env"
if _env.is_file():
    for _k, _v in parse_env(_env.read_text(encoding="utf-8")).items():
        os.environ.setdefault(_k, _v)
