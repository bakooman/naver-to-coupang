"""
텔레그램 봇 알림 모듈

설정 파일: data/config.json
  {
    "telegram": {
      "bot_token": "1234567890:AAFxxx...",
      "chat_id":   "123456789"
    }
  }

설정이 없거나 빈 값이면 알림을 발송하지 않고 조용히 건너뜁니다.
"""
from __future__ import annotations

import json
import urllib.request
import urllib.parse
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "data" / "config.json"
_TIMEOUT = 8  # 초

# 텔레그램 폴링 offset (getUpdates 중복 수신 방지)
_poll_offset: int = 0


def _load_config() -> tuple[str, str]:
    """bot_token, chat_id 반환. 설정 없으면 ("", "") 반환."""
    try:
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        tg = raw.get("telegram", {})
        token = (tg.get("bot_token") or "").strip()
        chat_id = (tg.get("chat_id") or "").strip()
        return token, chat_id
    except Exception:
        return "", ""


def send_notification(message: str) -> bool:
    """
    텔레그램으로 메시지 발송.

    Args:
        message: 발송할 텍스트 (이모지 포함 가능)

    Returns:
        True = 발송 성공, False = 설정 없음 or 실패
    """
    token, chat_id = _load_config()
    if not token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print("[Notifier] OK: 텔레그램 알림 발송 완료")
                return True
            else:
                print(f"[Notifier] ERR: 텔레그램 응답 오류: {result}")
                return False

    except Exception as e:
        print(f"[Notifier] ERR: 텔레그램 알림 실패 (무시): {e}")
        return False


def send_notification_with_register_button(message: str) -> bool:
    """
    수집완료 알림 + [▶ 자동등록 시작] 인라인 버튼 발송.

    Returns:
        True = 발송 성공
    """
    token, chat_id = _load_config()
    if not token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        keyboard = {
            "inline_keyboard": [[
                {"text": "▶ 자동등록 시작", "callback_data": "start_register"}
            ]]
        }
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print("[Notifier] OK: 자동등록 버튼 포함 알림 발송 완료")
                return True
            else:
                print(f"[Notifier] ERR: 텔레그램 응답 오류: {result}")
                return False

    except Exception as e:
        print(f"[Notifier] ERR: 텔레그램 알림 실패 (무시): {e}")
        return False


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """인라인 버튼 탭 후 로딩 스피너 제거 (answerCallbackQuery)."""
    token, _ = _load_config()
    if not token:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
        payload = json.dumps({
            "callback_query_id": callback_query_id,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=_TIMEOUT).close()
    except Exception:
        pass


def poll_register_callback() -> bool:
    """
    텔레그램 getUpdates 폴링 — [▶ 자동등록 시작] 버튼이 눌렸는지 확인.

    Returns:
        True = 버튼이 눌림 (한 번만 반환, offset 전진됨)
        False = 눌리지 않음 or 설정 없음 or 오류
    """
    global _poll_offset

    token, chat_id = _load_config()
    if not token or not chat_id:
        return False

    try:
        params = urllib.parse.urlencode({
            "offset": _poll_offset,
            "timeout": 0,
            "allowed_updates": '["callback_query"]',
        })
        url = f"https://api.telegram.org/bot{token}/getUpdates?{params}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        if not result.get("ok"):
            return False

        triggered = False
        for update in result.get("result", []):
            update_id = update.get("update_id", 0)
            _poll_offset = max(_poll_offset, update_id + 1)

            cq = update.get("callback_query")
            if not cq:
                continue

            # chat_id 확인 (다른 채팅에서 온 콜백 무시)
            cq_chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
            if cq_chat_id != chat_id:
                continue

            if cq.get("data") == "start_register":
                answer_callback_query(cq["id"], "⏳ 자동등록 시작 중...")
                triggered = True

        return triggered

    except Exception as e:
        print(f"[Notifier] poll 오류 (무시): {e}")
        return False
