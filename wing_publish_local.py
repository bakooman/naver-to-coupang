"""
Wing 임시저장 일괄 판매요청 — 로컬 PC 디버깅용 (headless=False)

서버에서는 Linux headless 환경이라 브라우저 창이 안 뜨므로
로컬 PC에서 이 스크립트를 실행하면 브라우저 창이 직접 보입니다.
어느 단계에서 막히는지 육안으로 확인 후 피드백 주세요.

사용법:
  python wing_publish_local.py
  python wing_publish_local.py --account zenith   # 제니스 계정만 실행
  python wing_publish_local.py --account shopk    # 샵케이 계정만 실행
"""

import asyncio
import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
import os

load_dotenv()

from modules.wing_automator import WingAutomator

# ── 계정 설정 ────────────────────────────────────────────────────────
ACCOUNTS = {
    "shopk": {
        "label": "샵케이",
        "username": os.getenv("WING_USERNAME", ""),
        "password": os.getenv("WING_PASSWORD", ""),
    },
    "zenith": {
        "label": "제니스 트레이딩",
        "username": os.getenv("WING_USERNAME_ZENITH", ""),
        "password": os.getenv("WING_PASSWORD_ZENITH", ""),
    },
}


def _pick_accounts() -> list[dict]:
    """커맨드라인 인수로 계정 필터링. 없으면 전체."""
    arg = ""
    for a in sys.argv[1:]:
        if a.startswith("--account"):
            parts = a.split("=", 1)
            arg = parts[1].strip().lower() if len(parts) == 2 else ""
            # --account zenith 형태
            idx = sys.argv.index(a)
            if not arg and idx + 1 < len(sys.argv):
                arg = sys.argv[idx + 1].lower()

    if arg == "zenith":
        return [ACCOUNTS["zenith"]]
    if arg in ("shopk", "shopkey"):
        return [ACCOUNTS["shopk"]]
    return list(ACCOUNTS.values())


async def run_account(acc: dict):
    username = acc["username"]
    password = acc["password"]
    label = acc["label"]

    if not username or not password:
        print(f"[{label}] ⚠️  계정 정보 없음 — .env에 WING_USERNAME/WING_PASSWORD 확인")
        return

    print(f"\n{'='*60}")
    print(f"  [{label}] Wing 판매요청 시작 (headless=False — 창 표시)")
    print(f"{'='*60}")
    print("  ★ 브라우저 창이 열립니다. 로그인 화면이 나오면 직접 로그인하세요.")
    print("  ★ 어느 단계에서 막히는지 메모 후 피드백 주세요.\n")

    async with WingAutomator(username, password, headless=False) as wa:
        result = await wa.bulk_publish_all(
            skip_names=None,
            log_cb=print,
            progress_cb=None,
            product_data=None,
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        )

    print(f"\n[{label}] 결과:")
    print(f"  등록 성공: {result.get('published', 0)}개")
    print(f"  건너뜀:   {result.get('skipped', 0)}개")
    errors = result.get("errors", [])
    if errors:
        print(f"  오류 ({len(errors)}개):")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  오류: 없음")


async def main():
    accounts = _pick_accounts()
    valid = [a for a in accounts if a["username"] and a["password"]]

    if not valid:
        print("❌ 실행할 계정이 없습니다. .env 파일에 WING_USERNAME/WING_PASSWORD를 확인하세요.")
        return

    for acc in valid:
        await run_account(acc)

    print("\n모든 계정 처리 완료.")


if __name__ == "__main__":
    asyncio.run(main())
