"""OAuth PKCE flow for Claude account authentication.

Implements the full OAuth2 + PKCE authorization flow:
1. Generate PKCE verifier + challenge
2. Start async callback server on ports 45100-45199
3. Open browser to Anthropic's auth URL
4. Receive callback with authorization code
5. Exchange code for tokens (JSON body, NOT form-encoded)
6. Optionally create long-lived API key
7. Fetch profile + usage data
8. Store everything in the database

Adapted from ralphx — same CLIENT_ID, same Anthropic endpoints.
"""

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import time
from typing import Optional
from urllib.parse import parse_qs, urlencode

import httpx
from aiohttp import web

from jacked.web.database import Database

logger = logging.getLogger("jacked.oauth")


def _activate_oauth_account(db: Database, account: dict, operation_id: str):
    """Run OAuth-triggered activation through the shared transaction engine."""
    from jacked.credentials.models import SwitchContext
    from jacked.credentials.runtime import activate_account

    return activate_account(db, account, SwitchContext.OAUTH, operation_id)

# ---------------------------------------------------------------------------
# Constants — from design doc section 5
# ---------------------------------------------------------------------------

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
API_KEY_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CALLBACK_PORT_RANGE = range(45100, 45200)
# Anthropic's registered code-display redirect for this client id. Approving
# with this redirect_uri renders the authorization code for manual copy-paste
# — the only callback shape that works when the dashboard is opened from a
# machine other than the one running jacked.
MANUAL_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
BROWSER_TIMEOUT_SECONDS = 120  # localhost-callback flow
MANUAL_TIMEOUT_SECONDS = 600  # a human copies a code across machines
# How long a FINISHED flow stays pollable. The dashboard learns the verdict by
# polling once a second, but the sign-in window takes the foreground and a
# hidden tab's timers get throttled (Chrome: once a minute after five minutes
# hidden, or frozen outright). Dropping the flow 30s after the callback meant a
# late poll came back not_found: the tokens were stored, the banner said
# "expired", and until the next reload the account-action guard stayed up and
# every Use Account click was silently refused.
FLOW_RETENTION_SECONDS = 300
# Each code submission costs an outbound token-exchange call, and the dashboard
# API is network-trusted (no per-request auth) — bound the attempts per flow.
MAX_SUBMIT_ATTEMPTS = 10
DEFAULT_TOKEN_TTL_SECONDS = 28800  # 8 hours — default token lifetime from Anthropic

# organization_type → subscription_type mapping (design doc section 4e)
ORG_TYPE_MAP = {
    "claude_max": "max",
    "claude_pro": "pro",
    "claude_enterprise": "enterprise",
    "claude_team": "team",
}


