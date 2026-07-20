"""Entrypoint shim: load the upstream pr-agent github_app FastAPI app, mount the
status page router onto it, then serve with gunicorn.

This exists so we can add /status without editing upstream pr-agent source.
The compose file / Unraid template overrides the container CMD to run this.

Run:
    python -m gunicorn -k uvicorn.workers.UvicornWorker \
      -c pr_agent/servers/gunicorn_config.py --forwarded-allow-ips "*" \
      pr_agent.status_app:app
"""

import sys

from pr_agent.servers.github_app import app  # triggers upstream module-level setup
from pr_agent.config_loader import get_settings
from pr_agent.status_page import mount

# --- Startup guard: refuse to boot if the webhook secret is missing. ----------
# Upstream's get_body() only calls verify_signature() inside `if webhook_secret:`,
# so an empty/missing secret silently leaves POST /api/v1/github_webhooks
# UNAUTHENTICATED. For a public-tunnel deployment that's a budget-drain / abuse
# vector (anyone can POST forged payloads that trigger Ollama Cloud calls and
# post reviews). Fail fast at boot instead.
_s = get_settings()
if str(_s.github.deployment_type).lower() == "app":
    _secret = getattr(_s.github, "webhook_secret", None)
    if not _secret or str(_secret).startswith("REPLACE_WITH"):
        sys.stderr.write(
            "FATAL: github.webhook_secret is not configured. Set it in your "
            ".secrets.toml (see secrets.example.toml) or via the GITHUB__WEBHOOK_SECRET "
            "env var. Refusing to start with an unauthenticated webhook endpoint.\n"
        )
        sys.exit(1)

mount(app)

# gunicorn loads `app` from this module's namespace.
__all__ = ["app"]