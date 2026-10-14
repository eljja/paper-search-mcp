"""Optional OAuth resource-server adapter; legacy transports remain unchanged.

An external authorization server owns login, consent, PKCE, code/refresh grants
and its discovery document. No passwords, OAuth client secrets or issued tokens
are stored here. Only non-writing tools are exposed through this adapter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import get_env

READ_SCOPE = "papers:read"
MAX_TOKEN_BYTES = 16384
MAX_JWKS_BYTES = 65536


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("OAuth URLs must be absolute HTTPS URLs without credentials, query or fragment")
    return value


@dataclass(frozen=True)
class OAuthConfig:
    resource: str
    issuer: str
    jwks_url: str
    subjects: frozenset[str]
    clients: frozenset[str]

    def __post_init__(self):
        for value in (self.resource, self.issuer, self.jwks_url):
            _https_url(value)
        if not urlsplit(self.resource).path.strip("/") or self.resource.endswith("/"):
            raise ValueError("resource must have a nonempty path without a trailing slash")
        if not self.subjects or not self.clients:
            raise ValueError("explicit OAuth subject and client allowlists are required")

    @property
    def path(self) -> str:
        return urlsplit(self.resource).path

    @property
    def metadata_path(self) -> str:
        return "/.well-known/oauth-protected-resource" + self.path

    @property
    def metadata_url(self) -> str:
        parsed = urlsplit(self.resource)
        return f"{parsed.scheme}://{parsed.netloc}{self.metadata_path}"

    @classmethod
    def from_env(cls):
        def required(name):
            value = get_env("OAUTH_" + name).strip()
            if not value:
                raise ValueError(f"PAPER_SEARCH_MCP_OAUTH_{name} is required")
            return value
        def allowlist(name):
            return frozenset(x.strip() for x in required(name).split(",") if x.strip())
        return cls(required("RESOURCE"), required("ISSUER"), required("JWKS_URL"),
                   allowlist("SUBJECTS"), allowlist("CLIENTS"))


class JWTVerifier:
    """RS256 only, fixed operator-configured JWKS; never follow token jku/x5u.

    Cache keys for five minutes; unknown kid refreshes at most once per thirty
    seconds. Failed refresh fails closed, including when a stale key is cached.
    """
    def __init__(self, config: OAuthConfig, http: httpx.AsyncClient):
        self.config = config
        self.http = http
        self.keys = {}
        self.fetched_at = float("-inf")
        self.attempted_at = float("-inf")
        self.lock = asyncio.Lock()

    async def _key(self, kid: str):
        async with self.lock:
            now = time.monotonic()
            fresh = now - self.fetched_at < 300
            if fresh and kid in self.keys:
                return self.keys[kid]
            if now - self.attempted_at < 30:
                return self.keys.get(kid) if fresh else None
            self.attempted_at = now
            async with self.http.stream("GET", self.config.jwks_url,
                                        timeout=5, follow_redirects=False) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        raise ValueError("JWKS response too large")
            document = json.loads(body)
            if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
                raise ValueError("JWKS must contain a keys array")
            keys = {}
            for entry in document["keys"]:
                if not isinstance(entry, dict):
                    raise ValueError("JWKS key must be an object")
                if (entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig"
                        or entry.get("alg", "RS256") != "RS256"
                        or "verify" not in entry.get("key_ops", ["verify"])):
                    continue
                key_id = entry.get("kid")
                if not isinstance(key_id, str) or not key_id or key_id in keys:
                    raise ValueError("JWKS requires unique nonempty key IDs")
                keys[key_id] = jwt.PyJWK.from_dict(entry, algorithm="RS256").key
            self.keys = keys
            self.fetched_at = time.monotonic()
            return keys.get(kid)

    async def verify(self, token: str) -> dict | None:
        if len(token) > MAX_TOKEN_BYTES:
            return None
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str)
                    or not kid or len(kid) > 256 or header.get("crit")):
                return None
            key = await self._key(kid)
            if key is None:
                return None
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                issuer=self.config.issuer, audience=self.config.resource,
                                options={"require": ["iss", "aud", "exp", "iat", "sub"]})
            if (not isinstance(claims["sub"], str) or not claims["sub"]
                    or not isinstance(claims.get("scope", ""), str)):
                return None
            if any(type(claims[field]) is not int for field in ("exp", "iat")):
                return None
            if "nbf" in claims and type(claims["nbf"]) is not int:
                return None
            if claims["exp"] <= claims["iat"]:
                return None
            return claims
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
            # Do not log tokens, headers, response bodies or exception strings.
            return None


class OAuthGuard:
    def __init__(self, app, config: OAuthConfig, verifier: JWTVerifier):
        self.app, self.config, self.verifier = app, config, verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [v.decode("latin1") for k, v in scope["headers"]
                   if k.lower() == b"authorization"]
        claims = None
        if len(headers) == 1:
            parts = headers[0].split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                claims = await self.verifier.verify(parts[1])
        error, status = "invalid_token", 401
        if claims is not None:
            client = claims.get("azp", claims.get("client_id"))
            if (not isinstance(client, str) or claims["sub"] not in self.config.subjects
                    or client not in self.config.clients):
                error, status = "access_denied", 403
            elif READ_SCOPE not in claims.get("scope", "").split():
                error, status = "insufficient_scope", 403
            else:
                # No tokens/claims are passed to paper providers or tool arguments.
                await self.app(scope, receive, send)
                return
        # RFC 6750 defines invalid_token and insufficient_scope, but not
        # access_denied. Keep an allowlist denial in JSON without inventing a
        # Bearer error code (and without exposing which principal was blocked).
        error_part = f'error="{error}", ' if error != "access_denied" else ""
        challenge = (f'Bearer {error_part}'
                     f'resource_metadata="{self.config.metadata_url}", scope="{READ_SCOPE}"')
        response = JSONResponse({"error": error}, status_code=status,
                                headers={"WWW-Authenticate": challenge, "Cache-Control": "no-store"})
        await response(scope, receive, send)


async def create_app(config: OAuthConfig, backend=None, http=None) -> Starlette:
    if backend is None:
        from .server import mcp as backend
    owned_http = http is None
    http = http or httpx.AsyncClient(timeout=5, follow_redirects=False)
    verifier = JWTVerifier(config, http)
    try:
        tools = {
            tool.name: tool.model_copy(update={
                "meta": {**(tool.meta or {}), "securitySchemes": [
                    {"type": "oauth2", "scopes": [READ_SCOPE]}
                ]},
                "securitySchemes": [{"type": "oauth2", "scopes": [READ_SCOPE]}],
            })
            for tool in await backend.list_tools()
            if tool.annotations is not None and tool.annotations.readOnlyHint is True
        }
    except BaseException:
        if owned_http:
            await http.aclose()
        raise
    gateway = Server("paper_search_oauth", instructions=(
        "Search and look up academic papers using the available read-only tools. "
        "This endpoint requires papers:read. Downloads and local file writes use the legacy client."
    ))

    @gateway.list_tools()
    async def list_tools():
        return list(tools.values())

    @gateway.call_tool()
    async def call_tool(name, arguments):
        if name not in tools:
            return CallToolResult(isError=True, content=[
                TextContent(type="text", text="Tool is not available on this endpoint")])
        return await backend.call_tool(name, arguments)

    parsed = urlsplit(config.resource)
    manager = StreamableHTTPSessionManager(
        gateway, stateless=True, json_response=True, max_request_body_size=1024 * 1024,
        security_settings=TransportSecuritySettings(
            allowed_hosts=[parsed.netloc],
            allowed_origins=[f"https://{parsed.netloc}", "https://chatgpt.com"],
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        try:
            async with manager.run():
                yield
        finally:
            if owned_http:
                await http.aclose()

    async def metadata(request: Request):
        return JSONResponse({
            "resource": config.resource,
            "authorization_servers": [config.issuer],
            "scopes_supported": [READ_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Paper Search MCP (read-only)",
        }, headers={"Cache-Control": "public, max-age=300"})

    app = Starlette(routes=[
        Route(config.metadata_path, metadata, methods=["GET"]),
        Route(config.path, OAuthGuard(manager.handle_request, config, verifier),
              methods=["GET", "POST", "DELETE"]),
    ], lifespan=lifespan)
    app.state.verifier = verifier
    return app


async def check_provider(config: OAuthConfig):
    """Read-only discovery check; does not claim a real OAuth login succeeded."""
    issuer = config.issuer.rstrip("/")
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
        response = await http.get(issuer + "/.well-known/openid-configuration")
        response.raise_for_status()
        metadata = response.json()
        if metadata.get("issuer") != config.issuer:
            raise ValueError("issuer discovery mismatch")
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise ValueError("provider must advertise S256 PKCE")
        if "code" not in metadata.get("response_types_supported", []):
            raise ValueError("provider must advertise authorization code flow")
        if not set(metadata.get("token_endpoint_auth_methods_supported", [])).intersection(
                {"none", "client_secret_basic", "client_secret_post"}):
            raise ValueError("provider must advertise a supported static-client token auth method")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            _https_url(metadata[field])
        if metadata["jwks_uri"] != config.jwks_url:
            raise ValueError("configured JWKS URL differs from provider discovery")
    print("Provider discovery passed; PKCE execution, resource binding and account linking remain unverified.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="OAuth-protected, read-only Paper Search MCP")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--check-provider", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = OAuthConfig.from_env()
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
    except ValueError as exc:
        parser.error(str(exc))
    if args.check_provider:
        asyncio.run(check_provider(config))
        return
    import uvicorn

    async def serve():
        app = await create_app(config)
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False))
        await server.serve()
    asyncio.run(serve())


if __name__ == "__main__":
    main()
