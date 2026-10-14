# ChatGPT 연결: 인증 호환성, OAuth 배포와 검증

확인 기준: 2026-10-14. 저장소 기준 커밋: `824224fcf00f4d9792f6a8d7dc94ffc4ef7d448d`.

## 1. Basic Auth로 직접 연결할 수 있는가?

ChatGPT의 **사용자 정의 MCP 연결** 화면은 OAuth, No authentication,
OAuth or no authentication을 제공한다. Basic 사용자명/비밀번호나 임의
Authorization 헤더를 입력하는 방식은 공식 연결 절차에 없다. 무인증을 선택해도
Caddy의 Basic 보호가 해제되지 않으며, URL에 비밀번호를 넣는 우회도 사용하지 않는다.
GPT Actions나 OpenAI API의 MCP `authorization` 필드는 이 연결 화면과 다른 기능이다.

이 저장소의 기존 `/mcp/paper-search` 경로는 Caddy가 Basic을 검사한 뒤
`127.0.0.1:8765/mcp`로 전달한다. stdio 및 SSE/Streamable HTTP 실행도 제공한다.
Basic 지원 클라이언트는 기존 구성 그대로 사용한다.

**운영 서버는 저장소와 다르다.** 같은 날 공개 URL을 자격 증명 없이 조회한 결과:

| 공개 경로 | 관찰한 응답 | 확인하지 못한 내용 |
| --- | --- | --- |
| `/mcp/paper-search` | 401, Bearer `resource_metadata` 및 Basic challenge | 유효한 Basic 로그인, 실제 Bearer 검증 코드 |
| `/.well-known/oauth-protected-resource/mcp/paper-search` | 200, resource=`https://openrhiza.com/mcp/paper-search`, issuer=`https://openrhiza.com`, scope=`paper-search` | 메타데이터와 실제 권한 검사의 일치 여부 |
| `/.well-known/oauth-authorization-server` | 200, authorize/token/register, code/refresh grants, S256, none/client_secret_post/client_secret_basic 광고 | PKCE 실행, redirect 검사, 코드 재사용 차단, refresh 회전 |

이미 배포된 OAuth 구현의 소스는 이 저장소에 없다. 이 관찰을 “Basic만 배포됨”이나
“ChatGPT 연결 완료”로 해석하면 안 된다. 배포 소스가 확보되고 안전성이 검증되면
현재 URL의 기존 OAuth를 그대로 이용할 수도 있다. 이 PR은 운영 설정을 수정하지
않으며, 다음의 별도 URL을 사용하는 검증 가능한 대안을 추가한다.

## 2. 선정한 최소 변경안

자체 로그인·토큰 발급 서버를 새로 만들지 않고, 외부 OAuth 제공자의 Authorization
Code + S256 PKCE 기능을 사용한다. 예시는 Auth0이며 같은 계약을 지원하는 다른
제공자도 사용할 수 있다. 서버 쪽에는 선택 설치형 **OAuth resource-server adapter**만
추가한다. 기존 Python 검색/조회 함수를 재사용하므로 검색 코어와 기존 전송 방식은
변경하지 않는다.

| 구성 | 역할 |
| --- | --- |
| ChatGPT | OAuth 클라이언트, 로그인 후 Bearer 토큰으로 MCP 호출 |
| 외부 OAuth 제공자 | 로그인, 동의, S256 PKCE, 코드/refresh 토큰 발급·폐기, authorization-server metadata/JWKS |
| Caddy | HTTPS, 기존 Basic 경로 유지, 새 OAuth/메타데이터 경로를 loopback 게이트웨이로 전달 |
| `paper-search-oauth` | JWT 서명·iss·aud·exp·iat·nbf 검사, 사용자/client allowlist, `papers:read`, 401/403, read-only 도구 목록 |

