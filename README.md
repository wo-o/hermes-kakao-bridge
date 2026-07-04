# hermes-kakao-bridge

Hermes Agent를 카카오톡 채널 챗봇에 연결하는 FastAPI 브리지 (Kakao i 오픈빌더 스킬 서버).

오픈빌더 스킬 서버는 5초 안에 응답해야 하므로, 콜백(useCallback)으로 즉답 후
`hermes -z` 결과를 callbackUrl(1회용, 최대 5분 유효)에 POST한다.

```
[카톡] → [채널 챗봇 → 폴백 블록 → 스킬] → POST /kakao/skill
   ├─ 즉시: useCallback=true + 대기 메시지
   └─ 백그라운드: hermes -z "<메시지>" → callbackUrl로 답변 POST
```

브리지는 `hermes` CLI를 서브프로세스로 실행한다 — hermes와 같은 호스트, 같은 유저 필수.

## 요구 사항

- hermes 설치·OAuth 인증 완료 (`~/.hermes/auth.json` 존재, `hermes -z "..."` 동작)
  — .env API 키만으로 인증한 환경은 지원하지 않음 (setup.sh가 auth.json을 프로파일로 복사)
- config.yaml에 `approvals: { mode: "off" }` (비대화형 호출용)
- 카카오톡 채널 + 오픈빌더 챗봇 (개인 채널 가능, 사업자등록 불필요)
- 공인 IP(HTTP, 테스트) 또는 도메인(HTTPS, 운영)

## 설치 (호스트 systemd + 공인 IP)

전제: `ubuntu` 유저, hermes도 같은 유저로 인증됨. 다른 유저면 유닛의 `User=`/`PATH` 수정.

```bash
sudo git clone https://github.com/wo-o/hermes-kakao-bridge.git /opt/kakao-bridge
sudo chown -R ubuntu:ubuntu /opt/kakao-bridge
cd /opt/kakao-bridge && ./setup.sh
```

setup.sh가 의존성·**kakao 프로파일 생성**·bridge.env(시크릿 자동 생성)·approvals off·
systemd 기동·공인 IP 감지·검증까지 처리하고, 카카오 콘솔에 등록할 스킬 URL과
X-Bridge-Secret을 출력한다. 재실행 안전.

방화벽: 클라우드 콘솔에서 80/tcp 인바운드 허용.

## 카카오 오픈빌더 등록

1. center-pf.kakao.com — 채널 생성
2. chatbot.kakao.com — 봇 생성 → 설정 > 카카오톡 채널 연결
3. 스킬 > 생성 — URL `http://<공인IP>/kakao/skill`, 헤더 `X-Bridge-Secret: <시크릿>`
   → 저장 후 "스킬서버로 전송" 테스트 (콜백 안내 JSON이 나오면 정상)
4. 시나리오 > 폴백 블록 — 스킬 선택, 응답 추가에서 스킬데이터 추가 후 기존 텍스트 응답 삭제
   (응답 최소 1개 제약 때문에 추가 → 삭제 순서)
5. 폴백 블록 우측 상단 ⋯ > Callback 설정 — 토글 ON + 대기 메시지 입력 → 확인 → 저장
6. 배포 메뉴 > 배포 (수정할 때마다 재배포 필수)
7. 카톡에서 채널 추가 → 메시지 → 대기 메시지 후 답변 오면 완료

## 카톡 전용 프로파일 (기본 동작 — 본체와 격리)

setup.sh가 전용 `kakao` 프로파일을 만들고 브리지가 그 프로파일로 응답한다:

1. `hermes profile create kakao --no-skills --no-alias` — 빈 프로파일 생성
2. 본체 `auth.json`·`config.yaml` 복사 (인증·모델 설정 재사용)
3. 프로파일 config에 `approvals: mode: "off"` 적용
4. bridge.env에 `HERMES_HOME=~/.hermes/profiles/kakao` 기록

→ 본체(`~/.hermes`)의 SOUL·메모리·세션·스킬에 카톡 채널이 접근하지 못한다.

카톡용 페르소나·도구 제한은 프로파일 쪽만 수정하면 된다:

```bash
vim ~/.hermes/profiles/kakao/SOUL.md                 # 카톡용 페르소나
echo 'HERMES_EXTRA_ARGS=--toolsets safe' >> bridge.env   # web/vision만 허용, terminal·file 차단
sudo systemctl restart kakao-bridge-http

HERMES_HOME=~/.hermes/profiles/kakao hermes -z "너는 누구야?"   # 검증
```

본체가 직접 응답하게 하려면 bridge.env의 `HERMES_HOME` 줄을 지우고 재시작.

## 환경 변수 (bridge.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HERMES_BIN` | `hermes` | hermes CLI 절대경로 |
| `HERMES_HOME` | `~/.hermes/profiles/kakao` | 카톡 전용 프로파일. 지우면 본체로 응답 |
| `HERMES_TIMEOUT` | `50` | 초. callbackUrl 유효시간(최대 5분) 내로 |
| `HERMES_EXTRA_ARGS` | (없음) | hermes 추가 인자 (예: `--toolsets safe` — terminal·file 도구 차단) |
| `KAKAO_PER_USER_SESSION` | `0` | `1`이면 사용자별 멀티턴 세션 |
| `KAKAO_BRIDGE_SECRET` | (빈 값) | `X-Bridge-Secret` 헤더 검증 |
| `KAKAO_WAITING_TEXT` | 🤔 답변을... | 즉답 대기 메시지 |

## TLS 운영 전환

`deploy/kakao-bridge.service`(127.0.0.1:8000) + Caddy 리버스 프록시로 교체.
도메인 없으면 DuckDNS 무료 서브도메인. Caddyfile은 두 줄이면 된다:

```
<도메인> {
    reverse_proxy 127.0.0.1:8000
}
```

## 트러블슈팅

- 카톡에 "콜백 기능이 필요합니다": Callback 토글 OFF이거나 재배포 누락
- 5초 후 "오류가 발생했습니다": 콜백 미적용 상태에서 hermes 응답 대기
- 대기 메시지 후 답이 없음: `journalctl -u kakao-bridge-http -f`에서 `callback rc=` 확인.
  hermes가 HERMES_TIMEOUT을 넘기면 유실
- hermes 미발견/401: 브리지 실행 유저 ≠ hermes 인증 유저 (유닛 `User=` 확인)
- 답변이 본체 페르소나로 옴: bridge.env `HERMES_HOME` 누락 — setup.sh 재실행 또는 수동 추가
- `hermes profile` 명령 없음: 구버전 hermes — `hermes update` 후 setup.sh 재실행
- config의 `approvals: mode:` 값은 `"off"` 따옴표 권장 (미인용 off는 false로 파싱되나
  hermes가 방어 처리함)

## 보안

- bridge.env 커밋 금지(.gitignore), chmod 600
- HTTP+공인IP는 평문 — 대화·시크릿 노출. 테스트 후 TLS 전환 권장
- 폴백 스킬 = 채널에 말 거는 누구나 hermes와 대화. 프로파일 격리(기본) +
  `HERMES_EXTRA_ARGS=--toolsets safe` + 프로파일 SOUL.md 가드 권장. 답변은 simpleText 1000자 절단
