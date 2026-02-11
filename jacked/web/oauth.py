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
import json
import logging
import secrets
import time
import webbrowser
from typing import Optional
from urllib.parse import urlencode

import httpx
from aiohttp import web

from jacked.web.database import Database

logger = logging.getLogger("jacked.oauth")

# ---------------------------------------------------------------------------
# Constants — from design doc section 5
# ---------------------------------------------------------------------------

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
API_KEY_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CALLBACK_PORT_RANGE = range(45100, 45200)

# organization_type → subscription_type mapping (design doc section 4e)
ORG_TYPE_MAP = {
    "claude_max": "max",
    "claude_pro": "pro",
    "claude_enterprise": "enterprise",
    "claude_team": "team",
}


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

    def __init__(self, db: Database):
        self.db = db
        self.flow_id = secrets.token_urlsafe(16)
        self._verifier: Optional[str] = None
        self._state: Optional[str] = None
        self._redirect_uri: Optional[str] = None
        self._status = "pending"  # pending | completed | error
        self._result: Optional[dict] = None
        self._error: Optional[str] = None
        self._event = asyncio.Event()
        self._created_at = time.time()

    def get_status(self) -> dict:
        """Get current flow status for polling.

        >>> db = Database(":memory:")
        >>> flow = OAuthFlow(db)
        >>> flow.get_status()["status"]
        'pending'
        """
        # 2-minute timeout
        if self._status == "pending" and time.time() - self._created_at > 120:
            self._status = "not_found"

        result: dict = {"status": self._status, "flow_id": self.flow_id}
        if self._result:
            result["account_id"] = self._result.get("account_id")
            result["email"] = self._result.get("email")
        if self._error:
            result["error"] = self._error
        return result

    async def start(self) -> dict:
        """Start the OAuth flow: spin up callback server, open browser.

        Returns dict with flow_id and auth_url for the frontend.
        """
        self._verifier, challenge = generate_pkce()
        self._state = secrets.token_urlsafe(32)

        # Register this flow globally
        _active_flows[self.flow_id] = self

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
        auth_url = f"{AUTH_URL}?{urlencode(params)}"

        # Open browser
        webbrowser.open(auth_url)
        logger.info("Opened browser for OAuth authorization")

        # Wait for callback in background — don't block the API response
        asyncio.create_task(self._wait_for_callback(runner))

        return {"flow_id": self.flow_id, "auth_url": auth_url}

    async def _wait_for_callback(self, runner: web.AppRunner) -> None:
        """Wait for the callback, then clean up the server."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=120)
        except asyncio.TimeoutError:
            self._status = "not_found"
            self._error = "OAuth flow timed out (2 minutes)"
        finally:
            await runner.cleanup()
            # Clean up from global registry after a delay
            await asyncio.sleep(30)
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

        # CSRF check
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
                logger.error(f"OAuth completion failed: {e}")
                self._status = "error"
                self._error = str(e)

        self._event.set()
        return web.Response(
            text="<h1>Success!</h1><p>You can close this window.</p><script>window.close()</script>",
            content_type="text/html",
        )

    async def _complete_auth(self, code: str) -> dict:
        """Complete the OAuth flow: token exchange, API key, profile, usage, DB store."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Step 1: Exchange code for tokens
            tokens = await self._exchange_code(client, code)

            access_token = tokens["access_token"]

            # Step 2: Optionally create API key (changes token lifecycle)
            if "org:create_api_key" in tokens.get("scope", ""):
                access_token, tokens = await self._create_api_key(
                    client, access_token, tokens
                )

            # Step 3: Fetch profile
            profile = await self._fetch_profile(client, access_token)

            # Step 4: Fetch usage
            usage = await self._fetch_usage(client, access_token)

            # Step 5: Store in database
            account = self._store_account(tokens, profile, usage)

            return {
                "account_id": account.get("id"),
                "email": account.get("email"),
            }

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
            logger.error(f"Token exchange HTTP {resp.status_code}: {resp.text}")
            resp.raise_for_status()

        tokens = resp.json()
        logger.info(
            f"Token exchange successful: expires_in={tokens.get('expires_in')}"
        )

        # Extract account metadata from response (design doc section 4b)
        account_data = tokens.get("account", {})
        if account_data.get("email_address"):
            tokens["email"] = account_data["email_address"]
        if account_data.get("subscriptionType"):
            tokens["subscription_type"] = account_data["subscriptionType"]
        if account_data.get("rateLimitTier"):
            tokens["rate_limit_tier"] = account_data["rateLimitTier"]

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
                logger.warning(
                    f"API key creation HTTP {resp.status_code}: {resp.text}"
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
        """Store account data in the database."""
        email = tokens.get("email", "unknown")
        expires_at = int(time.time()) + tokens.get("expires_in", 28800)

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

        # Create/update account
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
            )

        # Mark as valid
        self.db.update_account(
            account["id"],
            validation_status="valid",
            last_validated_at=int(time.time()),
        )

        logger.info(f"Account stored: {email} (id={account['id']})")
        return account