- 새 MCP URL: `https://openrhiza.com/mcp/paper-search/oauth`
- resource/audience: 위 URL과 **완전히 같은 값**, 끝의 `/` 없음
- 공개 보호 리소스 메타데이터:
  `https://openrhiza.com/.well-known/oauth-protected-resource/mcp/paper-search/oauth`
- 로컬 게이트웨이: `127.0.0.1:8766`, 외부 직접 노출 금지
- scope: `papers:read`
- JWT: RS256만 허용. Opaque 토큰, HS256, 무서명, 다른 audience는 거부
- 모든 MCP 요청이 인증 대상. Mixed 인증 모드는 이 구성에서 선택하지 않는다.
- 다운로드와 로컬 파일을 쓰는 `read_*` 도구는 새 경로에서 제외하며 직접 호출도 차단한다.
  기존 클라이언트의 다운로드 기능은 유지한다. 나중에 원격 다운로드가 필요하면 별도
  scope와 저장 경로 격리를 먼저 설계한다.

## 3. 외부 OAuth 제공자 설정 (Auth0 예시)

1. 개인 운영용 tenant에 API(Resource Server)를 만들고 Identifier를 새 MCP URL로
   설정한다. 서명 알고리즘은 RS256, permission은 `papers:read`로 설정한다.
2. **Resource Parameter Compatibility Profile**을 `compatibility`로 설정한다.
   MCP 클라이언트가 보내는 RFC 8707 `resource`를 API audience로 해석해야 한다.
   토큰이 `/userinfo`나 OAuth client ID를 audience로 가지면 게이트웨이는 거부한다.
3. ChatGPT용 OAuth 애플리케이션을 사전 등록한다. Authorization Code 및 S256 PKCE를
   사용하고 불필요한 password/client_credentials/implicit grant를 사용하지 않는다.
   이 최소 구성은 사전 등록 client ID를 사용하며, 자체 DCR/CIMD 서버를 추가하지 않는다.
4. 연결 관리 화면이 표시하는 **정확한 callback URL**을 제공자의 Allowed Callback URLs에
   등록한다. 화면에 따라 `https://chatgpt.com/connector/oauth/{callback_id}` 또는
   `https://chatgpt.com/connector_platform_oauth_redirect`일 수 있다. 하나를 추측하거나
   wildcard callback을 허용하지 않는다.
5. token endpoint의 인증 방법을 ChatGPT 연결 설정과 맞춘다. 제공자의 요구에 따라
   client secret이 필요한 경우 **ChatGPT의 전용 비밀 입력란에만** 넣는다. 이는 기존
   Basic 비밀번호와 다른 값이며, 저장소·MCP URL·일반 채팅·설정 예시에 기록하지 않는다.
6. `papers:read`는 지정 사용자에게만 부여하고 공개 가입을 제한한다. 제공자에서
   consent 및 callback 검사, S256 강제, authorization code 단회 사용·짧은 수명,
   refresh rotation/reuse detection을 설정한다. 제공자 설정과 실제 실행도 별도로 검증한다.
7. access token 수명은 짧게(예: 5~15분) 설정한다. 필요할 때만 refresh/offline access를
   제공자와 ChatGPT 양쪽에서 활성화한다. 코드에는 refresh token이 저장되지 않는다.
8. 제공자가 발급한 access token의 `sub`와 `azp`(또는 `client_id`)를 확인해 게이트웨이의
   allowlist에 지정한다. 여러 값은 쉼표로 구분한다. 토큰 자체를 복사해 저장하지 않는다.
   권한을 scope 대신 permissions 배열로만 발급하는 구성은 지원하지 않는다.
9. 제공자의 discovery가 issuer, HTTPS authorize/token/JWKS, S256, code flow를
   광고하는지 확인한다. JWKS 공개 주소와 issuer(끝 `/` 포함 여부)를 정확히 설정한다.

