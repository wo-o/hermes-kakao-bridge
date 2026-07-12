# hermes-kakao-bridge

Hermes Agent를 카카오톡 채널 챗봇에 연결하는 FastAPI 브리지 (Kakao i 오픈빌더 스킬 서버).

오픈빌더 스킬 서버는 5초 안에 응답해야 하므로, 콜백(useCallback)으로 즉답 후
`hermes -z` 결과를 callbackUrl(1회용, 유효시간 1분)에 POST한다.

```
[카톡] → [채널 챗봇 → 폴백 블록 → 스킬] → POST /kakao/skill
   ├─ 즉시: useCallback=true + 대기 메시지
   └─ 백그라운드: hermes -z "<메시지>" → callbackUrl로 답변 POST
```

## 요구 사항

- hermes 설치 + OAuth 인증 (`~/.hermes/auth.json` 존재, `.env` API 키 단독 인증은 미지원)
- 카카오톡 채널 + 오픈빌더 챗봇 (개인 채널 가능, 사업자등록 불필요)
- 공인 IP(HTTP, 테스트) 또는 도메인(HTTPS, 운영)

## 설치

`ubuntu` 유저 기준. 다른 유저면 유닛 파일의 `User=`/`PATH` 수정.

1) 카톡 전용 프로파일 생성 (`--clone`은 auth.json을 복사하지 않으므로 cp 필요):

```bash
hermes profile create kakao --no-skills --no-alias      # 빈 프로파일 (SOUL·메모리·스킬 미포함)
cp ~/.hermes/auth.json   ~/.hermes/profiles/kakao/      # 인증 재사용
cp ~/.hermes/config.yaml ~/.hermes/profiles/kakao/      # 모델 설정 재사용
```

2) 프로파일 config에 승인 프롬프트 끄기 (비대화형 실행 필수):

```bash
HERMES_HOME=~/.hermes/profiles/kakao hermes config set approvals.mode off
```

3) 브릿지 설치:

```bash
sudo git clone https://github.com/wo-o/hermes-kakao-bridge.git /opt/kakao-bridge
sudo chown -R ubuntu:ubuntu /opt/kakao-bridge
cd /opt/kakao-bridge && ./setup.sh
```

setup.sh가 의존성·bridge.env·systemd 기동·검증을 처리하고, 카카오 콘솔에 등록할
스킬 URL과 X-Bridge-Secret을 출력한다. 이미 만든 프로파일은 그대로 사용. 재실행 안전.
방화벽에서 80/tcp 인바운드 허용.

## 카카오 오픈빌더 등록

1. business.kakao.com/profiles — 채널 생성
2. chatbot.kakao.com — 봇 생성 → 설정 > 카카오톡 채널 연결
3. 스킬 > 생성 — 필드별 입력:
   - 스킬명: 자유 (예: `Hermes Bridge`) — 폴백 블록에서 이 이름으로 선택
   - URL: `http://<공인IP>/kakao/skill` (setup.sh가 출력한 스킬 URL 그대로)
   - 헤더값 입력: Key `X-Bridge-Secret` / Value = setup.sh가 출력한 시크릿
     (재확인: `grep '^KAKAO_BRIDGE_SECRET=' /opt/kakao-bridge/bridge.env`)
   - 설명·Test URL·테스트 헤더값: 비움. "기본 스킬로 설정" 체크 안 함
   - 저장 → 하단 스킬 테스트 "스킬서버로 전송" — "콜백 기능이 필요합니다" 응답이면 정상
     (테스트 콘솔은 callbackUrl을 안 보내므로 이 안내가 곧 서버 도달 + 시크릿 인증 통과.
     401 = 시크릿 불일치, 타임아웃 = 방화벽 80/tcp 미개방)
   - "현재 사용 중인 블록: 연결된 블록 없음"은 정상 — 4번에서 연결되면 채워짐
4. 시나리오 > 폴백 블록 — 봇 응답 "스킬데이터 사용" + 위 스킬 선택, 기존 텍스트 응답 삭제
5. 폴백 블록 ⋯ > Callback 설정 ON + 응답대기 메시지 입력 → 저장
   (이 필드가 브리지의 `KAKAO_WAITING_TEXT`보다 우선 — 대기 문구는 여기서 조정)
6. 배포 (수정할 때마다 재배포 필수)
7. 카톡에서 채널 추가 → 메시지 → 대기 메시지 후 답변 오면 완료

## 카톡 전용 프로파일

브리지는 전용 `kakao` 프로파일(`HERMES_HOME=~/.hermes/profiles/kakao`)로 응답한다 —
본체(`~/.hermes`)의 SOUL·메모리·세션·스킬과 격리. 페르소나·도구 제한은 프로파일 쪽만 수정:

```bash
vim ~/.hermes/profiles/kakao/SOUL.md                      # 카톡용 페르소나
echo 'HERMES_EXTRA_ARGS=--toolsets safe' >> bridge.env    # terminal·file 도구 차단
sudo systemctl restart kakao-bridge-http

HERMES_HOME=~/.hermes/profiles/kakao hermes -z "너는 누구야?"   # 검증
```

본체가 직접 응답하게 하려면 bridge.env의 `HERMES_HOME` 줄을 지우고 재시작.

## 환경 변수 (bridge.env)

| 변수 | 기본값 | 설명 |
|---|---|---|
| `HERMES_BIN` | `hermes` | hermes CLI 절대경로 |
| `HERMES_HOME` | `~/.hermes/profiles/kakao` | 카톡 전용 프로파일. 지우면 본체로 응답 |
| `HERMES_TIMEOUT` | `50` | 초. callbackUrl 유효시간(1분) 내로 — 50 초과 금지 |
| `HERMES_EXTRA_ARGS` | (없음) | hermes 추가 인자 (예: `--toolsets safe`) |
| `KAKAO_PER_USER_SESSION` | `1` | 사용자별 멀티턴 세션. `0`이면 stateless 단발 응답 |
| `KAKAO_BRIDGE_SECRET` | (빈 값) | `X-Bridge-Secret` 헤더 검증 |
| `KAKAO_WAITING_TEXT` | 🤔 답변을... | 즉답 대기 메시지 |