def identity_hint_params(account: Optional[dict]) -> dict:
    """Extra authorize-URL params that pre-select the account being re-authed.

    login_hint pre-fills the claude.ai email box and forces account selection;
    orgUUID pre-selects the org (matters for one email with several orgs).

    >>> identity_hint_params(None)
    {}
    >>> identity_hint_params({"email": "a@b.com", "organization_uuid": ""})
    {'login_hint': 'a@b.com'}
    >>> identity_hint_params({"email": "a@b.com", "organization_uuid": "org-1"})
    {'login_hint': 'a@b.com', 'orgUUID': 'org-1'}
    >>> identity_hint_params({"email": "", "organization_uuid": "org-1"})
    {'orgUUID': 'org-1'}
    """
    if not account:
        return {}
    params: dict = {}
    email = account.get("email") or ""
    if email:
        params["login_hint"] = email
    org_uuid = account.get("organization_uuid") or ""
    if org_uuid:
        params["orgUUID"] = org_uuid
    return params


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE verifier and challenge.

    >>> v, c = generate_pkce()
    >>> len(v) > 20
    True
    >>> len(c) > 20
    True
    >>> '=' not in c
    True
    """
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Flow manager — tracks in-flight OAuth flows by flow_id
# ---------------------------------------------------------------------------

# Global dict of active flows: flow_id -> OAuthFlow
_active_flows: dict[str, "OAuthFlow"] = {}


def reset_locks() -> None:
    """Clear in-flight OAuth flows on lifespan startup.

    Each OAuthFlow owns an ``asyncio.Event`` bound to the loop that
    created it (``OAuthFlow.__init__``). When the tray restarts uvicorn
    in a fresh thread/loop, any mid-OAuth flow's Event is stranded on
    the dead loop — a later ``await event.wait()`` would raise. Users
    who clicked Restart mid-OAuth already lost their flow; drop the
    stale entries so a fresh attempt starts clean. Log the count so
    on-call can correlate "PKCE callback returned 'flow not found'"
    complaints with a tray restart.
    """
    stranded = len(_active_flows)
    if stranded:
        logger.warning(
            "reset_locks: dropping %d stranded OAuth flow(s) from previous loop: %s",
            stranded, list(_active_flows.keys()),
        )
    _active_flows.clear()


def get_flow(flow_id: str) -> Optional["OAuthFlow"]:
    """Get an active OAuth flow by ID.

    >>> get_flow("nonexistent") is None
    True
    """
    return _active_flows.get(flow_id)


def get_flow_status(flow_id: str) -> dict:
    """Get the status of an OAuth flow by flow_id.

    Convenience wrapper for the API layer.
    Returns status dict with status, flow_id, and optional account_id/email/error.

    >>> get_flow_status("nonexistent")
    {'status': 'not_found', 'flow_id': 'nonexistent'}
    """
    flow = get_flow(flow_id)
    if flow is None:
        return {"status": "not_found", "flow_id": flow_id}
    return flow.get_status()


async def start_oauth_flow(db: Database) -> dict:
    """Start a new OAuth flow. Convenience function for the API layer.

    Creates an OAuthFlow, calls start(), returns the result dict
    containing flow_id and auth_url.
    """
    flow = OAuthFlow(db)
    return await flow.start()


class OAuthFlow:
    """Manages a single OAuth PKCE authorization flow.

    Lifecycle:
    1. Create flow with start() — opens browser, starts callback server
    2. Frontend polls status via get_status()
    3. Callback arrives → token exchange → profile/usage fetch → DB store
    4. Frontend sees status='completed' and reloads account list
    """

    def __init__(
        self,
        db: Database,
        purpose: str = "primary",
        target_account_id: Optional[int] = None,
        manual: bool = False,
        allow_credential_activation: bool = True,
    ):
        self.db = db
        self.purpose = purpose  # "primary" | "claude_code"
        self._target_account_id = target_account_id
        # Re-auth flows know whose account they are for. That identity drives
        # the login_hint/orgUUID params and the per-account browser profile.
        self._target_account: Optional[dict] = None
        if target_account_id:
            try:
                self._target_account = db.get_account(target_account_id)
            except Exception as e:
                logger.debug(
                    "Could not load re-auth target account %s: %s",
                    target_account_id, e,
                )
                self._target_account = None
        self._browser_mode: Optional[str] = None
        self._browser_name: Optional[str] = None
        # Manual mode (remote dashboards): no local callback server, no local
        # browser. The user opens the auth link themselves and pastes the code
        # that Anthropic's code page shows.
        self.manual = manual
        self._allow_credential_activation = allow_credential_activation
        self.flow_id = secrets.token_urlsafe(16)
        self._verifier: Optional[str] = None
        self._state: Optional[str] = None
        self._redirect_uri: Optional[str] = None
        self._auth_url: Optional[str] = None
        self._status = "pending"  # pending | completed | error
        self._result: Optional[dict] = None
        self._error: Optional[str] = None
        self._cc_flow_id: Optional[str] = None
        self._event = asyncio.Event()
        self._submit_lock = asyncio.Lock()
        self._submit_attempts = 0
        self._created_at = time.time()

    @property
    def mode(self) -> str:
        """Flow mode for the frontend: 'manual' or 'browser'."""
        return "manual" if self.manual else "browser"

    def _identity_fields(self) -> dict:
        """Who this flow is authorizing, for the dashboard's waiting banner."""
        fields: dict = {"purpose": self.purpose}
        if self._target_account:
            fields["target_account_id"] = self._target_account.get("id")
            fields["target_email"] = self._target_account.get("email")
            fields["target_org_name"] = (
                self._target_account.get("organization_name") or None
            )
        if self._browser_mode is not None:
            fields["browser_mode"] = self._browser_mode
        if self._browser_name is not None:
            fields["browser_name"] = self._browser_name
        return fields

    def get_status(self) -> dict:
        """Get current flow status for polling.

        >>> db = Database(":memory:")
        >>> flow = OAuthFlow(db)
        >>> flow.get_status()["status"]
        'pending'
        """
        # Manual flows get the longer window: a human copies a code across machines
        limit = MANUAL_TIMEOUT_SECONDS if self.manual else BROWSER_TIMEOUT_SECONDS
        if self._status == "pending" and time.time() - self._created_at > limit:
            self._status = "not_found"

        result: dict = {
            "status": self._status,
            "flow_id": self.flow_id,
            "mode": self.mode,
            **self._identity_fields(),
        }
        if self._auth_url:
            result["auth_url"] = self._auth_url
        if self._result:
            result["account_id"] = self._result.get("account_id") or self._result.get("id")
            result["email"] = self._result.get("email")
            result["organization_name"] = self._result.get("organization_name")
            for field in (
                "activation_status",
                "activation_operation_id",
                "activation_message",
            ):
                if field in self._result:
                    result[field] = self._result[field]
            if self._result.get("_redirected_from"):
                result["redirected_from_account_id"] = self._result["_redirected_from"]
        if self._error:
            result["error"] = self._error
        if self._cc_flow_id:
            result["cc_flow_id"] = self._cc_flow_id
        return result

    async def start(self) -> dict:
        """Start the OAuth flow.

        Browser mode: spin up the localhost callback server and open the
        local browser. Manual mode (remote dashboards): no server and no
        browser — the frontend shows the auth link and the user pastes the
        code that Anthropic's code page displays.

        Returns dict with flow_id, auth_url, and mode for the frontend.
        """
        self._verifier, challenge = generate_pkce()
        self._state = secrets.token_urlsafe(32)

        # Register this flow globally
        _active_flows[self.flow_id] = self

        runner: Optional[web.AppRunner] = None
        if self.manual:
            self._redirect_uri = MANUAL_REDIRECT_URI
        else:
            # Start callback server
            app = web.Application()
            app.router.add_get("/callback", self._handle_callback)
            runner = web.AppRunner(app)
            await runner.setup()

            port = None
            for p in CALLBACK_PORT_RANGE:
                try:
                    site = web.TCPSite(runner, "localhost", p)
                    await site.start()
                    port = p
                    break
                except OSError:
                    continue

            if port is None:
                await runner.cleanup()
                self._status = "error"
                self._error = "No available port for callback server (45100-45199)"
                return {"error": self._error, "flow_id": self.flow_id}

            self._redirect_uri = f"http://localhost:{port}/callback"
            logger.info(f"OAuth callback server started on port {port}")

        # Build auth URL — note: code=true is REQUIRED (non-standard)
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": self._redirect_uri,
            "scope": SCOPES,
            "state": self._state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "code": "true",
        }
        params.update(identity_hint_params(self._target_account))
        self._auth_url = f"{AUTH_URL}?{urlencode(params)}"

        if self.manual:
            logger.info(
                f"Manual OAuth flow started (remote dashboard, purpose={self.purpose})"
            )
            # Expire the flow ourselves — there is no callback server whose
            # _wait_for_callback would do it.
            asyncio.create_task(self._expire_manual_flow())
        else:
            # Best-effort: a headless server must not kill the flow — the
            # frontend renders the auth link either way.
            try:
                from jacked.web.browser_launch import open_auth_url

                launched = open_auth_url(
                    self._auth_url, self._target_account, self.db
                )
                self._browser_mode = launched.mode
                self._browser_name = launched.browser
                logger.info(
                    "Opened browser for OAuth authorization (mode=%s)",
                    self._browser_mode,
                )
            except Exception as e:
                logger.warning(f"Could not open a local browser for OAuth: {e}")

            # Wait for callback in background — don't block the API response
            asyncio.create_task(self._wait_for_callback(runner))

        return {
            "flow_id": self.flow_id,
            "auth_url": self._auth_url,
            "mode": self.mode,
            **self._identity_fields(),
        }

    async def reopen_browser(self) -> dict:
        """Open the sign-in window again for a flow that is still waiting.

        Windows can refuse to raise a window opened by a background service,
        so the user may never see the one start() launched. Re-launching is
        cheap and correct: Chrome, Edge and Brave hand the URL to the instance
        already running that profile, which opens a tab and gets focused, and
        the launcher's focus pass runs again either way.
        """
        if self.manual:
            return {
                **self.get_status(),
                "reopen_error": "This flow has no local browser to reopen.",
            }
        if self._status != "pending":
            return {
                **self.get_status(),
                "reopen_error": f"Flow is {self._status}, not awaiting sign-in.",
            }
        if not self._auth_url:
            return {
                **self.get_status(),
                "reopen_error": "This flow has no authorization URL yet.",
            }

        try:
            from jacked.web.browser_launch import open_auth_url

            launched = open_auth_url(
                self._auth_url, self._target_account, self.db
            )
        except Exception as e:
            logger.warning("Could not re-open the OAuth browser: %s", e)
            return {
                **self.get_status(),
                "reopen_error": "Could not open a browser on the jacked machine.",
            }

        self._browser_mode = launched.mode
        self._browser_name = launched.browser
        logger.info("Re-opened OAuth browser (mode=%s)", launched.mode)
        return self.get_status()

    async def _expire_manual_flow(self) -> None:
        """Expire a manual flow, mirroring _wait_for_callback's cleanup."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=MANUAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if self._status == "pending":
                self._status = "not_found"
                self._error = "OAuth flow timed out (10 minutes)"
        finally:
            # Keep the verdict pollable for a while before forgetting the flow
            await asyncio.sleep(FLOW_RETENTION_SECONDS)
            _active_flows.pop(self.flow_id, None)

    async def _wait_for_callback(self, runner: web.AppRunner) -> None:
        """Wait for the callback, then clean up the server."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=BROWSER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._status = "not_found"
            self._error = "OAuth flow timed out (2 minutes)"
        finally:
            await runner.cleanup()
            # Keep the verdict pollable for a while before forgetting the flow
            await asyncio.sleep(FLOW_RETENTION_SECONDS)
            _active_flows.pop(self.flow_id, None)

    async def _handle_callback(self, request: web.Request) -> web.Response:
        """Handle the OAuth callback from Anthropic."""
        code = request.query.get("code")
        state = request.query.get("state")
        error = request.query.get("error")
        error_desc = request.query.get("error_description", "")

        if error:
            self._status = "error"
            self._error = f"{error}: {error_desc}" if error_desc else error
            self._event.set()
            return web.Response(
                text="<h1>Error</h1><p>Authentication failed. You can close this window.</p>",
                content_type="text/html",
            )

        # CSRF check — state validation also prevents cross-flow purpose confusion
        # (each flow has a unique state token, so a CC callback can't hit a primary flow)
        if state != self._state:
            self._status = "error"
            self._error = "Invalid state parameter (possible CSRF attack)"
            self._event.set()
            return web.Response(
                text="<h1>Error</h1><p>Security validation failed.</p>",
                content_type="text/html",
            )

        if code:
            try:
                result = await self._complete_auth(code)
                self._result = result
                self._status = "completed"
            except Exception as e:
                logger.error(
                    f"OAuth completion failed for flow {self.flow_id} "
                    f"(purpose={self.purpose}): {e}"
                )
                self._status = "error"
                self._error = str(e)
        else:
            self._status = "error"
            self._error = "No authorization code received"

        self._event.set()

        if self._status == "error":
            safe_error = html.escape(self._error or "Unknown error")
            return web.Response(
                text=f"<h1>Authorization Failed</h1><p>{safe_error}</p>"
                     "<p>Return to the jacked dashboard to try again.</p>",
                content_type="text/html",
            )
        return web.Response(
            text="<h1>Success!</h1><p>You can close this window.</p>"
                 "<script>window.close()</script>",
            content_type="text/html",
        )

    @staticmethod
    def parse_pasted_code(pasted: str) -> tuple[Optional[str], Optional[str]]:
        """Parse a pasted authorization code into ``(code, state)``.

        Accepts the three shapes a user can plausibly paste:
        bare code, ``code#state`` (what the Anthropic code page shows),
        and a full callback URL or query string.

        >>> OAuthFlow.parse_pasted_code("abc123")
        ('abc123', None)
        >>> OAuthFlow.parse_pasted_code("abc123#xyz")
        ('abc123', 'xyz')
        >>> OAuthFlow.parse_pasted_code(
        ...     "http://localhost:45100/callback?code=abc&state=xyz")
        ('abc', 'xyz')
        >>> OAuthFlow.parse_pasted_code("  ")
        (None, None)
        """
        pasted = (pasted or "").strip()
        if not pasted:
            return None, None
        if "code=" in pasted:
            query = pasted.split("?", 1)[-1]
            fields = parse_qs(query)
            code = (fields.get("code") or [None])[0]
            state = (fields.get("state") or [None])[0]
            return code or None, state or None
        if "#" in pasted:
            code, _, state = pasted.partition("#")
            return code.strip() or None, state.strip() or None
        return pasted, None

    async def submit_code(self, pasted: str) -> dict:
        """Complete the flow from a manually pasted authorization code.

        A parse or state failure returns an inline ``submit_error`` and keeps
        the flow pending so the user can paste again; only a failed token
        exchange marks the flow as error (matching the callback path). A
        foreign code cannot complete the flow either way: PKCE binds every
        exchangeable code to this flow's own code_challenge.
        """

        def _rejected(message: str) -> dict:
            return {**self.get_status(), "submit_error": message}

        if self._status != "pending":
            return _rejected(f"Flow is {self._status}, not awaiting a code.")
        if self._submit_lock.locked():
            return _rejected("A code submission is already in progress.")
        async with self._submit_lock:
            if self._status != "pending":  # re-check after acquiring
                return _rejected(f"Flow is {self._status}, not awaiting a code.")
            self._submit_attempts += 1
            if self._submit_attempts > MAX_SUBMIT_ATTEMPTS:
                self._status = "error"
                self._error = "Too many code submissions. Start the flow again."
                self._event.set()
                return self.get_status()
            code, state = self.parse_pasted_code(pasted)
            if not code:
                return _rejected("No authorization code found in the pasted text.")
            if state is not None and state != self._state:
                # Same CSRF posture as _handle_callback, but a paste mistake
                # must not brick the flow — report inline and stay pending.
                return _rejected(
                    "The pasted code belongs to a different authorization "
                    "attempt. Open the authorization link again and paste "
                    "the code it shows."
                )
            try:
                result = await self._complete_auth(code)
                self._result = result
                self._status = "completed"
            except Exception as e:
                logger.error(
                    f"OAuth manual completion failed for flow {self.flow_id} "
                    f"(purpose={self.purpose}): {e}"
                )
                self._status = "error"
                self._error = str(e)
            self._event.set()
            return self.get_status()

    def _should_become_active(self, account: dict) -> bool:
        """Whether this freshly-stored account should become the active one.

        True only when there is no valid active account yet, or this account
        already IS the active one (a re-auth). Adding an account must never
        steal the active selection from a different existing account — the
        user stays on whoever they were on. First account (or one added when
        the prior active account is gone) still becomes active.
        """
        for setting_key in ("desired_account_id", "active_account_id"):
            current = self.db.get_setting(setting_key)
            if not current:
                continue
            try:
                existing = self.db.get_account(int(current))
            except (TypeError, ValueError):
                continue
            if existing is not None:
                return existing["id"] == account.get("id")
        return True

    async def _complete_auth(self, code: str) -> dict:
        """Complete the OAuth flow: token exchange, API key, profile, usage, DB store."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Exchange code for tokens
            tokens = await self._exchange_code(client, code)

            access_token = tokens["access_token"]

            # Step 2: Optionally create API key (changes token lifecycle)
            # Only for primary flows — CC tokens need refresh capability
            if self.purpose == "primary" and "org:create_api_key" in tokens.get("scope", ""):
                access_token, tokens = await self._create_api_key(
                    client, access_token, tokens
                )

            # Step 3 & 4: Fetch profile & usage (skip for CC — raw OAuth
            # token isn't accepted by these endpoints, causing 401s + misleading logs)
            if self.purpose == "primary":
                profile = await self._fetch_profile(client, access_token)
                usage = await self._fetch_usage(client, access_token)
            else:
                profile = {}
                usage = {}

            # Step 5: Store in database (branches on purpose)
            account = self._store_account(tokens, profile, usage)

            # Decide activation ONCE. An account becomes active only when there
            # is no valid active account yet, or it already IS the active one
            # (re-auth). Adding a background account must not steal the active
            # selection from the account the user is currently on.
            activate = self._should_become_active(account)

            # Step 6: Write credentials to the live stores ONLY when this account
            # is (becoming) active. Otherwise adding an account would clobber the
            # active account's live credentials and silently switch Claude Code
            # to the new one. Non-active accounts live in the DB until the user
            # explicitly switches (use_account) or launches them.
            activation_result = None
            if activate and self._allow_credential_activation:
                activation_result = await asyncio.to_thread(
                    _activate_oauth_account,
                    self.db,
                    account,
                    f"oauth-{self.flow_id}",
                )

            # Step 7: For primary flows, persist active account + auto-start CC flow
            if self.purpose == "primary":
                # Auto-start CC flow so the account gets independent CC tokens.
                # Wrapped in try/except: CC failure is non-fatal — primary account
                # is already saved. Without this guard, a CC failure (e.g., no
                # available port) would propagate up to _handle_callback() and
                # mark the PRIMARY flow as errored.
                try:
                    cc_flow = OAuthFlow(
                        self.db,
                        purpose="claude_code",
                        target_account_id=account["id"],
                        allow_credential_activation=self._allow_credential_activation,
                    )
                    cc_result = await cc_flow.start()
                    self._cc_flow_id = cc_result.get("flow_id")
                except Exception as e:
                    logger.warning(f"CC auto-flow failed (non-fatal): {e}")
                    self._cc_flow_id = None

            response = {
                "account_id": account.get("id"),
                "email": account.get("email"),
            }
            if activation_result is not None:
                response.update(
                    {
                        "activation_status": activation_result.outcome.value,
                        "activation_operation_id": activation_result.operation_id,
                        "activation_message": activation_result.message,
                    }
                )
            elif activate and not self._allow_credential_activation:
                response.update(
                    {
                        "activation_status": "local_only",
                        "activation_message": (
                            "Account saved; credential activation is local-only."
                        ),
                    }
                )
            return response

    async def _exchange_code(self, client: httpx.AsyncClient, code: str) -> dict:
        """Exchange authorization code for tokens (design doc section 4b)."""
        resp = await client.post(
            TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "code": code,
                "state": self._state,
                "client_id": CLIENT_ID,
                "code_verifier": self._verifier,
                "redirect_uri": self._redirect_uri,
            },
            headers={
                "Content-Type": "application/json",
                "anthropic-beta": OAUTH_BETA_HEADER,
            },
        )
        if resp.status_code != 200:
            # Truncate response body to avoid logging sensitive data
            body_preview = (resp.text or "")[:200]
            logger.error(f"Token exchange HTTP {resp.status_code}: {body_preview}")
            resp.raise_for_status()

        tokens = resp.json()
        logger.info(
            f"Token exchange successful: expires_in={tokens.get('expires_in')}, "
            f"has_refresh={bool(tokens.get('refresh_token'))}"
        )

        # Extract account metadata from response (design doc section 4b)
        account_data = tokens.get("account", {})
        if account_data.get("email_address"):
            tokens["email"] = account_data["email_address"]
        if account_data.get("subscriptionType"):
            tokens["subscription_type"] = account_data["subscriptionType"]
        if account_data.get("rateLimitTier"):
            tokens["rate_limit_tier"] = account_data["rateLimitTier"]

        # Extract organization data from token response.
        # Normalize: None/"" → sentinel "" for DB storage.
        org_data = tokens.get("organization", {})
        tokens["organization_uuid"] = org_data.get("uuid") or ""
        tokens["organization_name"] = org_data.get("name") or None

        # Store scopes as JSON array
        if tokens.get("scope"):
            tokens["scopes"] = tokens["scope"].split()

        return tokens

    async def _create_api_key(
        self, client: httpx.AsyncClient, access_token: str, tokens: dict
    ) -> tuple[str, dict]:
        """Create long-lived API key (design doc section 4c).

        Returns (new_access_token, updated_tokens).
        """
        try:
            resp = await client.post(
                API_KEY_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={},
            )
            if resp.status_code == 200:
                api_key_data = resp.json()
                if api_key_data.get("api_key"):
                    # CRITICAL side effects per design doc:
                    tokens["access_token"] = api_key_data["api_key"]
                    tokens["expires_in"] = 31536000  # 1 year
                    tokens["refresh_token"] = None  # API keys can't refresh
                    logger.info("Created long-lived API key (1 year)")
                    return api_key_data["api_key"], tokens
            else:
                body_preview = (resp.text or "")[:200]
                logger.warning(
                    f"API key creation HTTP {resp.status_code}: {body_preview}"
                )
        except Exception as e:
            logger.warning(f"API key creation failed: {e} — using short-lived token")

        return access_token, tokens

    async def _fetch_profile(
        self, client: httpx.AsyncClient, access_token: str
    ) -> dict:
        """Fetch profile data (design doc section 4e)."""
        try:
            resp = await client.get(
                PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Profile fetch HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Profile fetch failed: {e}")
        return {}

    async def _fetch_usage(
        self, client: httpx.AsyncClient, access_token: str
    ) -> dict:
        """Fetch usage data (design doc section 4f)."""
        try:
            resp = await client.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"Usage fetch HTTP {resp.status_code}")
        except Exception as e:
            logger.warning(f"Usage fetch failed: {e}")
        return {}

    def _store_account(self, tokens: dict, profile: dict, usage: dict) -> dict:
        """Store account data in the database.

        Branches on self.purpose:
        - "primary": create/update account with primary tokens (existing behavior)
        - "claude_code": validate identity, update cc_* columns on existing account
        """
        if self.purpose == "claude_code":
            return self._store_cc_tokens(tokens, profile)

        return self._store_primary_account(tokens, profile, usage)

    def _store_primary_account(
        self, tokens: dict, profile: dict, usage: dict
    ) -> dict:
        """Store primary account tokens (original behavior)."""
        email = tokens.get("email", "unknown")
        expires_at = int(time.time()) + tokens.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)

        # Build scopes JSON
        scopes_json = None
        if tokens.get("scopes"):
            scopes_json = json.dumps(tokens["scopes"])

        # Extract profile data (design doc section 4e mapping)
        org = profile.get("organization", {})
        acct_info = profile.get("account", {})
        org_type = org.get("organization_type", "")
        subscription_type = ORG_TYPE_MAP.get(
            org_type, tokens.get("subscription_type")
        )
        rate_limit_tier = org.get(
            "rate_limit_tier", tokens.get("rate_limit_tier")
        )
        has_extra_usage = org.get("has_extra_usage_enabled", False)
        display_name = acct_info.get("display_name")

        final_org_uuid = tokens.get("organization_uuid", "")
        final_org_name = tokens.get("organization_name")

        if self._target_account_id:
            # RE-AUTH: Update existing account by ID.
            # Don't use create_account()'s email+org_uuid upsert — org_uuid
            # can change between OAuth sessions, creating duplicates.
            target = self.db.get_account(self._target_account_id)
            if not target:
                raise ValueError(
                    f"Re-auth target account {self._target_account_id} not found"
                )

            # Identity check: ensure the user logged in with the same Claude account
            if email.lower() != target["email"].lower():
                raise ValueError(
                    f"Re-auth email ({email}) does not match "
                    f"target account ({target['email']})"
                )

            # If org_uuid changed, check if there's already an active account
            # with the new (email, org_uuid). This happens when the user clicks
            # re-auth on Account A but authorizes Account B's org on Anthropic's
            # page. Update Account B instead of crashing with UNIQUE violation.
            old_org_uuid = target.get("organization_uuid", "")
            actual_target_id = self._target_account_id
            if final_org_uuid != old_org_uuid:
                existing = self.db.get_account_by_email(
                    email, final_org_uuid, provider="claude"
                )
                if existing:
                    # Redirect: update the matching account, not the target
                    actual_target_id = existing["id"]
                    logger.info(
                        "Re-auth org mismatch: target account %d (org %s) "
                        "but authorized org %s which matches account %d — "
                        "updating account %d instead",
                        self._target_account_id, old_org_uuid,
                        final_org_uuid, existing["id"], existing["id"],
                    )
                else:
                    # No active account for this org — clean up soft-deleted ghosts
                    self.db.hard_delete_duplicate(email, final_org_uuid)

            self.db.update_account(
                actual_target_id,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                expires_at=expires_at,
                scopes=scopes_json,
                organization_uuid=final_org_uuid,
                organization_name=final_org_name,
                subscription_type=subscription_type,
                rate_limit_tier=rate_limit_tier,
                has_extra_usage=has_extra_usage,
                is_active=True,
                consecutive_failures=0,
                validation_status="valid",
                last_validated_at=int(time.time()),
                last_error=None,
            )
            account = self.db.get_account(actual_target_id)
            if actual_target_id != self._target_account_id:
                account["_redirected_from"] = self._target_account_id
        else:
            # ADD: Normal create_account with email+org upsert
            account = self.db.create_account(
                email=email,
                access_token=tokens["access_token"],
                refresh_token=tokens.get("refresh_token"),
                expires_at=expires_at,
                display_name=display_name,
                scopes=scopes_json,
                subscription_type=subscription_type,
                rate_limit_tier=rate_limit_tier,
                has_extra_usage=has_extra_usage,
                organization_uuid=final_org_uuid,
                organization_name=final_org_name,
            )

            # Non-fatal: account already persisted by create_account above
            updated = self.db.update_account(
                account["id"],
                validation_status="valid",
                last_validated_at=int(time.time()),
            )
            if not updated:
                logger.warning(
                    f"Validation status update failed for account {account['id']}"
                )

        # Update usage cache if we got usage data
        five_hour = usage.get("five_hour", {})
        seven_day = usage.get("seven_day", {})
        if five_hour or seven_day:
            self.db.update_account_usage_cache(
                account["id"],
                five_hour=five_hour.get("utilization"),
                seven_day=seven_day.get("utilization"),
                five_hour_resets_at=five_hour.get("resets_at"),
                seven_day_resets_at=seven_day.get("resets_at"),
                raw=usage,
            )

        logger.info(f"Account stored: {email} (id={account['id']})")
        return account

    def _store_cc_tokens(self, tokens: dict, profile: dict) -> dict:
        """Store CC (Claude Code) tokens on an existing account.

        Identity validation: the CC OAuth flow may authenticate a different
        Claude/email account than the target. We verify the email matches.
        """
        account_id = self._target_account_id
        target = self.db.get_account(account_id)
        if not target:
            raise ValueError(f"Target account {account_id} not found")

        # Identity validation — try token response first, fall back to profile.
        # CC flows skip API key creation, so _fetch_profile may fail with
        # the raw OAuth access token (returns {}). The token exchange response
        # already contains the email in tokens["email"] from the account field.
        cc_email = (
            tokens.get("email")
            or profile.get("account", {}).get("email_address")
        )
        if not cc_email:
            raise RuntimeError(
                "Cannot verify CC token identity — no email in token response or profile"
            )
        if cc_email.lower() != target["email"].lower():
            raise ValueError(
                f"CC auth email ({cc_email}) does not match "
                f"target account ({target['email']})"
            )

        # Org validation — fail closed: if target has a real org, CC must match it
        target_org = target.get("organization_uuid", "")
        cc_org = tokens.get("organization_uuid", "")
        if target_org:  # non-empty sentinel = real org
            if not cc_org:
                raise ValueError(
                    "CC auth missing org — cannot verify against org-scoped account "
                    f"(target org: {target_org})"
                )
            if cc_org != target_org:
                raise ValueError(
                    f"CC auth org ({cc_org}) does not match "
                    f"target account org ({target_org})"
                )

        expires_at = int(time.time()) + tokens.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)

        # Fatal: CC tokens are the sole deliverable of this flow
        updated = self.db.update_account(
            account_id,
            cc_access_token=tokens["access_token"],
            cc_refresh_token=tokens.get("refresh_token"),
            cc_expires_at=expires_at,
            validation_status="valid",
            last_validated_at=int(time.time()),
        )
        if not updated:
            raise RuntimeError(
                f"CC token update failed: account {account_id} — 0 rows affected"
            )

        logger.info(f"CC tokens stored for account {account_id} ({cc_email})")
        # Return the full account row so credential sync works
        return self.db.get_account(account_id)
