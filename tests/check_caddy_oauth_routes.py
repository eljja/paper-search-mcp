"""Validate Caddy's *adapted* route ordering, rather than source text order."""
import json
import sys

config = json.load(open(sys.argv[1]))
found = {}

def scan(value):
    if isinstance(value, dict):
        routes = value.get('routes', [])
        for index, route in enumerate(routes):
            for matcher in route.get('match', []):
                for path in matcher.get('path', []):
                    if path in (
                        '/mcp/paper-search/oauth',
                        '/.well-known/oauth-protected-resource/mcp/paper-search/oauth',
                        '/mcp/paper-search*',
                    ):
                        found[path] = (index, route)
        for child in value.values():
            scan(child)
    elif isinstance(value, list):
        for child in value:
            scan(child)

scan(config)
oauth_index, oauth = found['/mcp/paper-search/oauth']
_, discovery = found['/.well-known/oauth-protected-resource/mcp/paper-search/oauth']
legacy_index, legacy = found['/mcp/paper-search*']
assert oauth_index < legacy_index, 'OAuth route is shadowed by legacy Basic matcher'
assert 'authentication' not in json.dumps(oauth)
assert 'authentication' not in json.dumps(discovery)
assert '127.0.0.1:8766' in json.dumps(oauth)
assert 'authentication' in json.dumps(legacy)
assert '127.0.0.1:8765' in json.dumps(legacy)
print('Caddy OAuth/discovery routing and legacy Basic protection passed')
