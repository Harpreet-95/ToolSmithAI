"""Primitive Executor — Dynamic Tool Composer v1.

Executes approved dynamic tools from the DB registry using a closed set of
pre-built primitives. No arbitrary Python is ever evaluated (no exec/eval).

Supported primitives:
  http_request      — HTTP GET/POST with SSRF protection and response size cap
  transform_json    — dot-path key extraction/rename from step params
  send_email        — email delivery via existing SMTP infrastructure
  send_notification — in-app notification (simulated in v1)
  format_output     — safe {identifier}-only string template substitution

Security model:
  - ALLOWED_PRIMITIVES is a frozen set; any unknown primitive is rejected.
  - http_request blocks private/loopback/link-local IPs and optionally enforces
    a domain allowlist (DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS).
  - format_output only substitutes {simple_identifier} patterns; format specs,
    attribute access, and conversion flags are never evaluated.
  - DNS rebinding is a known v1 limitation — the IP check is pre-resolution.
    Use DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS in production for stronger control.
"""

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_PRIMITIVES: frozenset = frozenset({
    "http_request",
    "transform_json",
    "send_email",
    "send_notification",
    "format_output",
})

# Matches only {simple_identifier} — no format specs, no attribute access.
_SAFE_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_template(template: str, context: dict) -> str:
    """Substitute {identifier} placeholders from context.

    Unrecognised placeholders are left verbatim. Format specs and attribute
    access (e.g. {obj.attr}, {val!r}, {val:02d}) are never evaluated because
    the regex only matches bare identifiers.
    """
    return _SAFE_PLACEHOLDER.sub(
        lambda m: str(context.get(m.group(1), m.group(0))),
        template,
    )


