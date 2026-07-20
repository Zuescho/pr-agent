"""Status page for the self-hosted pr-agent.

Adds a GET /status (HTML) and GET /status.json (JSON) route to the running
pr-agent FastAPI app, showing:

  - Bot identity (GitHub App slug, resolved lazily)
  - Configured LLM model + fallbacks
  - Webhook endpoint path + container port
  - Process uptime (since this worker started)
  - The last N log lines (bounded in-memory ring buffer via loguru)
  - Reviews currently in flight, parsed from recent log lines:
      'Performing auto command ... for api_url=...'        -> started
      'Processing comment on PR api_url=...'               -> started
      (the PR URL appears once per review; completion is
       implicit when no new line references it for a while,
       OR explicit via an exception trace)

Zero changes to upstream pr-agent code. This module is mounted into the app
object by pr_agent/status_app.py (the entrypoint shim) at container startup.

Privacy: the status page shows repo/PR URLs and model names — no secrets, no
API keys. At the default CONFIG.VERBOSITY_LEVEL (0/1), no diff or prompt content
appears in the log tail (PR bodies are logged at DEBUG, below this sink's INFO
threshold). If an operator sets CONFIG.VERBOSITY_LEVEL>=2, pr-agent logs full
system/user prompts and AI responses at INFO, which this page's recent-log tail
will surface — lower verbosity or restrict status page access in that case.
Guard /status behind your tunnel's access control (Cloudflare Access, Tailscale
ACL, basic auth on the reverse proxy) regardless. The page enforces no auth
itself, by design — the webhook endpoint must be public, so layering auth only
on /status would be security theater.
"""

import time
from collections import deque
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# Bounded in-memory log buffer. Each entry is (epoch_seconds, formatted_line).
# loguru calls _sink on every record. Sized to hold ~5 min of a busy bot's logs.
LOG_BUFFER: deque = deque(maxlen=500)
_PROCESS_START = time.time()
# In-flight reviews older than this are considered finished (no explicit
# completion marker in the light scope). The LLM call is bounded by
# CONFIG.AI_TIMEOUT (upstream default 120s; this deployment sets 300s). We
# derive the TTL as 2x the configured AI_TIMEOUT so a review still running
# near its deadline stays visible; if an operator raises AI_TIMEOUT, the TTL
# scales automatically. Falls back to 360s if config is unreadable at import.
def _ttl_from_config():
    try:
        from pr_agent.config_loader import get_settings
        return max(360, int(get_settings().config.ai_timeout) * 2)
    except Exception:
        return 360
_INFLIGHT_TTL_SECONDS = _ttl_from_config()
_INFLIGHT_RE = None  # compiled lazily in _inflight_reviews()


def _sink(message):
    """loguru sink: append (now, rendered line) to the ring buffer."""
    try:
        LOG_BUFFER.append((time.time(), str(message).rstrip("\n")))
    except Exception:
        # Never let the status sink crash the app.
        pass


def install_log_sink():
    """Attach the in-memory sink to the global loguru logger. Idempotent."""
    from loguru import logger
    # Guard against double-install on gunicorn reload.
    if not getattr(logger, "_status_sink_installed", False):
        logger.add(_sink, level="INFO", format="{time:HH:mm:ss} | {level: <5} | {message}", colorize=False)
        logger._status_sink_installed = True  # type: ignore[attr-defined]


def _safe_get_settings():
    try:
        from pr_agent.config_loader import get_settings
        return get_settings()
    except Exception as e:
        return {"_error": f"config unavailable: {e}"}


def _try_app_slug(s):
    """Best-effort: resolve the GitHub App slug for display. Don't crash on failure."""
    try:
        from pr_agent.config_loader import get_settings
        if str(s.github.deployment_type).lower() != "app":
            return None
        # We don't want to make an outbound API call on every status hit, so just
        # surface the configured app_id. The actual slug requires a JWT-signed call.
        return f"app_id={s.github.app_id}"
    except Exception:
        return None


