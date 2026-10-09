"""The validation journeys' world — read from the environment / .env.

« rien en dur, tout variabilisé, qui se base sur le .env » (operator,
2026-10-09). The same contract as scripts/config.sh, in Python: a deployer
edits `.env`, never these files. Every value has a default that works on a
plain clone.
"""
import os
from pathlib import Path

_RACINE = Path(__file__).resolve().parents[2]

# Read .env the way docker-compose does — KEY=VALUE, ignore comments.
_env = _RACINE / ".env"
if _env.exists():
    for ligne in _env.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if not ligne or ligne.startswith("#") or "=" not in ligne:
            continue
        cle, val = ligne.split("=", 1)
        if cle.startswith("DGX_") and cle not in os.environ:
            os.environ[cle] = val.strip()

RACINE = Path(os.environ.get("DGX_ROOT", _RACINE))
WEB = os.environ.get("DGX_WEB", "https://example.org")
# The bare hostname (no scheme): cookies and assertions speak of a domain.
DOMAINE = WEB.split("//", 1)[-1].split("/", 1)[0]
# The identity provider the SSO redirects to — the journey checks the
# redirect, never the provider itself.
DOMAINE_SSO = os.environ.get("DGX_SSO_DOMAIN", "gjallarhorn.dgx.website")
API = os.environ.get("DGX_API", "https://api.example.org")
S3_CFG = os.environ.get("DGX_S3_CFG", str(RACINE / "secrets/s3-garage.cfg"))
S3_BUCKET = os.environ.get("DGX_S3_BUCKET", "s3://dgx/backups")
DEMO_USER = os.environ.get("DGX_DEMO_USER", "demo")
DEMO_PASS = os.environ.get("DGX_DEMO_PASS", "/root/shots/demo-credentials")
PYTEST_BIN = os.environ.get("DGX_PYTEST_BIN", "/root/shots-venv/bin/python")
PY
