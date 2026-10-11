"""The validation journeys' world — read from the environment / .env.

Nothing hard-coded: the same contract as scripts/config.sh, in Python. A
deployer edits `.env`, never these files. Every value has a default that works
on a plain clone.
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Read .env the way docker-compose does — KEY=VALUE, ignore comments.
_env = _ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.startswith("DGX_") and key not in os.environ:
            os.environ[key] = val.strip()

ROOT = Path(os.environ.get("DGX_ROOT", _ROOT))
WEB = os.environ.get("DGX_WEB", "https://example.org")
# The bare hostname (no scheme): cookies and assertions speak of a domain.
DOMAIN = WEB.split("//", 1)[-1].split("/", 1)[0]
# Optional: the identity provider's host, to pin the SSO redirect to it. The
# provider itself is configured in Admin → Config; without this variable the
# journey checks the redirect's shape (another host, client_id, redirect_uri).
SSO_DOMAIN = os.environ.get("DGX_SSO_DOMAIN", "")
API = os.environ.get("DGX_API", "https://api.example.org")
S3_CFG = os.environ.get("DGX_S3_CFG", str(ROOT / "secrets/s3-garage.cfg"))
S3_BUCKET = os.environ.get("DGX_S3_BUCKET", "s3://dgx/backups")
DEMO_USER = os.environ.get("DGX_DEMO_USER", "demo")
DEMO_PASS = os.environ.get("DGX_DEMO_PASS", "/root/shots/demo-credentials")
PYTEST_BIN = os.environ.get("DGX_PYTEST_BIN", "/root/shots-venv/bin/python")
