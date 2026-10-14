# Paper Search MCP 배포 메모

ChatGPT 연결은 [OAuth 배포 및 연결 안내](CHATGPT.ko.md)를 참고한다. 아래 Basic Auth 경로는 기존 클라이언트용으로 유지한다. 현재 공개 서버의 실제 인증 기능은 이 저장소의 배포 파일보다 확장되어 있을 수 있으므로, 운영 설정을 덮어쓰기 전에 배포 소스를 확인한다.

## 접속 정보

- MCP URL: `https://openrhiza.com/mcp/paper-search`
- 인증: HTTP Basic Auth
- 사용자명: `smep-n`
- 비밀번호: 설치 시 지정한 비밀번호
- 내부 백엔드: `http://127.0.0.1:8765/mcp` (외부에 직접 노출되지 않음)

MCP 클라이언트가 사용자명/비밀번호 입력란을 제공하지 않으면 아래 헤더를 설정한다.

```text
Authorization: Basic <base64("smep-n:<password>")>
```

## 운영 명령

```bash
systemctl --user status paper-search-mcp.service
journalctl --user -u paper-search-mcp.service -f
systemctl --user restart paper-search-mcp.service
```

서비스가 시작될 때 로컬 Caddy 관리 API에 인증 경로를 다시 적용하므로 재부팅 후에도
설정이 복구된다. 시스템 Caddy만 별도로 재시작했다면 위의 MCP 서비스 재시작 명령을
한 번 실행한다.

## 선택 API 키

프로젝트 루트의 `.env`에서 검색 제공자별 키를 설정한 다음 서비스를 재시작한다.
기본 검색은 키 없이도 동작하지만 Semantic Scholar, OpenAlex, CORE는 키가 있으면
요청 한도와 안정성이 좋아진다. Unpaywall은 이메일 설정이 필요하다.

비밀번호 해시는 공개 저장소에 커밋하지 않는다. `caddy hash-password`로 해시를
만들어 프로젝트 루트의 `.env`에 아래처럼 저장한다.

```dotenv
PAPER_SEARCH_MCP_BASIC_AUTH_HASH=<bcrypt 해시>
```

비밀번호를 변경한 후에는 설정을 검증하고 서비스를 재시작한다.

```bash
caddy hash-password
caddy validate --config deploy/Caddyfile --adapter caddyfile
systemctl --user restart paper-search-mcp.service
```