def _inflight_reviews(log_entries):
    """Parse recent log entries for reviews that started but have no completion
    marker yet. Returns a list of {api_url, command, age_seconds}.

    Heuristic, not authoritative — for a precise view use the Medium scope.
    Uses time-based TTL (not line-count) so long reviews with many progress
    log lines don't vanish from the dashboard mid-flight."""
    import re
    global _INFLIGHT_RE
    if _INFLIGHT_RE is None:
        # Two anchored patterns (not one alternation with lazy `.*?`) to avoid
        # O(n^2) backtracking on lines that contain the trigger phrase but no
        # rendered api_url=' (e.g. exception tracebacks embedding the source
        # line). Python's re disallows reusing a group name across `|`, so
        # each branch gets its own url group; we normalize below.
        # {api_url=} renders as api_url='...'. Require the closing quote so
        # the URL can't absorb the trailing quote / comma.
        _INFLIGHT_RE = re.compile(
            r"Performing auto command '(?P<cmd>[^']+)', for api_url='(?P<url1>[^']+)'"
            r"|"
            r"Processing comment on PR api_url='(?P<url2>[^']+)'"
        )
    now = time.time()
    started = {}  # url -> {api_url, command, age_seconds}
    for ts, line in log_entries:
        m = _INFLIGHT_RE.search(line)
        if m:
            url = m.group("url1") or m.group("url2")
            started[url] = {
                "api_url": url,
                "command": m.group("cmd") or "/ask",
                "age_seconds": int(now - ts),
            }
    # Drop entries older than the TTL — assumes they finished.
    return [v for v in started.values() if v["age_seconds"] < _INFLIGHT_TTL_SECONDS]


def _g(s, section, key, default=None):
    """Safe attribute access on a dynaconf settings namespace. Returns default
    on missing section/key instead of raising (consistent with the module's
    graceful-degradation pattern)."""
    try:
        return getattr(getattr(s, section), key, default)
    except Exception:
        return default


def _status_payload():
    s = _safe_get_settings()
    if isinstance(s, dict) and s.get("_error"):
        return {"error": s["_error"], "uptime_seconds": int(time.time() - _PROCESS_START)}
    entries = list(LOG_BUFFER)
    log_lines = [line for _, line in entries]
    _oai_base = _g(s, "openai", "api_base")
    _oll_base = _g(s, "ollama", "api_base")
    _model = _g(s, "config", "model", "") or ""
    # litellm routes by the model prefix; the api_base that actually gets used
    # follows litellm's init order (OLLAMA.api_base overwrites OPENAI.api_base at
    # litellm_ai_handler.py L170, so Ollama wins when both are set). Derive the
    # provider from the prefix (the routing signal) and the endpoint from the
    # ollama-wins order, and flag the both-sections-filled hazard so /status is
    # an honest diagnostic rather than a misleading one.
    _provider_from_prefix = _model.split("/", 1)[0] if "/" in _model else ""
    return {
        "status": "ok",
        "bot": _try_app_slug(s) or "not configured",
        "model": _g(s, "config", "model", "unknown"),
        "fallback_models": list(_g(s, "config", "fallback_models", []) or []),
        "llm_provider": _provider_from_prefix or "none",
        "llm_api_base": _oll_base or _oai_base or "not set",
        "provider_mismatch": bool(_oai_base and _oll_base),
        "ai_timeout_seconds": _g(s, "config", "ai_timeout", 0),
        "webhook_path": "/api/v1/github_webhooks",
        "deployment_type": _g(s, "github", "deployment_type", "unknown"),
        "uptime_seconds": int(time.time() - _PROCESS_START),
        "started_at": datetime.fromtimestamp(_PROCESS_START, tz=timezone.utc).isoformat(),
        "inflight_reviews": _inflight_reviews(entries),
        "recent_log_lines": log_lines[-80:],
    }