제공자가 RFC 8707 resource 바인딩을 지원하지 않으면 이 배포에 사용하지 않는다.
게이트웨이의 audience 검사를 완화해서 맞추지 않는다.

## 4. 설치와 배포

운영 서버의 기존 Caddy 설정과 배포 소스를 먼저 백업·확인한다. 특히 공개 서버에는
저장소 밖의 OAuth 경로가 있으므로 저장소 `Caddyfile`로 전체 설정을 덮어쓰지 않는다.
아래 user service의 `/home/eljja`와 프로젝트 경로는 실제 서버 경로에 맞춘다.

```bash
.venv/bin/python -m pip install -e '.[oauth]'
mkdir -p ~/.config/paper-search-mcp
install -m 600 /dev/null ~/.config/paper-search-mcp/oauth.env
```

위 파일을 서버에서 직접 편집한다. 아래는 모두 자리표시자이며 운영 비밀은 없다.
기존 provider API 키가 필요하면 같은 보호된 env 파일에 지정한다.

```dotenv
PAPER_SEARCH_MCP_OAUTH_RESOURCE=https://openrhiza.com/mcp/paper-search/oauth
PAPER_SEARCH_MCP_OAUTH_ISSUER=https://YOUR-TENANT.example.com/
PAPER_SEARCH_MCP_OAUTH_JWKS_URL=https://YOUR-TENANT.example.com/.well-known/jwks.json
PAPER_SEARCH_MCP_OAUTH_SUBJECTS=YOUR-PROVIDER-USER-SUBJECT
PAPER_SEARCH_MCP_OAUTH_CLIENTS=YOUR-REGISTERED-CHATGPT-CLIENT-ID
```

둘 중 한 allowlist라도 비면 시작하지 않는다. 파일 권한은 600으로 유지하고 env 파일은
커밋하지 않는다. JWT/access token/refresh token/사용자 비밀번호는 필요하지 않다.

```bash
PAPER_SEARCH_MCP_ENV_FILE="$HOME/.config/paper-search-mcp/oauth.env" .venv/bin/paper-search-oauth --check-provider
mkdir -p ~/.config/systemd/user
cp deploy/paper-search-oauth.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now paper-search-oauth.service
systemctl --user status paper-search-oauth.service
```

`--check-provider`는 OIDC discovery의 issuer, endpoints, S256, code 및 JWKS 주소를
읽기만 한다. 이는 사용자 로그인·토큰 발급·ChatGPT 연결 검증을 대체하지 않는다.

기존 site block 안에 `import oauth-enabled/*.caddy`를 추가한다(이 PR의 Caddyfile에는
이미 있다). OAuth 활성화 전 glob이 비면 기존 Basic 동작을 그대로 유지한다.

```bash
mkdir -p deploy/oauth-enabled
cp deploy/paper-search-oauth.caddy deploy/oauth-enabled/
caddy validate --config deploy/Caddyfile --adapter caddyfile
```

**실제로 운영하는** 전체 Caddy 설정에 신규 경로 두 개를 병합하고, 해당 설정 파일을
검증한 뒤 reload한다. service의 기존 ExecStartPost도 실제 운영 파일을 가리키도록
맞춰야 재시작 때 별도 OAuth 설정이 소실되지 않는다. 위 `deploy/Caddyfile`을 실제
설정으로 사용하는 운영에서만 다음을 실행한다. 기존 Basic hash 환경 변수는 해당
reload 프로세스에 그대로 제공하되 화면이나 기록에 출력하지 않는다.

```bash
caddy reload --config deploy/Caddyfile --adapter caddyfile
```

새 resource의 canonical hostname은 `openrhiza.com`이다. `www.openrhiza.com`을
ChatGPT URL로 입력하지 않는다. Caddy는 새 경로의 Host를 원본 그대로 전달한다.
게이트웨이는 resource hostname과 허용 Origin만 받는다. Uvicorn access log를 끄며
토큰 검증 실패의 예외·헤더·본문을 기록하지 않는다. 외부 로그 설정에서도
Authorization·query token·인증 제공자의 code/refresh token을 기록하지 않게 한다.

