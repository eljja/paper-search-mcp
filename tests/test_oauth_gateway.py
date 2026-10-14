"""Offline HTTP/crypto tests. No production credentials or provider requests."""
import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp.types import Tool, ToolAnnotations
from starlette.testclient import TestClient

from paper_search_mcp.oauth_gateway import OAuthConfig, create_app, check_provider

RESOURCE = 'https://mcp.example.com/mcp/paper-search/oauth'
ISSUER = 'https://auth.example.com/'
JWKS = ISSUER + 'jwks'


@pytest.fixture(scope='module')
def keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, other


def jwk(private, kid='key1'):
    return {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key())),
            'kid': kid, 'alg': 'RS256', 'use': 'sig'}


def token(keys, **changes):
    now = int(time.time())
    claims = dict(iss=ISSUER, aud=RESOURCE, exp=now + 300, iat=now,
                  sub='owner', azp='chatgpt-client', scope='papers:read')
    claims.update(changes)
    return jwt.encode(claims, keys[0], algorithm='RS256', headers={'kid': 'key1'})


def config():
    return OAuthConfig(RESOURCE, ISSUER, JWKS, frozenset({'owner'}), frozenset({'chatgpt-client'}))


@pytest.fixture
def setup(keys):
    backend = AsyncMock()
    backend.list_tools.return_value = [
        Tool(name='search_test', inputSchema={'type': 'object', 'properties': {'query': {'type': 'string'}},
                                             'required': ['query'], 'additionalProperties': False},
             outputSchema={'type': 'object', 'properties': {'papers': {'type': 'array'}}},
             annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True)),
        Tool(name='download_test', inputSchema={'type': 'object'},
             annotations=ToolAnnotations(readOnlyHint=False)),
        Tool(name='unannotated', inputSchema={'type': 'object'}),
    ]
    backend.call_tool.return_value = {'papers': []}
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'keys': [jwk(keys[0])]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    app = asyncio.run(create_app(config(), backend=backend, http=http))
    with TestClient(app, base_url='https://mcp.example.com') as client:
        yield client, backend, calls, app
    asyncio.run(http.aclose())