_STATUS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>pr-agent status</title>
<style>
  body { font: 14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; margin: 2rem auto; max-width: 60rem; color: #1f2328; background: #fff; }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  .sub { color: #57606a; margin-bottom: 1.5rem; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }
  td { padding: .35rem .75rem; border-bottom: 1px solid #eaecef; vertical-align: top; }
  td:first-child { color: #57606a; white-space: nowrap; width: 14rem; }
  code { background: #f6f8fa; padding: .1rem .3rem; border-radius: 4px; font-size: .85em; }
  .inflight { margin-bottom: 1.5rem; }
  .inflight-item { padding: .5rem .75rem; background: #fff8c5; border-left: 3px solid #d4a72c; margin-bottom: .4rem; border-radius: 4px; }
  .inflight-item code { background: transparent; }
  pre { background: #f6f8fa; padding: .75rem; border-radius: 6px; overflow-x: auto; font-size: .8rem; line-height: 1.4; max-height: 24rem; }
  .auto { font-size: .8rem; color: #57606a; }
</style>
</head>
<body>
<h1>pr-agent <span style="color:#57606a">self-hosted</span></h1>
<div class="sub">Bot status &amp; recent activity. Auto-refreshes every 5s.</div>

<table id="cfg">
  <tr><td>Bot identity</td><td id="bot"></td></tr>
  <tr><td>LLM model</td><td id="model"></td></tr>
  <tr><td>Fallback models</td><td id="fallback"></td></tr>
  <tr><td>LLM endpoint</td><td id="llm"></td></tr>
  <tr><td>AI call timeout</td><td id="timeout"></td></tr>
  <tr><td>Webhook endpoint</td><td id="webhook"></td></tr>
  <tr><td>Uptime</td><td id="uptime"></td></tr>
  <tr><td>Started at</td><td id="started"></td></tr>
</table>

<div class="inflight">
  <strong>In-flight reviews</strong> <span id="inflight-count" class="sub"></span>
  <div id="inflight"></div>
</div>

<div>
  <strong>Recent log</strong>
  <pre id="log">loading…</pre>
</div>

<div class="auto">JSON: <a href="/status.json">/status.json</a> · Health: <a href="/">/</a></div>

<script>
function fmtDur(s){ if(s<60) return s+'s'; const m=Math.floor(s/60); return m+'m '+(s%60)+'s'; }
async function refresh(){
  try {
    const d = await (await fetch('/status.json')).json();
    document.getElementById('bot').textContent = d.bot;
    document.getElementById('model').textContent = d.model;
    document.getElementById('fallback').textContent = (d.fallback_models||[]).join(' ');
    document.getElementById('llm').textContent = (d.llm_provider ? '[' + d.llm_provider + '] ' : '') + d.llm_api_base + (d.provider_mismatch ? '  ⚠ BOTH [openai] and [ollama] api_base are set — Ollama wins; comment out the section for the provider you are NOT using' : '');
    document.getElementById('timeout').textContent = d.ai_timeout_seconds + 's';
    document.getElementById('webhook').textContent = 'POST ' + d.webhook_path;
    document.getElementById('uptime').textContent = fmtDur(d.uptime_seconds);
    document.getElementById('started').textContent = d.started_at;
    const inf = document.getElementById('inflight');
    inf.innerHTML = '';
    (d.inflight_reviews||[]).forEach(r=>{
      const el = document.createElement('div'); el.className='inflight-item';
      el.textContent = r.api_url + ' — ' + r.command;
      inf.appendChild(el);
    });
    document.getElementById('inflight-count').textContent = '('+(d.inflight_reviews||[]).length+')';
    document.getElementById('log').textContent = (d.recent_log_lines||[]).join('\\n');
  } catch(e) { document.getElementById('log').textContent = 'refresh failed: '+e; }
}
refresh(); setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def mount(app: FastAPI):
    """Attach the status routes to an existing FastAPI app. Call once at startup."""
    install_log_sink()

    @app.get("/status", response_class=HTMLResponse)
    async def status_html():
        return _STATUS_HTML

    @app.get("/status.json", response_class=JSONResponse)
    async def status_json():
        return _status_payload()