장애 시 새 fragment를 제거하고 실제 운영 Caddy 설정을 reload한다. 그 후 새 서비스만
중지하면 기존 Basic/stdio/SSE/HTTP 방식으로 돌아갈 수 있다.

## 5. ChatGPT 연결 절차

1. ChatGPT 웹의 Plugins로 이동하고 `+` → **Add custom MCP server**를 선택한다.
   계정/워크스페이스에 따라 Apps 또는 개발자 모드의 MCP 연결 화면으로 보일 수 있다.
   해당 메뉴가 없으면 워크스페이스 관리자의 custom MCP 권한을 확인한다.
2. 이름 `Paper Search`, 서버 URL `https://openrhiza.com/mcp/paper-search/oauth`를 넣는다.
3. 인증은 **OAuth**를 선택한다. 사전 등록한 OAuth client ID를 입력하고, 필요한 경우
   전용 입력란에 client secret을 입력한다. 이 예시는 DCR/CIMD 선택을 사용하지 않는다.
4. 관리 화면에 표시되는 callback URL이 provider allowlist와 동일한지 다시 확인한다.
   scope는 `papers:read`. authorize/token 주소가 자동 발견되지 않으면 provider의
   정확한 HTTPS 주소를 연결 화면이 허용하는 설정에 지정한다.
5. Create as a plugin을 선택하고 제공자의 로그인·동의를 완료한다. 개인 플러그인
   또는 지정 워크스페이스에 설치한다. 기존 Basic 비밀번호는 이 OAuth 로그인에 쓰지 않는다.
6. 도구 목록을 새로고침해 `search_papers` 등이 보이고 `download_with_fallback`은
   보이지 않는지 확인한다.
7. 대화에서 `@Paper Search`를 선택하고 다음을 실행한다.
   “Paper Search의 search_papers로 charge trap flash 논문을 arxiv와 crossref에서
   각각 최대 3개 검색해 주세요. 이번에는 내장 웹검색 대신 이 도구를 사용하세요.”
8. 도구 호출 상세에서 도구명·입력·결과를 확인한다. 로그아웃/재연결, 토큰 만료 후
   갱신 또는 재인증도 확인한다. **이 단계까지 실제 계정에서 성공해야 연결 완료이다.**

공식 현재 연결 안내에는 검색 도구를 꼭 `search`/`fetch`로 바꿀 필요가 없다.
서버의 기존 도구 스키마와 readOnlyHint를 유지하며 각 도구에 OAuth securitySchemes를
추가했다. 전체 경로를 OAuth로 보호하므로 인증 실패는 HTTP challenge로 처리한다.
Mixed/noauth 모드는 제공하지 않아 도구 내부의 재인증 UI 오류를 따로 구현하지 않는다.

## 6. 보안 및 검증 경계

| 항목 | 자동 검증 범위 | 운영/실제 계정 확인 |
| --- | --- | --- |
| JWT | 생성한 RSA 테스트 키로 서명, 다른 키, none/HS256, issuer/audience/만료/미래 iat·nbf·필수 claim | 제공자 실제 access token의 audience/scope/subject/client 발급 |
| 권한 | subject/client allowlist, scope 부족 403, Basic/미인증 401 | 사용자 권한 부여·회수와 재로그인 |
| discovery | canonical resource/issuer/scope, challenge metadata URL | 공개 HTTPS 경로와 provider discovery 접근 |
| MCP | 실제 SDK의 HTTP initialize/list/call, 기존 descriptor/schema, 숨긴 도구 직접 호출 차단 | ChatGPT 도구 스캔·선택·검색 성공 |
| HTTP | 중복 Authorization, query token 거부, Host/Origin, 1 MiB 요청 한도 | TLS, 방화벽/loopback, 프록시 헤더, 전역 요청률 제한 |
| JWKS | 고정 URL만 조회, jku 우회 차단, 캐시/키 회전, unknown kid 쿨다운, 크기 한도, 오류·redirect fail closed | provider 키 회전 및 장애 복구 |
| OAuth 발급 | provider discovery preflight를 모의 응답으로 확인 | PKCE 검증, redirect allowlist, state 검증, code 재사용, refresh 회전/폐기 |
| Caddy | 실제 Caddy adapt/validate 및 신규 경로 우선순위, 기존 Basic 보호 | 운영 설정 병합 후 기존 클라이언트 재연결 |

