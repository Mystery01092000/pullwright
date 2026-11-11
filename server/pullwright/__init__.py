"""Load server/.env into os.environ (no override) so the API key never has to be exported by hand."""
import os
from pathlib import Path

_env = Path(__file__).resolve().parent.parent / ".env"
if _env.is_file():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