def rpc(client, auth=None, method='tools/list', params=None, **kwargs):
    headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-06-18'}
    if auth:
        headers['Authorization'] = auth
    headers.update(kwargs.pop('headers', {}))
    return client.post('/mcp/paper-search/oauth', headers=headers,
                       json={'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}, **kwargs)


def test_discovery_is_public_and_canonical(setup):
    client, backend, calls, app = setup
    response = client.get(config().metadata_path, headers={'Host': 'attacker.example'})
    assert response.status_code == 200
    assert response.json()['resource'] == RESOURCE
    assert response.json()['authorization_servers'] == [ISSUER]
    assert response.json()['scopes_supported'] == ['papers:read']
    assert not calls
    backend.call_tool.assert_not_awaited()


@pytest.mark.parametrize('auth', [None, 'Basic Zm9vOmJhcg==', 'Bearer', 'Bearer garbage', 'Bearer ' + 'a' * 17000])
def test_missing_basic_malformed_tokens_are_rejected(setup, auth):
    client, backend, calls, app = setup
    response = rpc(client, auth)
    assert response.status_code == 401
    assert response.json() == {'error': 'invalid_token'}
    assert config().metadata_url in response.headers['WWW-Authenticate']
    assert 'scope="papers:read"' in response.headers['WWW-Authenticate']
    assert response.headers['cache-control'] == 'no-store'
    backend.call_tool.assert_not_awaited()
    assert not calls


@pytest.mark.parametrize('changes', [
    {'iss': 'https://other.example/'}, {'aud': 'https://other.example/mcp'},
    {'exp': 1}, {'iat': int(time.time()) + 3600}, {'nbf': int(time.time()) + 3600},
    {'sub': ''}, {'scope': ['papers:read']},
])
def test_invalid_claims(setup, keys, changes):
    response = rpc(setup[0], 'Bearer ' + token(keys, **changes))
    assert response.status_code == 401
    setup[1].call_tool.assert_not_awaited()


@pytest.mark.parametrize('missing', ['exp', 'iat', 'sub', 'aud', 'iss'])
def test_required_claims(setup, keys, missing):
    claims = jwt.decode(token(keys), options={'verify_signature': False})
    del claims[missing]
    signed = jwt.encode(claims, keys[0], algorithm='RS256', headers={'kid': 'key1'})
    assert rpc(setup[0], 'Bearer ' + signed).status_code == 401


@pytest.mark.parametrize('changes,error', [
    ({'scope': ''}, 'insufficient_scope'), ({'scope': 'papers:reader'}, 'insufficient_scope'),
    ({'sub': 'other'}, 'access_denied'), ({'azp': 'other'}, 'access_denied'),
    ({'azp': ['chatgpt-client']}, 'access_denied'),
])
def test_subject_client_and_scope_restrictions(setup, keys, changes, error):
    response = rpc(setup[0], 'Bearer ' + token(keys, **changes))
    assert response.status_code == 403
    assert response.json()['error'] == error
    setup[1].call_tool.assert_not_awaited()


def test_valid_initialize_list_call_and_metadata(setup, keys):
    client, backend, calls, app = setup
    auth = 'bEaReR ' + token(keys)
    initialized = rpc(client, auth, 'initialize', {'protocolVersion': '2025-06-18',
                                                 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}})
    assert initialized.status_code == 200
    assert initialized.json()['result']['protocolVersion'] == '2025-06-18'
    listed = rpc(client, auth).json()['result']['tools']
    assert [x['name'] for x in listed] == ['search_test']
    scheme = [{'type': 'oauth2', 'scopes': ['papers:read']}]
    assert listed[0]['securitySchemes'] == scheme
    assert listed[0]['_meta']['securitySchemes'] == scheme
    result = rpc(client, auth, 'tools/call', {'name': 'search_test', 'arguments': {'query': 'flash memory'}})
    assert result.status_code == 200
    assert result.json()['result']['structuredContent'] == {'papers': []}
    backend.call_tool.assert_awaited_once_with('search_test', {'query': 'flash memory'})
    assert len(calls) == 1
    assert all('authorization' not in request.headers for request in calls)


@pytest.mark.parametrize('name', ['download_test', 'unannotated', 'nonexistent'])
def test_hidden_tool_cannot_be_called_directly(setup, keys, name):
    response = rpc(setup[0], 'Bearer ' + token(keys), 'tools/call', {'name': name, 'arguments': {}})
    assert response.json()['result']['isError'] is True
    setup[1].call_tool.assert_not_awaited()


def test_invalid_tool_arguments_do_not_execute(setup, keys):
    response = rpc(setup[0], 'Bearer ' + token(keys), 'tools/call', {'name': 'search_test', 'arguments': {'query': 42}})
    assert response.json()['result']['isError'] is True
    setup[1].call_tool.assert_not_awaited()


@pytest.mark.parametrize('headers,status', [({'Host': 'evil.example'}, 421), ({'Origin': 'https://evil.example'}, 403)])
def test_host_origin_checks(setup, keys, headers, status):
    assert rpc(setup[0], 'Bearer ' + token(keys), headers=headers).status_code == status


def test_query_token_does_not_authenticate(setup, keys):
    response = setup[0].post(config().path + '?access_token=' + token(keys), json={})
    assert response.status_code == 401


def test_duplicate_authorization_rejected(setup, keys):
    response = setup[0].post(config().path, headers=[('Authorization', 'Bearer ' + token(keys)),
                                                   ('Authorization', 'Bearer ' + token(keys))], json={})
    assert response.status_code == 401


def test_signature_algorithm_confusion_and_untrusted_jku(setup, keys):
    claims = jwt.decode(token(keys), options={'verify_signature': False})
    for alg, key in [('none', None), ('HS256', 'offline-test-only-key-at-least-32-bytes'), ('RS256', keys[1])]:
        signed = jwt.encode(claims, key, algorithm=alg,
                            headers={'kid': 'key1', 'jku': 'https://attacker.example/keys'})
        assert rpc(setup[0], 'Bearer ' + signed).status_code == 401
    assert all(str(request.url) == JWKS for request in setup[2])


def test_unknown_kid_refresh_is_bounded(setup, keys):
    signed = token(keys)
    assert rpc(setup[0], 'Bearer ' + signed).status_code == 200
    claims = jwt.decode(signed, options={'verify_signature': False})
    for n in range(10):
        signed = jwt.encode(claims, keys[0], algorithm='RS256', headers={'kid': 'unknown' + str(n)})
        assert rpc(setup[0], 'Bearer ' + signed).status_code == 401
    assert len(setup[2]) == 1


def test_key_rotation_and_expired_cache_outage(keys):
    responses = [{'keys': [jwk(keys[0])]}, {'keys': [jwk(keys[1], 'key2')]}, None]
    def handle(request):
        data = responses.pop(0)
        return httpx.Response(503) if data is None else httpx.Response(200, json=data)
    async def run():
        from paper_search_mcp.oauth_gateway import JWTVerifier
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            verifier = JWTVerifier(config(), http)
            assert await verifier.verify(token(keys))
            verifier.attempted_at -= 31
            claims = jwt.decode(token(keys), options={'verify_signature': False})
            rotated = jwt.encode(claims, keys[1], algorithm='RS256', headers={'kid': 'key2'})
            assert await verifier.verify(rotated)
            assert await verifier.verify(token(keys)) is None
            verifier.fetched_at -= 301
            verifier.attempted_at -= 31
            assert await verifier.verify(rotated) is None
            assert await verifier.verify(rotated) is None
    asyncio.run(run())


@pytest.mark.parametrize('response', [
    httpx.Response(302, headers={'Location': 'https://attacker.example/keys'}),
    httpx.Response(503), httpx.Response(200, content='not json'),
    httpx.Response(200, json={'keys': []}), httpx.Response(200, content=b'x' * 65537),
])
def test_bad_jwks_fails_closed(keys, response):
    async def run():
        from paper_search_mcp.oauth_gateway import JWTVerifier
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
            assert await JWTVerifier(config(), http).verify(token(keys)) is None
    asyncio.run(run())


@pytest.mark.parametrize('url', ['http://auth.example/', 'https://u:p@auth.example/', 'https://auth.example/?key=x'])
def test_unsafe_config(url):
    with pytest.raises(ValueError):
        OAuthConfig(RESOURCE, url, JWKS, frozenset({'owner'}), frozenset({'client'}))


def test_empty_allowlists_fail_closed():
    with pytest.raises(ValueError):
        OAuthConfig(RESOURCE, ISSUER, JWKS, frozenset(), frozenset({'client'}))


def test_real_backend_tools_only_exports_nonwriting_tools():
    from paper_search_mcp.server import mcp
    async def run():
        async with httpx.AsyncClient() as http:
            app = await create_app(config(), backend=mcp, http=http)
            # Test the real descriptors through HTTP, with only JWT verification replaced.
            app.state.verifier.verify = AsyncMock(return_value={'sub': 'owner', 'azp': 'chatgpt-client', 'scope': 'papers:read'})
            with TestClient(app, base_url='https://mcp.example.com') as client:
                tools = rpc(client, 'Bearer test').json()['result']['tools']
            all_tools = await mcp.list_tools()
            expected = {t.name for t in all_tools if t.annotations and t.annotations.readOnlyHint is True}
            assert {t['name'] for t in tools} == expected
            assert 'search_papers' in expected
            assert 'download_with_fallback' not in expected
            assert 'read_arxiv_paper' not in expected
    asyncio.run(run())


def test_sdk_client_streamable_http_handshake(keys):
    """Real MCP SDK client negotiation over HTTP ASGI transport with signed JWT."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    async def run():
        backend = AsyncMock()
        backend.list_tools.return_value = [Tool(name='search_test', inputSchema={'type': 'object'},
                                               annotations=ToolAnnotations(readOnlyHint=True))]
        backend.call_tool.return_value = {'papers': []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={'keys': [jwk(keys[0])]}))) as jwks_http:
            app = await create_app(config(), backend=backend, http=jwks_http)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             headers={'Authorization': 'Bearer ' + token(keys)}) as client:
                    async with streamable_http_client(RESOURCE, http_client=client) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            initialized = await session.initialize()
                            assert initialized.serverInfo.name == 'paper_search_oauth'
                            listed = await session.list_tools()
                            assert [t.name for t in listed.tools] == ['search_test']
                            result = await session.call_tool('search_test', {})
                            assert result.isError is False
                            assert result.structuredContent == {'papers': []}
                            # Backend JWT is never added to arguments.
                            backend.call_tool.assert_awaited_once_with('search_test', {})
    asyncio.run(run())


def test_provider_preflight_and_pkce_rejection(monkeypatch, capsys):
    good = dict(issuer=ISSUER, authorization_endpoint=ISSUER + 'authorize',
                token_endpoint=ISSUER + 'token', jwks_uri=JWKS,
                code_challenge_methods_supported=['S256'], response_types_supported=['code'],
                token_endpoint_auth_methods_supported=['none'])
    real_client = httpx.AsyncClient
    def run_with(metadata):
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real_client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=metadata)), **kw))
        asyncio.run(check_provider(config()))
    run_with(good)
    assert 'remain unverified' in capsys.readouterr().out
    for changes in [{'issuer': 'https://evil.example/'}, {'code_challenge_methods_supported': ['plain']},
                    {'jwks_uri': 'https://evil.example/keys'}, {'token_endpoint': 'http://auth.example/token'},
                    {'token_endpoint_auth_methods_supported': ['client_secret_jwt']}]:
        with pytest.raises(ValueError):
            run_with({**good, **changes})


@pytest.mark.parametrize('response', [
    httpx.Response(200, json=[]), httpx.Response(200, json={'keys': ['invalid']}),
    httpx.Response(200, json={'keys': {}}),
])
def test_invalid_jwks_shapes_fail_closed(keys, response):
    async def run():
        from paper_search_mcp.oauth_gateway import JWTVerifier
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as http:
            assert await JWTVerifier(config(), http).verify(token(keys)) is None
    asyncio.run(run())


def test_request_body_limit(setup, keys):
    response = setup[0].post(config().path, headers={
        'Authorization': 'Bearer ' + token(keys), 'Accept': 'application/json, text/event-stream',
        'Content-Type': 'application/json'}, content=b'x' * (1024 * 1024 + 1))
    assert response.status_code == 413
    setup[1].call_tool.assert_not_awaited()
