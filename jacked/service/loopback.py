"""JSON requests from the tray agents to the jacked service on this machine.

The macOS menu-bar agent and the Windows/Linux tray tooltip read the service
they belong to. These requests never go through a proxy (a proxy setting must
not capture a request to this machine), never follow a redirect, and read at
most :data:`MAX_BODY_BYTES`. A failed request raises; the caller decides what
to show.
"""

from __future__ import annotations

import json
import urllib.request

# The largest response body a tray agent reads. A summary holds two accounts;
# its largest field is capped far below this by the 256 KiB usage-payload cap.
MAX_BODY_BYTES = 1 << 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: the 3xx answer then raises HTTPError."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _opener() -> urllib.request.OpenerDirector:
    """A new opener with no proxy and no redirects.

    It is built per request on purpose: an opener reads the proxy settings
    when it is built, so a module-level opener would hide a later change in
    how it is built from the tests.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _read_json(req: urllib.request.Request, timeout: float) -> dict:
    with _opener().open(req, timeout=timeout) as resp:
        body = resp.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(f"response body is larger than {MAX_BODY_BYTES} bytes")
    if not body:
        return {}
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def get_json(url: str, timeout: float = 2.0) -> dict:
    """GET a JSON object. A body that is not a JSON object gives ``{}``."""
    return _read_json(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout)


def send_json(url: str, method: str, payload: dict | None, timeout: float = 3.0) -> dict:
    """Send *payload* as JSON with *method* and return the JSON object answer."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    return _read_json(req, timeout)
