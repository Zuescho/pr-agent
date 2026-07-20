"""Entrypoint shim: load the upstream pr-agent github_app FastAPI app and serve
it with gunicorn, WITHOUT mounting the status page.

A minimal webhook-only image: the upstream github_app exposes the HMAC-guarded
POST /api/v1/github_webhooks and the GET / healthcheck. No /status route, so
nothing about in-flight reviews or repo activity is exposed — suitable for
putting the port directly behind a public tunnel (Cloudflare Tunnel /
Tailscale Funnel) without an extra auth layer on the status page.

This file exists to preserve the webhook-secret boot guard (below) without
editing upstream pr-agent source. The guard runs at import time; gunicorn
loads `app` from this module's namespace.

Run (as the container CMD):
    python -m gunicorn -k uvicorn.workers.UvicornWorker \
      -c pr_agent/servers/gunicorn_config.py \
      --forwarded-allow-ips 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.1 \
      pr_agent.webhook_app:app
"""

import sys

from pr_agent.servers.github_app import app  # triggers upstream module-level setup
from pr_agent.config_loader import get_settings

# --- Startup guard: refuse to boot if the webhook secret is missing. ----------
# Upstream's get_body() only calls verify_signature() inside `if webhook_secret:`,
# so an empty/missing/whitespace-only secret silently leaves
# POST /api/v1/github_webhooks UNAUTHENTICATED. For a public-tunnel deployment
# that's a budget-drain / abuse vector (anyone can POST forged payloads that
# trigger LLM calls and post reviews). Fail fast at boot instead.
#
# This guard runs unconditionally for this image — the image exists solely to
# serve the webhook, so deployment_type is not a meaningful gate here. (The
# upstream 'user' default would otherwise let a misconfigured/missing
# .secrets.toml boot unauthenticated behind the public tunnel.)
_s = get_settings()
_secret = getattr(_s.github, "webhook_secret", None)
if (not _secret) or (not str(_secret).strip()) or str(_secret).strip().startswith("REPLACE_WITH"):
    sys.stderr.write(
        "FATAL: github.webhook_secret is not configured (or is blank/placeholder). "
        "Set it in your .secrets.toml (see secrets.example.toml) or via the "
        "GITHUB__WEBHOOK_SECRET env var. Refusing to start — an unauthenticated "
        "webhook endpoint would let anyone trigger LLM calls.\n"
    )
    sys.exit(1)

# gunicorn loads `app` from this module's namespace.
__all__ = ["app"]