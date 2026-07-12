#!/usr/bin/env bash
# One-shot installer — install A: host systemd + HTTP + public IP (no domain).
# Run from the repo root as the user that owns hermes (default: ubuntu):
#   cd /opt/kakao-bridge && ./setup.sh
# Idempotent: safe to re-run; keeps an existing bridge.env (and its secret).
set -euo pipefail

if [[ "$(whoami)" != "ubuntu" ]]; then
  echo "경고: 현재 유저는 $(whoami) — 유닛 파일은 User=ubuntu 기준."
  echo "      이 유저로 계속하려면 deploy/kakao-bridge-http.service의 User=/PATH를 맞추고 재실행."
fi

command -v hermes >/dev/null 2>&1 || {
  echo "hermes를 찾을 수 없음 — 이 유저로 hermes 설치·인증 후 재실행 (command -v hermes)"; exit 1; }

[[ -f "$HOME/.hermes/auth.json" ]] || {
  echo "~/.hermes/auth.json 없음 — OAuth 인증(hermes auth add ...) 완료 후 재실행"; exit 1; }

echo "→ 파이썬 의존성"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv >/dev/null
python3 -m venv venv
venv/bin/pip install -q -r requirements.txt

# Dedicated hermes profile — isolates the Kakao persona (SOUL/memory/sessions)
# from the main agent. Auth and model config are seeded from the main profile.
echo "→ hermes 프로파일 (kakao)"
PROFILE_DIR="$HOME/.hermes/profiles/kakao"
if [[ ! -d "$PROFILE_DIR" ]]; then
  hermes profile create kakao --no-skills --no-alias
fi
[[ -f "$PROFILE_DIR/auth.json" ]]   || cp "$HOME/.hermes/auth.json"   "$PROFILE_DIR/auth.json"
[[ -f "$PROFILE_DIR/config.yaml" ]] || cp "$HOME/.hermes/config.yaml" "$PROFILE_DIR/config.yaml"

if [[ -f bridge.env ]]; then
  echo "→ bridge.env 이미 있음 — 기존 시크릿 유지"
  grep -q "^HERMES_HOME=" bridge.env \
    || echo "HERMES_HOME=${PROFILE_DIR}" >> bridge.env
else
  echo "→ bridge.env 생성"
  install -m 600 /dev/null bridge.env
  cat > bridge.env <<EOF
HERMES_BIN=$(command -v hermes)
HERMES_HOME=${PROFILE_DIR}
HERMES_TIMEOUT=50
KAKAO_PER_USER_SESSION=1
KAKAO_BRIDGE_SECRET=$(openssl rand -hex 32)
EOF
fi

echo "→ hermes approvals off (kakao 프로파일)"
grep -q "^approvals:" "$PROFILE_DIR/config.yaml" \
  || printf 'approvals:\n  mode: "off"\n' >> "$PROFILE_DIR/config.yaml"

echo "→ systemd 등록·기동"
sudo cp deploy/kakao-bridge-http.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kakao-bridge-http
# enable --now does not restart an already-running service — restart so a
# re-run picks up bridge.env changes (e.g. newly added HERMES_HOME)
sudo systemctl restart kakao-bridge-http
sleep 2

# Public IP auto-detection (external echo services, fallback chain)
PUBLIC_IP=$(curl -fsS --max-time 5 https://checkip.amazonaws.com 2>/dev/null \
  || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null || true)
PUBLIC_IP=$(echo "${PUBLIC_IP}" | tr -d '[:space:]')
SECRET=$(grep '^KAKAO_BRIDGE_SECRET=' bridge.env | cut -d= -f2-)

echo
echo "== 상태 =="
echo "service      : $(systemctl is-active kakao-bridge-http)"
echo "local health : $(curl -s --max-time 5 http://127.0.0.1/health || echo FAIL)"
if [[ -n "${PUBLIC_IP}" ]]; then
  PUB_HEALTH=$(curl -s --max-time 5 "http://${PUBLIC_IP}/health" || true)
  if [[ "${PUB_HEALTH}" == '{"ok":true}' ]]; then
    echo "public health: ${PUB_HEALTH}"
  else
    echo "public health: FAIL — 클라우드 콘솔 방화벽에서 80/tcp 인바운드 허용 확인"
    echo "               (일부 클라우드는 서버 내부에서 자기 공인 IP 접근이 막힘 — 로컬 PC에서 재확인:"
    echo "                curl http://${PUBLIC_IP}/health)"
  fi
else
  echo "public IP    : 감지 실패 — 클라우드 콘솔에서 확인"
fi
echo
echo "== hermes 검증 — kakao 프로파일 (60초까지 걸릴 수 있음) =="
HERMES_HOME="$PROFILE_DIR" timeout 90 hermes -z "Reply with exactly one word: PONG" \
  || echo "FAIL — kakao 프로파일 인증/approvals 확인 (${PROFILE_DIR})"
echo
echo "== 카카오 오픈빌더에 등록할 값 =="
echo "스킬 URL       : http://${PUBLIC_IP:-<공인IP>}/kakao/skill"
echo "헤더            : X-Bridge-Secret: ${SECRET}"