def _extract_path(data, path: str):
    """Traverse a dot-separated key path in a nested dict/list structure.

    Returns None on any miss rather than raising.
    """
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _check_ssrf(url: str) -> None:
    """Raise ValueError if the URL targets a private, loopback, or reserved address.

    Checks scheme, explicit IP addresses, and well-known private hostnames.
    Note: hostname-based SSRF via DNS rebinding is not fully mitigated here.
    Use DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS for defence-in-depth in production.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as exc:
        raise ValueError(f"Malformed URL: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            f"URL scheme '{scheme}' is not permitted. Only http and https are allowed."
        )

    host = parsed.hostname
    if not host:
        raise ValueError("URL has no host component.")

    # Block well-known private hostnames regardless of what they resolve to
    if host.lower() in ("localhost", "localhost.localdomain", "broadcasthost"):
        raise ValueError(f"Requests to '{host}' are blocked (private host).")

    # If the host looks like an IP address, check the address range
    try:
        addr = ipaddress.ip_address(host)
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise ValueError(
                f"Requests to '{host}' are blocked (private/reserved IP range)."
            )
    except ValueError as exc:
        if "blocked" in str(exc):
            raise  # re-raise our own SSRF error
        # ValueError from ip_address() means it's a hostname — proceed

    # Belt-and-suspenders: block common private IP string prefixes even if they
    # sneak through as hostnames (e.g. "10.0.0.1.nip.io" tricks are handled by
    # the allowlist, not here — this just catches bare IPs as strings)
    _PRIVATE_PREFIXES = ("127.", "10.", "172.16.", "172.17.", "172.18.", "172.19.",
                         "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
                         "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
                         "172.30.", "172.31.", "192.168.", "169.254.", "::1",
                         "fc00:", "fd", "fe80:")
    if any(host.startswith(p) for p in _PRIVATE_PREFIXES):
        raise ValueError(
            f"Requests to '{host}' are blocked (private/reserved IP range)."
        )


def _check_allowlist(url: str, allowed_domains: list) -> None:
    """Raise ValueError if the URL host is not in the configured allowlist.

    When allowed_domains is empty (default), any non-private host is allowed.
    """
    if not allowed_domains:
        return
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    for domain in allowed_domains:
        d = domain.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return
    raise ValueError(
        f"URL host '{host}' is not in DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS. "
        f"Allowed: {allowed_domains}"
    )


# ---------------------------------------------------------------------------
# Primitive handlers
# ---------------------------------------------------------------------------

def _exec_http_request(config: dict, params: dict) -> dict:
    """Make an HTTP GET or POST request to an external URL.

    config keys:
      url_template  (str, required) — URL with {param} placeholders
      method        (str)           — "GET" or "POST" (default "GET")
      headers       (dict)          — extra HTTP headers
      body_template (str)           — POST body with {param} placeholders
      output_path   (str)           — dot-path to extract from JSON response
      output_key    (str)           — key name for extracted value in output dict
    """
    from core.config import (
        DYNAMIC_TOOL_HTTP_TIMEOUT_SECONDS,
        DYNAMIC_TOOL_HTTP_MAX_RESPONSE_BYTES,
        DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS,
    )

    url_template = config.get("url_template", "")
    if not url_template:
        raise ValueError("http_request config is missing required 'url_template'.")

    url = _safe_template(url_template, params)
    _check_ssrf(url)
    _check_allowlist(url, DYNAMIC_TOOL_HTTP_ALLOWED_DOMAINS)

    method = (config.get("method") or "GET").upper()
    if method not in ("GET", "POST"):
        raise ValueError(
            f"http_request method '{method}' is not allowed. Only GET and POST are supported."
        )

    headers: dict = {}
    raw_headers = config.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ValueError("http_request config 'headers' must be a JSON object.")
    for k, v in raw_headers.items():
        headers[str(k)] = _safe_template(str(v), params)

    body_bytes: bytes | None = None
    if method == "POST":
        body_template = config.get("body_template") or ""
        body_str = _safe_template(body_template, params)
        body_bytes = body_str.encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=DYNAMIC_TOOL_HTTP_TIMEOUT_SECONDS) as resp:
            status_code: int = resp.getcode()
            raw_body: bytes = resp.read(DYNAMIC_TOOL_HTTP_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP {exc.code} error from '{url}': {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Request to '{url}' failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ValueError(
            f"Request to '{url}' timed out after {DYNAMIC_TOOL_HTTP_TIMEOUT_SECONDS}s."
        ) from exc

    body_str = raw_body.decode("utf-8", errors="replace")

    # Attempt JSON parse; fall back to raw string
    try:
        parsed_body = json.loads(body_str)
    except (ValueError, TypeError):
        parsed_body = body_str

    output_path = config.get("output_path")
    output_key = config.get("output_key") or "result"

    if output_path and isinstance(parsed_body, dict):
        value = _extract_path(parsed_body, output_path)
    else:
        value = parsed_body

    return {"status_code": status_code, "url": url, output_key: value}


def _exec_transform_json(config: dict, params: dict) -> dict:
    """Extract and optionally rename fields from step params.

    config keys:
      mappings (list, required) — list of {"from": "dot.path", "to": "output_key"}

    Each "from" is a dot-path traversed against params.
    "to" defaults to the last segment of "from" if omitted.
    """
    mappings = config.get("mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError(
            "transform_json config requires a non-empty 'mappings' list "
            "e.g. [{\"from\": \"a.b\", \"to\": \"value\"}]"
        )

    result: dict = {}
    for mapping in mappings:
        if not isinstance(mapping, dict):
            raise ValueError("Each entry in 'mappings' must be a JSON object.")
        src = str(mapping.get("from") or "")
        if not src:
            raise ValueError("Each mapping must have a non-empty 'from' key.")
        dst = str(mapping.get("to") or src.split(".")[-1])
        result[dst] = _extract_path(params, src)

    return result


def _exec_send_email(config: dict, params: dict) -> dict:
    """Send an email using the configured SMTP infrastructure.

    config keys (all overridable by params):
      to       (str) — recipient address
      subject  (str) — email subject
      body     (str) — plain-text body with {identifier} placeholders
    """
    from core.config import ENABLE_REAL_EMAIL
    from core.email import send_real_email

    merged = {**config, **params}
    to      = merged.get("to")
    subject = _safe_template(str(merged.get("subject") or "ToolSmithAI Notification"), params)
    body    = _safe_template(str(merged.get("body") or f"Notification: {subject}"), params)

    if not ENABLE_REAL_EMAIL:
        return {"to": to, "subject": subject, "message": "Email sent (simulated)"}

    if not to:
        return {"to": None, "subject": subject, "sent": False, "message": "No recipient address."}

    result = send_real_email(to=to, subject=subject, body=body)
    return {
        "to": to,
        "subject": subject,
        "sent": result["sent"],
        "message": (
            f"Email delivered to {to}"
            if result["sent"]
            else f"Email failed: {result.get('reason', 'unknown error')}"
        ),
    }


def _exec_send_notification(config: dict, params: dict) -> dict:
    """Send an in-app notification (simulated in v1).

    config keys (all overridable by params):
      channel  (str)
      message  (str) — message body with {identifier} placeholders
      priority (str)
    """
    merged = {**config, **params}
    return {
        "channel":  merged.get("channel"),
        "priority": merged.get("priority"),
        "message":  _safe_template(
            str(merged.get("message") or "Notification delivered."), params
        ),
        "status": "delivered (simulated)",
    }


def _exec_format_output(config: dict, params: dict) -> dict:
    """Render a template string using step params.

    config keys:
      template   (str, required) — string with {identifier} placeholders
      output_key (str)           — key for the rendered string in the output dict
    """
    template = config.get("template")
    if not isinstance(template, str):
        raise ValueError("format_output config requires a 'template' string.")
    output_key = config.get("output_key") or "text"
    return {output_key: _safe_template(template, params)}


# ---------------------------------------------------------------------------
# Dispatch map and public interface
# ---------------------------------------------------------------------------

_PRIMITIVE_HANDLERS: dict = {
    "http_request":      _exec_http_request,
    "transform_json":    _exec_transform_json,
    "send_email":        _exec_send_email,
    "send_notification": _exec_send_notification,
    "format_output":     _exec_format_output,
}


def run_primitive(tool_def: dict, step: dict) -> dict:
    """Execute a dynamic tool's primitive. Never runs arbitrary code.

    Args:
        tool_def: registry entry for the tool; must have source="dynamic",
                  primitive_type, and primitive_config populated by _load_registry.
        step:     execution step dict with operation and params.

    Raises:
        ValueError: invalid config, blocked URL, unknown primitive, etc.
    """
    primitive_type = tool_def.get("primitive_type")
    if not primitive_type:
        raise ValueError(
            f"Dynamic tool '{tool_def.get('name')}' has no 'primitive_type' "
            "in its config_json. Cannot execute."
        )
    if primitive_type not in ALLOWED_PRIMITIVES:
        raise ValueError(
            f"Primitive type '{primitive_type}' is not in the allowed set: "
            f"{sorted(ALLOWED_PRIMITIVES)}"
        )

    primitive_config = tool_def.get("primitive_config")
    if not isinstance(primitive_config, dict):
        raise ValueError(
            f"Dynamic tool '{tool_def.get('name')}' has invalid primitive_config "
            "(must be a JSON object)."
        )

    params = step.get("params") or {}
    return _PRIMITIVE_HANDLERS[primitive_type](primitive_config, params)
