"""
Wing 세션 쿠키 갱신 스크립트 — 로컬 PC에서 실행

서버에서는 xauth.coupang.com 접근이 차단되므로
로컬 PC에서 이 스크립트를 실행해 세션 파일을 생성한 뒤 서버에 업로드합니다.

사용법:
  python refresh_wing_session.py

완료 후 자동으로 서버에 업로드됩니다.
"""

import asyncio
import json
import re
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright

# ── 계정 설정 ────────────────────────────────────────────────────────
from dotenv import load_dotenv
import os

load_dotenv()

ACCOUNTS = [
    (os.getenv("WING_USERNAME", ""), os.getenv("WING_PASSWORD", "")),
    (os.getenv("WING_USERNAME_ZENITH", ""), os.getenv("WING_PASSWORD_ZENITH", "")),
]
ACCOUNTS = [(u, p) for u, p in ACCOUNTS if u and p]

WING_URL = "https://wing.coupang.com"
DATA_DIR = Path(__file__).resolve().parent / "data"

# ── 서버 업로드 설정 ───────────────────────────────────────────────
SERVER_HOST = "ubuntu@1.201.123.110"
SSH_KEY = str(Path(__file__).resolve().parent / "ssh_keys" / "SSH_KeyPair-260527213658.pem")
REMOTE_DATA = "/home/ubuntu/naver-to-coupang/data"

# Akamai 봇 탐지 회피용 스텔스 스크립트
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['ko-KR', 'ko', 'en-US', 'en'] });
window.chrome = { runtime: {} };
"""


def _slug(username: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '_', username)[:32]


def _is_success_url(url: str) -> bool:
    """Wing 메인 또는 대시보드 URL인지 확인 (에러/인증 페이지 제외)."""
    bad = ["xauth.coupang.com", "login", "edgesuite.net", "errors.", "access denied", "authenticate"]
    url_lower = url.lower()
    return (
        "wing.coupang.com" in url_lower
        and not any(b in url_lower for b in bad)
    )


async def refresh_account(username: str, password: str):
    session_file = DATA_DIR / f"wing_session_{_slug(username)}.json"
    print(f"\n[{username}] 세션 갱신 시작...")

    async with async_playwright() as pw:
        br = await pw.chromium.launch(
            headless=False,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await br.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        # navigator.webdriver 숨기기 (Akamai 봇 탐지 회피)
        await ctx.add_init_script(_STEALTH_JS)

        pg = await ctx.new_page()

        await pg.goto(WING_URL, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        cur = pg.url
        need_login = "xauth.coupang.com" in cur or "login" in cur.lower()

        if not need_login and _is_success_url(cur):
            print(f"[{username}] 이미 로그인 상태")
        else:
            print(f"[{username}] 로그인 필요, 자동 입력 시도...")

            # ID 입력
            _id_filled = False
            for sel in ["#username", "input[name='username']", "input[id='username']",
                        "input[placeholder*='아이디']", "input[placeholder*='ID']"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click()
                        await asyncio.sleep(0.2)
                        await loc.first.fill(username)
                        print(f"[{username}] 아이디 입력 완료 ({sel})")
                        _id_filled = True
                        break
                except Exception:
                    continue

            await asyncio.sleep(0.5)

            # PW 입력
            _pw_filled = False
            for sel in ["#password", "input[name='password']", "input[id='password']",
                        "input[type='password']"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click()
                        await asyncio.sleep(0.2)
                        await loc.first.fill(password)
                        print(f"[{username}] 비밀번호 입력 완료")
                        _pw_filled = True
                        break
                except Exception:
                    continue

            await asyncio.sleep(0.5)

            if _id_filled and _pw_filled:
                # 로그인 버튼
                for sel in ["#kc-login", "input[type='submit']", "button[type='submit']",
                            "button:has-text('로그인')", "button:has-text('Login')"]:
                    try:
                        loc = pg.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click()
                            print(f"[{username}] 로그인 버튼 클릭")
                            break
                    except Exception:
                        continue
            else:
                print(f"[{username}] 자동 입력 실패 — 브라우저 창에서 직접 로그인하세요!")

            print(f"[{username}] 로그인 완료 대기 중 (최대 3분)...")
            print("  ★ Access Denied 떴으면 뒤로가기 후 직접 로그인하세요!")
            for i in range(90):
                await asyncio.sleep(2)
                cur = pg.url
                if _is_success_url(cur):
                    print(f"[{username}] 로그인 성공! ({(i+1)*2}초, URL: {cur[:60]})")
                    break
                if i % 10 == 9:
                    print(f"  대기 중... {(i+1)*2}초 / 현재 URL: {cur[:60]}")
            else:
                print(f"[{username}] 로그인 3분 타임아웃")
                await br.close()
                return False

        # 세션 저장
        state = await ctx.storage_state()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump(state, f)
        print(f"[{username}] [OK] 세션 파일 저장: {session_file.name}")

        await br.close()
        return session_file


def upload_to_server(session_file: Path):
    print(f"\n서버 업로드 중: {session_file.name} ...")
    cmd = [
        "scp",
        "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        str(session_file),
        f"{SERVER_HOST}:{REMOTE_DATA}/{session_file.name}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[OK] 업로드 완료: {session_file.name}")
    else:
        print(f"실패: {result.stderr}")


async def main():
    if not ACCOUNTS:
        print(".env에 WING_USERNAME/WING_PASSWORD가 없습니다.")
        return

    saved_files = []
    for username, password in ACCOUNTS:
        result = await refresh_account(username, password)
        if result:
            saved_files.append(result)

    if not saved_files:
        print("\n세션 파일 생성 실패")
        return

    print(f"\n{len(saved_files)}개 세션 파일 생성 완료")
    ans = input("서버에 업로드할까요? (y/n): ").strip().lower()
    if ans == "y":
        for f in saved_files:
            upload_to_server(f)
        print("\n서버 업로드 완료! Wing 판매요청을 다시 시도하세요.")
    else:
        print(f"\n세션 파일 위치: {DATA_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
