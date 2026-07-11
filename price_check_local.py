"""
네이버 가격 감시 — 로컬 PC 실행 스크립트
==========================================
서버 IP가 Naver에 차단돼 있어서 이 PC(집 IP)에서 직접 가격을 체크하고
서버로 결과를 전송합니다.

사용법:
  python price_check_local.py

자동 실행:
  run_price_check.bat 를 Windows 작업 스케줄러에 등록 (00:00 / 12:00)
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

# Windows 콘솔 UTF-8 출력
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── 설정 ─────────────────────────────────────────────────────────────────────
SERVER_URL  = "https://bakoo-mm.com"
CONFIG_FILE = Path(__file__).parent / "data" / "config.json"
DELAY_SEC   = 1.0   # 요청 간 딜레이 (초) — Naver 레이트리밋 방지
TIMEOUT     = 15    # 단일 요청 타임아웃 (초)

# ── price_checker._fetch_product_info 재사용 ─────────────────────────────────
# 서버와 동일한 파싱 로직을 사용하기 위해 로컬 modules 경로를 추가합니다.
sys.path.insert(0, str(Path(__file__).parent))

try:
    from modules.price_checker import _fetch_product_info as _fetch
    print("[로컬] 서버 파싱 모듈 로드 완료")
except ImportError as e:
    print(f"[오류] modules/price_checker.py 불러오기 실패: {e}")
    print("프로젝트 루트 디렉터리에서 실행하고 있는지 확인하세요.")
    sys.exit(1)


# ── 헬퍼 ─────────────────────────────────────────────────────────────────────

def _load_token() -> str:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        token = cfg.get("local_api_token", "")
        if not token:
            print("[오류] config.json에 local_api_token이 없습니다.")
        return token
    except Exception as e:
        print(f"[오류] config.json 읽기 실패: {e}")
        return ""


def _api_headers(token: str) -> dict:
    return {"X-API-Token": token, "Content-Type": "application/json"}


def _get_watchlist(token: str) -> list:
    print(f"[1단계] 서버에서 감시목록 가져오는 중... ({SERVER_URL}/api/watchlist)")
    try:
        r = requests.get(
            f"{SERVER_URL}/api/watchlist",
            headers=_api_headers(token),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            print(f"[오류] {data['error']}")
            return []
        print(f"  → 감시 상품: {len(data)}개")
        return data
    except Exception as e:
        print(f"[오류] watchlist 가져오기 실패: {e}")
        return []


def _check_prices(watches: list) -> list:
    total      = len(watches)
    results    = []
    ok_cnt     = err_cnt = 0
    consec_err = 0          # 연속 실패 카운트
    last_err   = ""         # 마지막 에러 (중복 출력 방지)

    print(f"\n[2단계] 가격 체크 시작 ({total}개)")

    for i, w in enumerate(watches, 1):
        uid  = w["uid"]
        url  = w["url"]
        name = w.get("name", "")[:30]

        try:
            info = _fetch(url)
        except Exception as e:
            info = {"price": 0, "soldout": False, "name": "", "error": str(e)}

        results.append({
            "uid":     uid,
            "price":   info.get("price", 0),
            "soldout": info.get("soldout", False),
            "name":    info.get("name", "") or name,
            "error":   info.get("error", ""),
        })

        err_msg = info.get("error", "")
        if err_msg and not info.get("price"):
            err_cnt    += 1
            consec_err += 1
            symbol      = "✗"
            price_str   = "실패"
            # 에러 메시지 — 처음 등장하거나 내용이 바뀔 때만 출력
            if err_msg != last_err:
                print(f"  [에러] {err_msg}")
                last_err = err_msg
        else:
            ok_cnt    += 1
            consec_err = 0
            last_err   = ""
            symbol      = "✓"
            price_str   = f"{info.get('price', 0):,}원"

        print(f"  [{i:4d}/{total}] {symbol} {name:<30} {price_str}")

        # 연속 실패 10개 이상이면 30초 대기 후 재개
        if consec_err == 10:
            print(f"\n  [주의] 연속 {consec_err}회 실패 — 30초 대기 후 재개합니다...")
            time.sleep(30)
            consec_err = 0
        elif DELAY_SEC > 0:
            time.sleep(DELAY_SEC)

    print(f"\n  체크 완료: 성공 {ok_cnt}개 / 실패 {err_cnt}개")
    return results


def _send_results(token: str, results: list) -> dict:
    print(f"\n[3단계] 서버로 결과 전송 중... ({len(results)}개)")
    try:
        # 이름 필드의 제어문자 제거 (JSON 파싱 오류 방지)
        import re
        clean = []
        for item in results:
            c = dict(item)
            if c.get("name"):
                c["name"] = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', c["name"])
            clean.append(c)
        # str → bytes 명시 인코딩 (Python http.client 기본값 ISO-8859-1 방지)
        body = json.dumps(clean, ensure_ascii=False).encode('utf-8')
        r = requests.post(
            f"{SERVER_URL}/api/price-report",
            headers=_api_headers(token),
            data=body,
            timeout=60,
        )
        r.raise_for_status()
        resp = r.json()
        if resp.get("ok"):
            s = resp.get("stats", {})
            print(f"  [완료] 상승 {s.get('risen',0)} / 하락 {s.get('fallen',0)} "
                  f"/ 품절 {s.get('soldout',0)} / 정상 {s.get('ok',0)} / 오류 {s.get('errors',0)}")
            if s.get("risen", 0) + s.get("fallen", 0) + s.get("soldout", 0) > 0:
                print("  [알림] 텔레그램 알림 발송됨")
        else:
            print(f"  [오류] 서버 응답: {resp}")
        return resp
    except Exception as e:
        print(f"[오류] 결과 전송 실패: {e}")
        return {"error": str(e)}


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    import datetime
    print("=" * 60)
    print(f"  네이버 가격 감시 - 로컬 체크")
    print(f"  시작: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    token = _load_token()
    if not token:
        sys.exit(1)

    watches = _get_watchlist(token)
    if not watches:
        print("[종료] 감시 목록이 없습니다.")
        sys.exit(0)

    results = _check_prices(watches)
    _send_results(token, results)

    print("\n완료.")


if __name__ == "__main__":
    main()
