#!/usr/bin/env python3
"""
Hermes <-> KakaoTalk bridge (Kakao i 오픈빌더 skill server).

Why this exists:
  Hermes has no native KakaoTalk channel. Kakao's only official two-way path is
  a 채널 챗봇 whose 스킬 서버 must answer an HTTP POST within a 5-second SLA.
  An LLM reply takes longer than 5s, so we use Kakao's callback (useCallback)
  mechanism: respond instantly, then POST the real answer to userRequest.callbackUrl.

Flow:
  Kakao 챗봇 ──POST /kakao/skill──▶ this bridge        (must answer in <5s)
                                     ├─ returns {"useCallback": true, ...}
                                     └─ background: run `hermes -z "<utterance>"`
                                          └─ POST simpleText reply to callbackUrl

Run via systemd (deploy/kakao-bridge-http.service, port 80) or behind a TLS
proxy (deploy/kakao-bridge.service, 127.0.0.1:8000).

The bridge invokes the local `hermes` CLI, so it must run on the same host and
as the same user that owns ~/.hermes so auth.json/config are picked up. Point
HERMES_HOME at a hermes profile to isolate the Kakao persona from the main agent.
"""

import asyncio
import hmac
import logging
import os
import re
import shlex

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger("kakao-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# --- config (env vars; set via systemd EnvironmentFile) ---
BRIDGE_SECRET = os.environ.get(
    "KAKAO_BRIDGE_SECRET", ""
)  # optional X-Bridge-Secret header check
HERMES_BIN = os.environ.get(
    "HERMES_BIN", "hermes"
)  # absolute path recommended in systemd
HERMES_TIMEOUT = int(
    os.environ.get("HERMES_TIMEOUT", "50")
)  # seconds; MUST stay under Kakao's callbackUrl expiry (1 min, single-use)
HERMES_EXTRA_ARGS = shlex.split(
    os.environ.get("HERMES_EXTRA_ARGS", "")
)  # e.g. "--toolsets safe" to restrict tools for the Kakao channel
PER_USER_SESSION = (
    os.environ.get("KAKAO_PER_USER_SESSION", "0") == "1"
)  # 0 = stateless one-shot (default), 1 = per-user multi-turn memory
WAITING_TEXT = os.environ.get(
    "KAKAO_WAITING_TEXT", "🤔 답변을 작성하고 있어요. 잠시만 기다려 주세요..."
)
KAKAO_TEXT_LIMIT = 1000  # simpleText practical character limit

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

app = FastAPI(title="hermes-kakao-bridge")

# Keep strong refs to fire-and-forget tasks so they aren't GC'd before completion.
_BG_TASKS: set = set()


def _simple_text(text: str) -> dict:
    return {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": text}}]}}


def _clean(s: str) -> str:
    s = ANSI_RE.sub("", s).strip()
    if len(s) > KAKAO_TEXT_LIMIT:
        s = s[: KAKAO_TEXT_LIMIT - 1] + "…"
    return s or "응답을 생성하지 못했어요. 다시 시도해 주세요."


_SESSION_ID_RE = re.compile(r"session_id:\s*(\S+)")


async def _exec(cmd: list) -> tuple:
    """Run a hermes command. Returns (rc, stdout, stderr); rc=None on timeout."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=HERMES_TIMEOUT)
    except asyncio.TimeoutError:
        # Kill the subprocess — otherwise timed-out hermes runs pile up and
        # exhaust RAM on small instances.
        LOG.error("hermes timeout after %ss — killing pid=%s", HERMES_TIMEOUT, proc.pid)
        proc.kill()
        await proc.wait()
        return None, "", ""
    except FileNotFoundError:
        LOG.error("hermes binary not found: %s", HERMES_BIN)
        return 127, "", ""
    return (
        proc.returncode,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def _run_hermes(utterance: str, user_id: str) -> str:
    # Prefix the anonymous Kakao user id so hermes can tell users apart in
    # session context and long-term memory (the id is a bot-scoped hash, not PII).
    prompt = f"[카카오톡 사용자 {user_id}]\n{utterance}"
    LOG.info(
        "hermes invoke (%s) user=%s",
        "session" if PER_USER_SESSION else "oneshot",
        user_id,
    )
    if not PER_USER_SESSION:
        # Stateless one-shot. `-z` prints ONLY the final response text (cleanest for a chatbot).
        # Hermes' persistent long-term memory still applies across calls.
        rc, out, err = await _exec([HERMES_BIN, "-z", prompt, *HERMES_EXTRA_ARGS])
        if rc is None:
            return "응답이 조금 더 걸리고 있어요. 잠시 후 다시 물어봐 주세요."
        if rc == 127:
            return "서버 설정 오류(hermes 미발견). 관리자에게 문의해 주세요."
        if rc != 0:
            LOG.error("hermes rc=%s err=%s", rc, err[:500])
        return _clean(out)

    # Per-user thread: `--continue <name>` resumes an EXISTING named session only —
    # it exits 1 with "No session found" for a first-time user, so create + name
    # the session on that miss and answer from the fresh run.
    session_name = f"kakao-{user_id}"
    rc, out, err = await _exec(
        [
            HERMES_BIN,
            "chat",
            "-Q",
            "-q",
            prompt,
            "--continue",
            session_name,
            *HERMES_EXTRA_ARGS,
        ]
    )
    if rc is None:
        return "응답이 조금 더 걸리고 있어요. 잠시 후 다시 물어봐 주세요."
    if rc == 127:
        return "서버 설정 오류(hermes 미발견). 관리자에게 문의해 주세요."
    if rc != 0 and "No session found" in (out + err):
        LOG.info("no session for %s — creating", session_name)
        rc, out, err = await _exec(
            [HERMES_BIN, "chat", "-Q", "-q", prompt, *HERMES_EXTRA_ARGS]
        )
        if rc is None:
            return "응답이 조금 더 걸리고 있어요. 잠시 후 다시 물어봐 주세요."
        # `chat -Q` prints "session_id: <id>" to stderr — name it so the next
        # message's --continue finds it. Best-effort: a failed rename just means
        # the next message starts a new session instead of crashing.
        m = _SESSION_ID_RE.search(err)
        if m:
            r_rc, _, r_err = await _exec(
                [HERMES_BIN, "sessions", "rename", m.group(1), session_name]
            )
            if r_rc != 0:
                LOG.error("session rename failed rc=%s err=%s", r_rc, r_err[:300])
        else:
            LOG.error("no session_id in hermes stderr — multi-turn will not stick")
    if rc != 0:
        LOG.error("hermes rc=%s err=%s", rc, err[:500])
    return _clean(out)


async def _process_and_callback(
    callback_url: str, utterance: str, user_id: str
) -> None:
    reply = await _run_hermes(utterance, user_id)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(callback_url, json=_simple_text(reply))
            LOG.info("callback rc=%s body=%s", r.status_code, r.text[:200])
    except Exception as e:  # noqa: BLE001
        LOG.error("callback post failed: %s", e)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/kakao/skill")
async def kakao_skill(request: Request, x_bridge_secret: str = Header(default="")):
    if BRIDGE_SECRET and not hmac.compare_digest(x_bridge_secret, BRIDGE_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    ureq = (body or {}).get("userRequest", {})
    utterance = (ureq.get("utterance") or "").strip()
    callback_url = ureq.get("callbackUrl")
    user_id = (ureq.get("user") or {}).get("id", "anon")

    if not utterance:
        return JSONResponse(
            _simple_text("메시지를 이해하지 못했어요. 다시 입력해 주세요.")
        )

    if not callback_url:
        # Without callback we cannot beat the 5s SLA for an LLM reply.
        return JSONResponse(
            _simple_text(
                "이 봇은 콜백 기능이 필요합니다. 오픈빌더 블록에서 콜백을 활성화해 주세요."
            )
        )

    if not callback_url.startswith("https://"):
        # Kakao always issues https callbackUrls; anything else is a forged request.
        raise HTTPException(status_code=400, detail="invalid callbackUrl")

    # Respond within 5s; the real answer is delivered to callbackUrl by the background task.
    task = asyncio.create_task(_process_and_callback(callback_url, utterance, user_id))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return JSONResponse(
        {"version": "2.0", "useCallback": True, "data": {"text": WAITING_TEXT}}
    )