자동 테스트는 실제 OAuth 제공자의 login/code/token 동작을 모의로 “성공”시키지 않는다.
MCP SDK HTTP 테스트는 ASGI 전송으로 실제 프로토콜 초기화와 호출을 수행하지만
외부 TLS나 ChatGPT 계정 UI를 검증하지 않는다.

JWT는 요청마다 검증한다. 이미 유효한 JWT의 즉시 폐기는 이 resource server가 제공하지
않는다. 사용자/client allowlist에서 제거 후 서비스를 재시작하면 해당 주체는 차단되며,
provider 세션/refresh token도 별도로 폐기한다. 일반적인 폐기 지연은 access token 수명에
달려 있다. JWKS는 5분 캐시하고 unknown kid 재조회는 최대 30초마다 한 번이다.
즉시 token별 revocation이 필수이면 introspection/denylist 지원을 별도 추가해야 한다.

readOnlyHint는 인증/인가 수단이 아니다. 게이트웨이는 인증 완료 뒤 사전에 선택한
non-writing 도구만 실행한다. 검색·조회도 외부 제공자에 질의를 전송하므로 서비스
계정의 provider 키를 사용하고 요청률 제한은 Caddy 앞단 또는 인프라에서 추가한다.

### 이 변경에서 확인한 결과

- 로컬 Python 3.12 / MCP SDK 1.30.0에서 자동 테스트 **86개 통과**(추가 unittest subtest 5개),
  `pip check`, console entrypoint 및 `git diff --check`를 확인했다.
- 기존 stdio/SSE/HTTP 실행 및 기존 도구 스키마 회귀 테스트를 포함했다.
- 현재 공개 서버의 위 세 경로는 무인증 읽기만 수행했다. 자격 증명을 전송하지 않았다.
- 외부 provider tenant 생성, 운영 서버 배포, 실제 Basic 성공 로그인, ChatGPT 계정 연결은
  수행하지 않았다. 접근 가능한 운영 소스/관리 환경 및 실제 OAuth 설정이 필요하다.
- Caddy 2.10.0으로 OAuth 활성/비활성 두 구성의 validate와 활성 구성의 adapt/경로
  우선순위 검사를 실행해 통과했다. CI에도 Python 3.10/3.12 및 Caddy 검사를 추가했다.

```bash
python -m pip install -e '.[dev,oauth]'
python -m pip check
python -m pytest -q tests/test_oauth_gateway.py tests/test_server_transport.py tests/test_tool_schema_compat.py tests/test_package_entrypoints.py tests/test_config_env.py tests/test_stabilization_regressions.py tests/test_search_timeouts.py
```

## 공식 참고 자료

- [OpenAI: Add custom MCP server](https://developers.openai.com/api/docs/guides/custom-mcp-server)
- [OpenAI: OAuth 인증 계약, discovery, resource, PKCE, callback](https://developers.openai.com/plugins/build/auth)
- [OpenAI: 연결 화면의 Basic/API-key 제한](https://developers.openai.com/plugins/deploy/submission#authentication-limitations)
- [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [Auth0: S256 및 resource/audience 호환 프로필](https://auth0.com/docs/api/authentication/authorization-code-flow-with-pkce/authorize-with-pkce)
