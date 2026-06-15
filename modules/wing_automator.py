"""
Wing(wing.coupang.com) 브라우저 자동화
- URL: /tenants/seller-web/vendor-inventory/modify?vendorInventoryId={id}
- 주요 기능: 자동가격조정 최저가·설정가격 입력, 개당 용량 설정

설치: pip install playwright && playwright install chromium

[DOM 구조 정리 - 2025-05 진단]
  option-pane-table-column 목록 (left 기준):
    left=282  : 행선택 checkbox
    left=330  : 옵션명
    left=630  : 노출상태
    left=830  : 정상가(원)
    left=971  : 판매가(원)
    left=1112 : 단위당가격(원)
    left=1253 : 판매자 자동가격조정 (checkbox toggle)
    left=1253 : 최저가              (sub-col)
    left=1404 : 설정 가격           (sub-col)
    left=1553 : 재고수량
    ...
  .option-pane-table-body : 실제 옵션 행 컨테이너
  .option-pane-table-head  input[type=checkbox] : nth(1) = 자동가격조정 토글
  수량 dimension input  : .option-pane-component input[placeholder='숫자만 입력'] nth(0)
  개당 용량 dimension   : .option-pane-component input[placeholder='숫자만 입력'] nth(1)
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from playwright.async_api import (
        async_playwright, Browser, BrowserContext,
        Page, Playwright, TimeoutError as PWTimeout,
    )
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

WING_URL = "https://wing.coupang.com"
_DBG_DIR = Path(__file__).resolve().parent.parent / "data" / "wing_debug"

# ── 현재 실행 중인 자동화 인스턴스 (GUI → 자동화 신호 전달용) ────────
_current_automator: Optional["WingAutomator"] = None

# ── 일괄 판매요청 중복 실행 방지 플래그 ─────────────────────────────
_BULK_GUARD: dict = {"running": False}


def signal_wing_continue() -> None:
    """GUI의 'Wing 계속 ▶' 버튼이 클릭될 때 호출.
    현재 대기 중인 Wing 자동화를 재개시킨다."""
    if _current_automator is not None:
        _current_automator.signal_user_ready()
        print("[Wing] ▶  GUI 계속 신호 수신")


# ── 데이터 클래스 ──────────────────────────────────────────────────

@dataclass
class BundleInfo:
    qty:            int   # 묶음 수량 (1, 2, 3 ...)
    sale_price:     int   # x1.27 최저가
    original_price: int   # x1.37 판매가 / 설정가격
    image_url:      str = ""  # 옵션별 대표이미지 URL


@dataclass
class WingEditParams:
    seller_product_id: int
    volume:            float          # 개당 용량 (숫자, 0이면 생략)
    volume_unit:       str            # L / ml / cc / kg / g
    bundles:           list[BundleInfo]  # 묶음 목록 (1개~N개)
    detail_image_url:  str = ""       # 상세페이지 이미지 URL


@dataclass
class WingEditResult:
    success: bool
    message: str = ""
    errors:  list[str] = field(default_factory=list)


# ── 메인 클래스 ────────────────────────────────────────────────────

class WingAutomator:
    def __init__(self, username: str, password: str, headless: bool = False):
        if not PLAYWRIGHT_OK:
            raise RuntimeError(
                "playwright 미설치:\n  pip install playwright\n  playwright install chromium"
            )
        self.username = username
        self.password = password
        self.headless = headless
        self._pw:  Optional[Playwright]     = None
        self._br:  Optional[Browser]        = None
        self._ctx: Optional[BrowserContext] = None
        self._pg:  Optional[Page]           = None
        self._log_cb = print  # bulk_publish_all에서 log_cb 연결 후 GUI 표시 가능
        import re as _re_slug
        _slug = _re_slug.sub(r'[^a-zA-Z0-9]', '_', username)[:32] if username else "default"
        self._session_path = (
            Path(__file__).resolve().parent.parent / "data" / f"wing_session_{_slug}.json"
        )
        # GUI ↔ 자동화 동기화 게이트 (threading.Event — 별도 스레드에서 실행되는 asyncio 루프와
        # NiceGUI 메인 루프 간 신호 전달)
        self._user_gate = threading.Event()
        _DBG_DIR.mkdir(parents=True, exist_ok=True)

    async def __aenter__(self):
        await self._start(); return self

    async def __aexit__(self, *_):
        await self._close()

    # ── 브라우저 시작 / 종료 ──────────────────────────────────────

    async def _start(self):
        self._pw = await async_playwright().start()
        self._br = await self._pw.chromium.launch(
            headless=self.headless, args=["--start-maximized"],
        )
        kw: dict = {"viewport": {"width": 1440, "height": 900}}
        if self._session_path.exists():
            try:
                kw["storage_state"] = str(self._session_path)
                print("[Wing] 이전 세션 쿠키 로드")
            except Exception:
                pass
        self._ctx = await self._br.new_context(**kw)
        self._pg  = await self._ctx.new_page()

    async def _close(self):
        # Page / Context / Browser 순서대로 닫기
        for o in (self._pg, self._ctx, self._br):
            try:
                if o: await o.close()
            except Exception:
                pass
        # Playwright 인스턴스는 .stop() — .close()는 존재하지 않아 AttributeError 발생
        # .stop() 없이 루프를 닫으면 Connection.run() 태스크가 좀비로 남아
        # 다음 계정의 async_playwright().start()가 무한 hang함
        try:
            if self._pw: await self._pw.stop()
        except Exception:
            pass

    # ── 스크린샷 ──────────────────────────────────────────────────

    async def _shot(self, name: str):
        try:
            p = _DBG_DIR / f"{int(time.time())}_{name}.png"
            await self._pg.screenshot(path=str(p), full_page=False)
            print(f"[Wing] shot: {p.name}")
        except Exception:
            pass

    # ── 사용자 게이트 ─────────────────────────────────────────────

    def signal_user_ready(self) -> None:
        """GUI의 'Wing 계속 ▶' 버튼 클릭 시 호출 — 자동화 재개."""
        self._user_gate.set()

    async def _wait_for_user(self, msg: str) -> None:
        """GUI 신호(threading.Event)가 올 때까지 비동기 폴링으로 대기.

        asyncio 루프가 별도 스레드에서 실행되기 때문에
        threading.Event 를 0.3초 간격으로 폴링한다.
        """
        print(f"\n[Wing] ⏸️  {msg}\n")
        self._user_gate.clear()
        while not self._user_gate.is_set():
            await asyncio.sleep(0.3)
        print("[Wing] ▶️  계속 진행\n")

    # ── 로그인 ────────────────────────────────────────────────────

    def _is_auth_url(self, url: str) -> bool:
        """xauth/openid-connect 리다이렉트 URL인지 확인."""
        return "xauth.coupang.com" in url or "openid-connect/auth" in url

    async def _ensure_login(self) -> bool:
        log = self._log_cb
        pg = self._pg
        log("[Wing] Wing 접속 중...")
        await pg.goto(WING_URL, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(2)

        cur = pg.url
        if self._is_auth_url(cur) or "login" in cur.lower():
            log(f"[Wing] 로그인 필요 (URL: {cur[:80]})")
            if self._session_path.exists():
                self._session_path.unlink()
                log("[Wing] 스테일 세션 파일 삭제")
            return await self._do_login()

        if await pg.locator(".wing-gnb, .gnb-menu, nav, [class*=sidebar]").count() > 0:
            log("[Wing] 세션 유효 - 로그인 생략")
            return True

        log("[Wing] 로그인 시작...")
        return await self._do_login()

    async def _do_login(self) -> bool:
        log = self._log_cb
        pg = self._pg
        try:
            await pg.goto(WING_URL, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)

            if not self._is_auth_url(pg.url) and "login" not in pg.url.lower():
                log("[Wing] 이미 로그인 상태")
                return True

            await self._shot("01_login_page")
            log("[Wing] 자동 로그인 시도 중...")

            await asyncio.sleep(2)
            await self._shot("01b_login_form")

            _id_filled = False
            for sel in ["#username", "input[name='username']", "input[id='username']",
                        "input[placeholder*='아이디']", "input[placeholder*='ID']"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click()
                        await loc.first.fill(self.username)
                        log(f"[Wing] 아이디 입력 ({sel})")
                        _id_filled = True
                        break
                except Exception:
                    continue

            await asyncio.sleep(0.5)

            _pw_filled = False
            for sel in ["#password", "input[name='password']", "input[id='password']",
                        "input[type='password']"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click()
                        await loc.first.fill(self.password)
                        log("[Wing] 비밀번호 입력 완료")
                        _pw_filled = True
                        break
                except Exception:
                    continue

            await self._shot("02_filled")

            if not _id_filled or not _pw_filled:
                log(f"[Wing] 자동입력 실패 (id={_id_filled}, pw={_pw_filled}) — headless 모드에서 재시도 불가")
            else:
                await asyncio.sleep(0.5)
                for sel in ["#kc-login", "input[type='submit']", "button[type='submit']",
                            "button:has-text('로그인')", "button:has-text('Login')"]:
                    try:
                        loc = pg.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click()
                            log(f"[Wing] 로그인 버튼 클릭 ({sel})")
                            break
                    except Exception:
                        continue

            log("[Wing] 로그인 완료 대기 중 (최대 3분)...")
            for i in range(90):
                await asyncio.sleep(2)
                cur = pg.url
                if not self._is_auth_url(cur) and "login" not in cur.lower():
                    log(f"[Wing] 로그인 성공 감지 ({(i+1)*2}초, URL: {cur[:60]})")
                    break
                if i % 15 == 14:
                    log(f"[Wing] 로그인 대기 중... {(i+1)*2}초 경과")
            else:
                log("[Wing] 로그인 3분 타임아웃")
                return False

            await asyncio.sleep(1)
            await self._shot("03_after_login")
            await self._save_session()
            log("[Wing] 로그인 완료 — 세션 저장")
            return True

        except Exception as e:
            print(f"[Wing] 로그인 실패: {e}")
            await self._shot("login_error")
            return False

    async def _save_session(self):
        try:
            state = await self._ctx.storage_state()
            self._session_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._session_path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception as e:
            print(f"[Wing] 세션 저장 실패: {e}")

    # ── 수정 페이지 이동 ───────────────────────────────────────────

    async def _goto_edit(self, seller_product_id: int) -> bool:
        pg  = self._pg
        pid = str(seller_product_id)
        url = (
            f"{WING_URL}/tenants/seller-web/vendor-inventory/modify"
            f"?vendorInventoryId={pid}&locale=ko_KR"
        )
        print(f"[Wing] 수정 페이지 이동: {url}")
        try:
            # "load" 로 변경: domcontentloaded 는 React SPA 렌더 전에 리턴되므로
            # JS 번들 실행 + 초기 API 호출까지 포함한 load 이벤트까지 대기
            await pg.goto(url, wait_until="load", timeout=40_000)
            await asyncio.sleep(1.0)
            await self._shot("04_edit_page")
            if await self._is_edit_page():
                print("[Wing] 수정 페이지 진입 성공")
                return True
        except Exception as e:
            print(f"[Wing] goto 실패: {e}")

        print("[Wing] 직접 URL 실패 - 상품 검색 시도")
        return await self._search_and_open(pid)

    async def _is_edit_page(self) -> bool:
        pg = self._pg
        for sel in ["button:has-text('상품등록')", "button:has-text('임시 저장')",
                    "button:has-text('저장')", "button:has-text('수정완료')"]:
            if await pg.locator(sel).count() > 0:
                return True
        return False

    async def _search_and_open(self, pid: str) -> bool:
        pg = self._pg
        for url in [f"{WING_URL}/tenants/seller-web/vendor-inventory/list"]:
            try:
                await pg.goto(url, wait_until="domcontentloaded", timeout=12_000)
                await asyncio.sleep(2)
                for sel in ["input[placeholder*='검색']", "input[placeholder*='상품번호']"]:
                    if await pg.locator(sel).count() > 0:
                        await pg.locator(sel).first.fill(pid)
                        await pg.keyboard.press("Enter")
                        await asyncio.sleep(2)
                        for edit_sel in ["a:has-text('수정')", "button:has-text('수정')"]:
                            if await pg.locator(edit_sel).count() > 0:
                                await pg.locator(edit_sel).first.click()
                                await asyncio.sleep(2)
                                if await self._is_edit_page():
                                    return True
                        break
            except Exception:
                continue
        print(f"[Wing] 상품 {pid} 수정 페이지 진입 실패")
        await self._shot("edit_fail")
        return False

    # ── 다이얼로그 닫기 ───────────────────────────────────────────

    async def _dismiss_dialogs(self):
        """열려있는 모달 / 다이얼로그 닫기"""
        pg = self._pg
        await pg.keyboard.press("Escape")
        await asyncio.sleep(0.4)
        for sel in [
            "button:has-text('닫기')",
            "[class*='modal'] button:has-text('취소')",
            "[class*='dialog'] button:has-text('닫기')",
        ]:
            try:
                if await pg.locator(sel).count() > 0:
                    await pg.locator(sel).first.click()
                    await asyncio.sleep(0.3)
                    print(f"[Wing] 다이얼로그 닫기: {sel}")
                    break
            except Exception:
                pass

    # ── 자동가격조정 (핵심 기능) ───────────────────────────────────

    async def _fill_auto_pricing(self, min_price: int, wish_price: int) -> list[str]:
        """
        Wing 옵션 테이블에서 판매자 자동가격조정 최저가·설정가격 입력.

        알고리즘:
        1. 다이얼로그 닫기
        2. 옵션 테이블 영역(scroll ~1100)으로 이동
        3. 판매자 자동가격조정 토글(option-pane-table-head 내 두 번째 checkbox) ON 확인
        4. '최저가' 컬럼 헤더의 left 좌표 파악
        5. .option-pane-table-body 내 text input 중 동일 left 위치의 것에 값 입력
           (React native setter + dispatchEvent 로 state 갱신)
        """
        errors: list[str] = []
        pg = self._pg

        # 1. 다이얼로그 닫기
        await self._dismiss_dialogs()
        await asyncio.sleep(0.3)

        # 2. 옵션 테이블 영역으로 스크롤
        await pg.evaluate("window.scrollTo(0, 1100)")
        await asyncio.sleep(0.5)
        await self._shot("20_option_table")

        # 3. 판매자 자동가격조정 토글 확인 / 활성화
        #    option-pane-table-head 내 checkbox 는 2개:
        #      nth(0) = 행 전체 선택(all-select)
        #      nth(1) = 판매자 자동가격조정 toggle
        toggle_sel = ".option-pane-table-head input[type='checkbox']"
        toggle_count = await pg.locator(toggle_sel).count()
        if toggle_count >= 2:
            toggle_loc = pg.locator(toggle_sel).nth(1)
            try:
                is_checked = await toggle_loc.is_checked()
                if not is_checked:
                    await toggle_loc.click()
                    await asyncio.sleep(0.8)
                    print("[Wing] 자동가격조정 토글 ON 으로 변경")
                else:
                    print("[Wing] 자동가격조정 이미 활성화됨")
            except Exception as e:
                print(f"[Wing] 자동가격조정 토글 확인 실패: {e}")
                errors.append(f"자동가격조정 토글 오류: {e}")
        else:
            print(f"[Wing] 자동가격조정 토글 미발견 (header checkbox 수: {toggle_count})")
            errors.append(f"자동가격조정 토글 미발견 (count={toggle_count})")

        await asyncio.sleep(0.3)

        # 4. 컬럼 헤더 좌표 파악 + 최저가 우선 입력
        #    (설정가격 input은 최저가 입력 후 React re-render 이후에 등장할 수 있음)
        js_result: dict = await pg.evaluate(
            """
            (args) => {
                const {minPrice, wishPrice} = args;

                /* ── 컬럼 헤더 좌표 파악 ── */
                const headerCols = Array.from(
                    document.querySelectorAll('.option-pane-table-column')
                );
                let minLeft = -1, wishLeft = -1;
                for (const col of headerCols) {
                    const text = col.textContent.trim();
                    const r    = col.getBoundingClientRect();
                    if (text === '최저가')                                minLeft  = Math.round(r.left);
                    if (text === '설정 가격' || text === '설정가격')      wishLeft = Math.round(r.left);
                }

                /* ── React native setter (controlled input 우회) ── */
                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;

                const setAndDispatch = (inp, val) => {
                    nativeSetter.call(inp, String(val));
                    inp.dispatchEvent(new Event('input',  {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                };

                /* ── option-pane-table-body 내 모든 input 탐색 ── */
                const bodyInputs = Array.from(
                    document.querySelectorAll('.option-pane-table-body input')
                ).filter(inp => {
                    const t = (inp.type || '').toLowerCase();
                    return t === 'text' || t === 'number' || t === '';
                });

                /* 디버깅용 위치 목록 */
                const bodyPositions = bodyInputs.map(inp => ({
                    left:    Math.round(inp.getBoundingClientRect().left),
                    type:    inp.type,
                    value:   inp.value,
                    visible: inp.offsetParent !== null,
                }));

                const filled = [];
                const MARGIN = 50;   // px 허용 오차

                for (const inp of bodyInputs) {
                    const left = Math.round(inp.getBoundingClientRect().left);

                    if (minLeft > 0 && Math.abs(left - minLeft) < MARGIN) {
                        setAndDispatch(inp, minPrice);
                        filled.push({field: 'minPrice', left, value: minPrice});
                    } else if (wishLeft > 0 && Math.abs(left - wishLeft) < MARGIN) {
                        setAndDispatch(inp, wishPrice);
                        filled.push({field: 'wishPrice', left, value: wishPrice});
                    }
                }

                return {
                    minLeft, wishLeft,
                    bodyInputCount: bodyInputs.length,
                    bodyPositions,
                    filled,
                };
            }
            """,
            {"minPrice": min_price, "wishPrice": wish_price},
        )

        print(f"[Wing] 1차 JS 결과: {js_result}")
        print(f"[Wing] body inputs: {js_result.get('bodyPositions', [])}")

        filled_fields = {f["field"] for f in js_result.get("filled", [])}

        if "minPrice" not in filled_fields:
            errors.append(
                f"최저가 input 미발견 (minLeft={js_result.get('minLeft')}, "
                f"bodyInputs={js_result.get('bodyInputCount')})"
            )
        else:
            print(f"[Wing] 최저가 입력 완료: {min_price:,}원")

        # 5. 설정가격 input 재탐색 (최저가 입력 후 React re-render 대기)
        #    설정가격 컬럼이 최저가 입력 전에는 비활성이었다가 활성화되는 경우 처리
        if "wishPrice" not in filled_fields:
            await asyncio.sleep(0.8)   # React state 갱신 대기
            js_wish: dict = await pg.evaluate(
                """
                (args) => {
                    const {wishPrice, wishLeft} = args;
                    const MARGIN = 50;

                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    ).set;
                    const setAndDispatch = (inp, val) => {
                        nativeSetter.call(inp, String(val));
                        inp.dispatchEvent(new Event('input',  {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                    };

                    const bodyInputs = Array.from(
                        document.querySelectorAll('.option-pane-table-body input')
                    ).filter(inp => {
                        const t = (inp.type || '').toLowerCase();
                        return t === 'text' || t === 'number' || t === '';
                    });

                    const positions = bodyInputs.map(inp => ({
                        left: Math.round(inp.getBoundingClientRect().left),
                        value: inp.value,
                    }));

                    /* wishLeft 위치에 input 이 생겼는지 확인 */
                    for (const inp of bodyInputs) {
                        const left = Math.round(inp.getBoundingClientRect().left);
                        if (wishLeft > 0 && Math.abs(left - wishLeft) < MARGIN) {
                            setAndDispatch(inp, wishPrice);
                            return {found: true, method: 'wishLeft', left, positions};
                        }
                    }

                    return {found: false, positions};
                }
                """,
                {
                    "wishPrice": wish_price,
                    "wishLeft":  js_result.get("wishLeft", -1),
                },
            )
            print(f"[Wing] 2차 설정가격 탐색: {js_wish}")
            if js_wish.get("found"):
                method = js_wish.get("method", "")
                if "fallback" in method:
                    print(f"[Wing] 설정가격 -> 판매가 컬럼 fallback 입력: {wish_price:,}원")
                else:
                    print(f"[Wing] 설정가격 입력 완료: {wish_price:,}원")
                filled_fields.add("wishPrice")
            else:
                # 설정가격 컬럼 input 없음 = display-only column
                # 이 경우 최저가만 설정하면 자동가격조정이 동작함
                print(f"[Wing] 설정가격 input 없음 (display-only) - 최저가만 설정됨")
                # errors 에 추가하지 않음 (정상 동작 범위)

        await self._shot("21_after_pricing")
        return errors

    # ── 개당 용량 차원 값 추가 ──────────────────────────────────────

    async def _fill_volume(self, volume: float, unit: str) -> list[str]:
        """
        '개당 용량' 차원에 값 추가.

        핵심 전략:
        - 값 입력·단위 선택·추가 클릭을 단일 JS evaluate 로 처리
          (Python fill → JS click 분리 시 React state 비동기 문제 발생)
        - 섹션 탐지: textContent 텍스트 매칭 대신 volume input(nth 1) 을 앵커로
          DOM 을 위로 순회하며 '추가' 버튼이 있는 컨테이너를 찾음
          (아이콘/불릿 등으로 인한 텍스트 매칭 실패 방지)
        - 추가 버튼은 JS click — 모달 오버레이가 Playwright click 을 차단
        """
        errors: list[str] = []
        pg = self._pg
        vol_str = f"{volume:g}"

        # ── 중복 태그 확인 (개당 용량 섹션 한정) ────────────────────────
        # ⚠️ 전체 textContent 검색은 "10W-40"의 "4" 등 오탐 가능 → nth(1) input 앵커 기준
        already_exists: bool = await pg.evaluate(
            """
            (args) => {
                const {volStr, unit} = args;
                const tagFull = volStr + unit;

                /* nth(1) input 을 앵커로 상위 컨테이너 탐색 */
                const inputs = Array.from(document.querySelectorAll(
                    ".option-pane-component input[placeholder='숫자만 입력']"
                ));
                if (inputs.length < 2) return false;

                let el = inputs[1].parentElement;
                for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
                    const tags = el.querySelectorAll(
                        '[class*="tag"],[class*="chip"],.option-pane-dimension-tag'
                    );
                    for (const tag of tags) {
                        const txt = tag.textContent.trim();
                        if (txt === volStr || txt === tagFull) return true;
                    }
                    /* 추가 버튼을 포함한 컨테이너에 도달하면 중단 */
                    if (Array.from(el.querySelectorAll('button'))
                            .some(b => b.textContent.trim() === '추가')) break;
                }
                return false;
            }
            """,
            {"volStr": vol_str, "unit": unit},
        )
        if already_exists:
            print(f"[Wing] 개당 용량 {vol_str}{unit} 이미 등록됨 - 건너뜀")
            return errors

        # ── 모달 닫기 후 단일 JS 로 입력·단위·추가 클릭 ──────────────────
        # 수량 추가 후 Wing 이 "입력된 옵션 값이 없습니다" 팝업을 띄우면 먼저 닫음
        await self._dismiss_dialogs()
        await asyncio.sleep(0.3)

        result: str = await pg.evaluate(
            """
            (args) => {
                const {volStr, unit} = args;

                /* ── React native setter ── */
                const nativeInpSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set;
                const nativeSelSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value'
                )?.set;

                function setInp(inp, v) {
                    if (nativeInpSetter) nativeInpSetter.call(inp, String(v));
                    else inp.value = String(v);
                    inp.dispatchEvent(new Event('input',  {bubbles:true}));
                    inp.dispatchEvent(new Event('change', {bubbles:true}));
                    inp.dispatchEvent(new Event('blur',   {bubbles:true}));
                }

                /* ── 개당 용량 input 을 nth(1) 로 특정 ── */
                const inputs = Array.from(document.querySelectorAll(
                    ".option-pane-component input[placeholder='숫자만 입력']"
                ));
                if (inputs.length < 2) return 'inputs-count:' + inputs.length;

                const volInp = inputs[1];

                /* ① 값 입력 */
                setInp(volInp, volStr);

                /* ② DOM 위로 순회 → 추가 버튼이 있는 컨테이너 찾기 */
                let container = volInp.parentElement;
                let addBtn    = null;
                let unitSet   = false;

                for (let depth = 0; depth < 10 && container; depth++) {
                    /* 단위 select */
                    if (!unitSet) {
                        const sel = container.querySelector('select');
                        if (sel) {
                            let opt = Array.from(sel.options).find(
                                o => o.value === unit || o.text.trim() === unit
                            );
                            if (!opt) opt = Array.from(sel.options).find(
                                o => o.text.trim().includes(unit) ||
                                     unit.includes(o.value.trim())
                            );
                            if (opt) {
                                if (nativeSelSetter) nativeSelSetter.call(sel, opt.value);
                                else sel.value = opt.value;
                                sel.dispatchEvent(new Event('input',  {bubbles:true}));
                                sel.dispatchEvent(new Event('change', {bubbles:true}));
                                unitSet = true;
                            }
                        }
                        /* 버튼식 단위 토글 */
                        if (!unitSet) {
                            for (const btn of container.querySelectorAll('button')) {
                                if (btn.textContent.trim() === unit) {
                                    btn.click(); unitSet = true; break;
                                }
                            }
                        }
                    }

                    /* 추가 버튼 탐색 */
                    if (!addBtn) {
                        addBtn = Array.from(container.querySelectorAll('button'))
                            .find(b => b.textContent.trim() === '추가');
                    }

                    if (addBtn) break;   /* 추가 버튼을 찾으면 중단 */
                    container = container.parentElement;
                }

                if (!addBtn) return 'no-add-btn';

                addBtn.click();
                return unitSet ? 'ok' : 'ok-no-unit';
            }
            """,
            {"volStr": vol_str, "unit": unit},
        )

        print(f"[Wing] 개당 용량 JS 결과: {result}")

        if result.startswith("ok"):
            await asyncio.sleep(0.6)
            if result == "ok-no-unit":
                print(f"[Wing] 개당 용량 추가: {vol_str} (단위 {unit} 설정 실패)")
                errors.append(f"개당 용량 단위 설정 실패 ({unit})")
            else:
                print(f"[Wing] 개당 용량 추가: {vol_str}{unit}")

            # Wing UI 중복 알림 팝업 감지 및 닫기
            dup_alert: str | None = await pg.evaluate("""
                () => {
                    for (const sel of [
                        '[role="alert"]', '[class*="toast"]', '[class*="snack"]',
                        '[class*="notification"]', '[class*="Error"]',
                        '[class*="error-message"]', '[class*="duplicate"]',
                    ]) {
                        for (const el of document.querySelectorAll(sel)) {
                            if (!el.offsetParent) continue;
                            const t = el.textContent.trim();
                            if (t && (t.includes('이미') || t.includes('중복') ||
                                      t.includes('존재') || t.includes('already'))) {
                                const btn = el.querySelector(
                                    'button,[class*="close"],[class*="dismiss"]'
                                );
                                if (btn) btn.click();
                                return t.substring(0, 80);
                            }
                        }
                    }
                    return null;
                }
            """)
            if dup_alert:
                print(f"[Wing] 개당 용량 중복 알림 — 이미 등록됨: {dup_alert}")
        else:
            errors.append(f"개당 용량 추가 실패: {result}")
            print(f"[Wing] 개당 용량 추가 실패: {result}")

        return errors

    # ── 수량 차원 활성화 (설정함) ──────────────────────────────────

    async def _ensure_qty_dimension_enabled(self) -> bool:
        """
        수량 차원이 '설정 안 함'이면 '설정함' 버튼 클릭.
        이미 활성화(input 보임)면 즉시 True 반환.
        """
        pg = self._pg
        if await pg.locator(
            ".option-pane-component input[placeholder='숫자만 입력']"
        ).count() >= 1:
            return True   # 이미 활성화

        clicked: bool = await pg.evaluate("""
            () => {
                const comps = Array.from(
                    document.querySelectorAll('.option-pane-component')
                );
                for (const comp of comps) {
                    let hasLabel = false;
                    for (const el of comp.querySelectorAll('label,span,p,div')) {
                        const t = el.textContent.trim();
                        if (t === '수량' || t.startsWith('수량 ')) {
                            hasLabel = true; break;
                        }
                    }
                    if (!hasLabel) continue;
                    for (const btn of comp.querySelectorAll('button')) {
                        if (btn.textContent.trim() === '설정함') {
                            btn.click(); return true;
                        }
                    }
                }
                return false;
            }
        """)
        if clicked:
            await asyncio.sleep(0.6)
            print("[Wing] 수량 차원 '설정함' 클릭")
        else:
            print("[Wing] 수량 차원 '설정함' 버튼 미발견 (이미 활성 가능)")
        return True

    # ── 수량 차원에 묶음 수량 값 추가 ──────────────────────────────

    async def _add_qty_dimension_value(self, qty: int) -> bool:
        """
        '수량' 차원 input 에 qty 입력 후 '추가' 클릭.
        이미 같은 값이 태그로 존재하면 건너뜀.
        Returns True if added or already present.
        """
        pg = self._pg
        qty_str = str(qty)

        # 중복 태그 체크
        already: bool = await pg.evaluate(
            """
            (q) => {
                const sel = '.option-pane-component [class*="tag"],'
                          + '.option-pane-component [class*="chip"],'
                          + '.option-pane-component .option-pane-dimension-tag';
                return Array.from(document.querySelectorAll(sel))
                    .some(t => {
                        const txt = t.textContent.trim();
                        return txt === q + '개' || txt === q;
                    });
            }
            """,
            qty_str,
        )
        if already:
            print(f"[Wing] 수량 {qty}개 이미 등록됨 - 건너뜀")
            return True

        # 비활성화 상태면 설정함 클릭
        await self._ensure_qty_dimension_enabled()

        dim_inputs = pg.locator(
            ".option-pane-component input[placeholder='숫자만 입력']"
        )
        if await dim_inputs.count() < 1:
            print("[Wing] 수량 차원 input 미발견")
            return False

        qty_inp = dim_inputs.nth(0)
        try:
            await qty_inp.click()
            await qty_inp.fill(qty_str)
        except Exception as e:
            print(f"[Wing] 수량 값 입력 실패: {e}")
            return False

        add_btns = pg.locator(".option-pane-component button:has-text('추가')")
        if await add_btns.count() < 1:
            print("[Wing] 수량 추가 버튼 미발견")
            return False

        try:
            await add_btns.nth(0).click()
            await asyncio.sleep(0.5)
            print(f"[Wing] 수량 {qty}개 차원값 추가")
            return True
        except Exception as e:
            print(f"[Wing] 수량 추가 버튼 클릭 실패: {e}")
            return False

    # ── 옵션 테이블 행별 가격 일괄 입력 ───────────────────────────

    async def _set_all_row_prices(self, bundles: list[BundleInfo]) -> list[str]:
        """
        수량 차원값 추가 후 옵션 테이블의 각 행에 가격 입력.

        2단계 입력 전략 (자동가격조정 ON 시 판매가 input 이 width=0 으로 숨겨짐):
        - Phase 1: 자동가격조정 OFF → 판매가(원) 입력 (original_price ×1.37)
        - Phase 2: 자동가격조정 ON  → 최저가 입력     (sale_price    ×1.27)

        행-번들 매핑: Y좌표 오름차순 정렬 인덱스 = qty 오름차순 번들 인덱스
        """
        errors: list[str] = []
        pg = self._pg

        sorted_bundles = sorted(bundles, key=lambda b: b.qty)
        bundle_map = {
            str(b.qty): {
                "qty":            b.qty,
                "original_price": b.original_price,
                "sale_price":     b.sale_price,
            }
            for b in sorted_bundles
        }

        # 자동가격조정 토글 위치
        toggle_sel = ".option-pane-table-head input[type='checkbox']"
        has_toggle = await pg.locator(toggle_sel).count() >= 2
        toggle     = pg.locator(toggle_sel).nth(1) if has_toggle else None

        # ── JS 헬퍼: 행-입력 추출 + Y버킷 그룹핑 후 지정 컬럼에 값 입력 ────
        # colLeft: 채울 컬럼의 header left (동적 감지) — -1이면 전체 입력 중 가장 오른쪽
        _FILL_JS = """
            (args) => {
                const {bundleMap, targetLeft, margin} = args;

                const nativeSetter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                )?.set;
                function setVal(inp, v) {
                    if (nativeSetter) nativeSetter.call(inp, String(v));
                    else inp.value = String(v);
                    inp.dispatchEvent(new Event('input',  {bubbles:true}));
                    inp.dispatchEvent(new Event('change', {bubbles:true}));
                    inp.dispatchEvent(new Event('blur',   {bubbles:true}));
                }

                const tbody = document.querySelector('.option-pane-table-body');
                if (!tbody) return {error: 'tbody not found'};

                const allInps = Array.from(tbody.querySelectorAll('input')).filter(i => {
                    const t = (i.type || '').toLowerCase();
                    return (t === 'text' || t === 'number' || t === '')
                        && i.getBoundingClientRect().width > 0;
                });
                if (allInps.length === 0) return {error: 'no inputs', filled: []};

                /* Y 버킷으로 행 그룹핑 (40 px) */
                const rowMap = new Map();
                for (const inp of allInps) {
                    const rect = inp.getBoundingClientRect();
                    const y    = Math.round(rect.top / 40) * 40;
                    if (!rowMap.has(y)) rowMap.set(y, []);
                    rowMap.get(y).push({left: Math.round(rect.left), inp});
                }
                const rows = [...rowMap.entries()].sort((a, b) => a[0] - b[0]);
                const sortedBundles = Object.values(bundleMap).sort((a, b) => a.qty - b.qty);

                const filled = [];
                rows.forEach(([y, inps], idx) => {
                    const bundle = sortedBundles[idx];
                    if (!bundle) return;
                    for (const {left, inp} of inps) {
                        if (Math.abs(left - targetLeft) < margin) {
                            setVal(inp, bundle[args.priceKey]);
                            filled.push({row: idx+1, qty: bundle.qty, val: bundle[args.priceKey]});
                        }
                    }
                });
                return {rowCount: rows.length, allInpsCount: allInps.length, filled};
            }
        """

        # ── Phase 1: 자동가격조정 OFF → 판매가(원) 입력 ─────────────────────
        if has_toggle:
            try:
                if await toggle.is_checked():
                    await toggle.click()
                    await asyncio.sleep(0.8)
                    print("[Wing] 자동가격조정 OFF (판매가 입력 준비)")
                else:
                    print("[Wing] 자동가격조정 이미 OFF")
            except Exception as e:
                errors.append(f"자동가격조정 OFF 실패: {e}")
                print(f"[Wing] 자동가격조정 OFF 실패: {e}")

        # 판매가 컬럼 헤더 left 동적 감지
        sale_left: int = await pg.evaluate("""
            () => {
                for (const col of document.querySelectorAll('.option-pane-table-column')) {
                    const txt = col.textContent.trim();
                    if (txt === '판매가' || txt === '판매가(원)') {
                        return Math.round(col.getBoundingClientRect().left);
                    }
                }
                return -1;
            }
        """)
        print(f"[Wing] Phase1: 판매가 헤더 left={sale_left}")

        res1: dict = await pg.evaluate(
            _FILL_JS,
            {"bundleMap": bundle_map, "targetLeft": sale_left, "margin": 80, "priceKey": "original_price"},
        )
        print(f"[Wing] Phase1 판매가 입력: rows={res1.get('rowCount')}, "
              f"inps={res1.get('allInpsCount')}, filled={res1.get('filled')}")
        if "error" in res1:
            errors.append(f"Phase1 판매가 오류: {res1['error']}")
        elif not res1.get("filled"):
            errors.append(f"판매가 입력 실패 (saleLeft={sale_left}, "
                          f"rows={res1.get('rowCount')}, inps={res1.get('allInpsCount')})")
        else:
            for f in res1["filled"]:
                print(f"[Wing]   행{f['row']} (qty={f['qty']}) 판매가: {f['val']:,}원")

        await asyncio.sleep(0.5)

        # ── Phase 2: 자동가격조정 ON → 최저가 입력 ──────────────────────────
        if has_toggle:
            try:
                if not await toggle.is_checked():
                    await toggle.click()
                    await asyncio.sleep(0.8)
                    print("[Wing] 자동가격조정 ON (최저가 입력 준비)")
                else:
                    print("[Wing] 자동가격조정 이미 ON")
            except Exception as e:
                errors.append(f"자동가격조정 ON 실패: {e}")
                print(f"[Wing] 자동가격조정 ON 실패: {e}")
        else:
            print("[Wing] 자동가격조정 토글 미발견 — 최저가만 시도")

        # 최저가 컬럼 헤더 left 동적 감지
        min_left: int = await pg.evaluate("""
            () => {
                for (const col of document.querySelectorAll('.option-pane-table-column')) {
                    if (col.textContent.trim() === '최저가') {
                        return Math.round(col.getBoundingClientRect().left);
                    }
                }
                return -1;
            }
        """)
        print(f"[Wing] Phase2: 최저가 헤더 left={min_left}")

        res2: dict = await pg.evaluate(
            _FILL_JS,
            {"bundleMap": bundle_map, "targetLeft": min_left, "margin": 80, "priceKey": "sale_price"},
        )
        print(f"[Wing] Phase2 최저가 입력: rows={res2.get('rowCount')}, "
              f"inps={res2.get('allInpsCount')}, filled={res2.get('filled')}")
        if "error" in res2:
            errors.append(f"Phase2 최저가 오류: {res2['error']}")
        elif not res2.get("filled"):
            errors.append(f"최저가 입력 실패 (minLeft={min_left}, "
                          f"rows={res2.get('rowCount')}, inps={res2.get('allInpsCount')})")
        else:
            for f in res2["filled"]:
                print(f"[Wing]   행{f['row']} (qty={f['qty']}) 최저가: {f['val']:,}원")

        total = len(res1.get("filled", [])) + len(res2.get("filled", []))
        print(f"[Wing] 행별 가격 입력 완료 — 총 {total}개 필드")
        return errors

    # ── 저장 ──────────────────────────────────────────────────────

    async def _save(self, draft: bool = False) -> bool:
        """
        draft=False (즉시등록): 고정 footer의 '상품등록' 버튼 클릭 → 바로 판매
        draft=True  (임시저장): '임시 저장' 버튼 클릭 → Wing 임시저장 상태로 저장
                               이후 bulk_publish_all() 로 일괄 판매요청 가능
        """
        pg = self._pg

        # 저장 전 모달/다이얼로그 닫기
        await self._dismiss_dialogs()
        await asyncio.sleep(0.5)

        # 페이지 상단으로 스크롤
        await pg.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.4)
        await self._shot("30_before_save")

        # 페이지 버튼 파악 (디버그용)
        btn_info = await pg.evaluate("""
            () => Array.from(document.querySelectorAll('button'))
                .filter(b => b.offsetParent !== null)
                .map(b => ({
                    text: b.textContent.trim().replace(/\\s+/g, ' '),
                    top: Math.round(b.getBoundingClientRect().top + window.scrollY),
                }))
                .filter(b => b.text.length > 0 && b.text.length < 20)
        """)
        print(f"[Wing] 페이지 버튼 목록: {btn_info}")

        if draft:
            # ── 임시저장 모드: '임시 저장' 버튼 클릭 ────────────────────
            # 고정 footer의 왼쪽(흰색) 버튼이 '임시 저장'
            print("[Wing] 임시저장 모드 — '임시 저장' 버튼 클릭")
            clicked = await pg.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    // 정확히 "임시 저장" 텍스트인 버튼 (공백 포함)
                    for (const btn of btns) {
                        const t = btn.textContent.trim().replace(/\\s+/g, ' ');
                        if ((t === '임시 저장' || t === '임시저장') && btn.offsetParent) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                }
            """)
            if clicked:
                print(f"[Wing] JS '임시 저장' 버튼 클릭: {clicked!r}")
                await asyncio.sleep(3)
                await self._shot("31_after_save")
                return True
            # fallback
            for sel in ["button:has-text('임시 저장')", "button:has-text('임시저장')"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=8_000)
                        await asyncio.sleep(3)
                        await self._shot("31_after_save")
                        print(f"[Wing] Playwright '임시 저장' 버튼 클릭: {sel}")
                        return True
                except Exception as e:
                    print(f"[Wing] '임시 저장' 버튼 클릭 실패 ({sel}): {e}")
        else:
            # ── 즉시등록 모드: 고정 footer '상품등록' 버튼 클릭 ──────────
            print("[Wing] 즉시등록 모드 — '상품등록' 버튼 클릭")
            save_keywords = ['상품등록', '저장', '수정완료', '수정 완료', '확인', '적용']
            exclude_keywords = ['매뉴얼', '임시', '취소', '닫기', '삭제']

            clicked = await pg.evaluate("""
                (args) => {
                    const {saveKw, excludeKw} = args;
                    const btns = Array.from(document.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    for (const kw of saveKw) {
                        const matches = btns.filter(b => {
                            const t = b.textContent.trim().replace(/\\s+/g, ' ');
                            if (!t.includes(kw)) return false;
                            for (const ex of excludeKw) {
                                if (t.includes(ex)) return false;
                            }
                            return true;
                        });
                        if (matches.length > 0) {
                            const btn = matches[matches.length - 1];
                            btn.click();
                            return btn.textContent.trim();
                        }
                    }
                    return null;
                }
            """, {"saveKw": save_keywords, "excludeKw": exclude_keywords})

            if clicked:
                print(f"[Wing] JS '상품등록' 버튼 클릭: {clicked!r}")
                await asyncio.sleep(3)

                # 즉시등록 시에도 "판매요청 하시겠습니까?" 팝업이 뜰 수 있음
                try:
                    popup_btn = await pg.evaluate_handle("""
                        () => {
                            for (const sel of ['[class*="modal"] button', '[role="dialog"] button', '[class*="popup"] button']) {
                                for (const btn of document.querySelectorAll(sel)) {
                                    const t = btn.textContent.trim();
                                    if ((t === '상품등록' || t === '확인') && btn.offsetParent) return btn;
                                }
                            }
                            return null;
                        }
                    """)
                    popup_el = popup_btn.as_element()
                    if popup_el and await popup_el.is_visible():
                        await popup_el.click()
                        print("[Wing] 즉시등록 팝업 '상품등록' 확인 클릭")
                        await asyncio.sleep(3)
                except Exception:
                    pass

                await self._shot("31_after_save")
                return True

            # fallback
            for sel in ["button:has-text('저장')", "button:has-text('수정완료')"]:
                try:
                    loc = pg.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=8_000)
                        await asyncio.sleep(3)
                        await self._shot("31_after_save")
                        print(f"[Wing] Playwright 저장 버튼 클릭: {sel}")
                        return True
                except Exception as e:
                    print(f"[Wing] 저장 버튼 클릭 실패 ({sel}): {e}")

        print("[Wing] 저장 버튼 미발견")
        await self._shot("save_missing")
        return False

    # ── 옵션별 이미지 URL 등록 ─────────────────────────────────────

    async def _set_option_images(self, bundles: list[BundleInfo]) -> list[str]:
        """
        Wing 상품이미지 섹션 → '옵션별 등록' 탭 → 각 행 이미지 URL 등록.

        흐름:
        1. 페이지 상단 스크롤
        2. '옵션별 등록' 탭/버튼 클릭
        3. 각 번들(qty 기준 행 매칭) → URL 입력 버튼 → URL 기입 → 확인
        """
        errors: list[str] = []
        pg = self._pg

        valid_bundles = [b for b in bundles if b.image_url]
        if not valid_bundles:
            print("[Wing] 옵션 이미지 URL 없음 - 이미지 등록 건너뜀")
            return errors

        # 이미지 섹션은 상단에 위치 — 상단 스크롤
        await pg.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(0.5)

        # Wing 모달 닫기 (가격 미입력 등으로 뜰 수 있는 "옵션 값이 없습니다" 팝업)
        # Playwright click은 modal overlay가 pointer events를 가로채므로 JS click 우선 사용
        await self._dismiss_dialogs()
        await asyncio.sleep(0.4)
        await self._shot("25_before_option_images")

        # '옵션별 등록' 탭: JS click 우선 (modal 아래 요소도 클릭 가능)
        tab_clicked: bool = await pg.evaluate("""
            () => {
                for (const el of document.querySelectorAll('button,label,[role="tab"]')) {
                    const t = el.textContent.trim();
                    if (t === '옵션별 등록') {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        if tab_clicked:
            await asyncio.sleep(0.8)
            print("[Wing] '옵션별 등록' 탭 JS 클릭")
        else:
            # Playwright fallback (짧은 타임아웃)
            for sel in [
                "button:has-text('옵션별 등록')",
                "[role='tab']:has-text('옵션별 등록')",
                "label:has-text('옵션별 등록')",
            ]:
                try:
                    if await pg.locator(sel).count() > 0:
                        await pg.locator(sel).first.click(timeout=5_000)
                        await asyncio.sleep(0.8)
                        print(f"[Wing] '옵션별 등록' 탭 클릭: {sel}")
                        tab_clicked = True
                        break
                except Exception:
                    continue

        if not tab_clicked:
            print("[Wing] '옵션별 등록' 탭 미발견 - 옵션 이미지 등록 건너뜀")
            errors.append("옵션별 등록 탭 미발견")
            return errors

        await self._shot("25_option_image_tab")

        # 각 번들 행 → 이미지 URL 입력
        for bundle in sorted(valid_bundles, key=lambda b: b.qty):
            qty     = bundle.qty
            img_url = bundle.image_url

            # 행 찾기 + URL 버튼 클릭
            click_res: dict = await pg.evaluate(
                """
                (args) => {
                    const {qty} = args;
                    const qtyStr = String(qty) + '개';

                    /* qty 텍스트 포함 행 탐색 */
                    const candidates = Array.from(
                        document.querySelectorAll('td, [class*="row"], [class*="cell"], [class*="option-item"]')
                    );
                    for (const el of candidates) {
                        if (!el.textContent.includes(qtyStr)) continue;

                        /* 가장 가까운 row 컨테이너 */
                        const row = el.closest('tr, [class*="row"], [class*="item"]') || el.parentElement;
                        if (!row) continue;

                        /* URL 버튼 탐색 */
                        for (const btn of row.querySelectorAll('button,[role="button"]')) {
                            const t = btn.textContent.trim();
                            if (t.includes('URL') || t.includes('이미지') ||
                                t.includes('등록') || t.includes('업로드') || t.includes('추가')) {
                                btn.click();
                                return {found: true, method: 'button', rowText: el.textContent.trim().substring(0, 50)};
                            }
                        }

                        /* inline input 이미 있으면 바로 반환 */
                        if (row.querySelector('input[type="url"],input[type="text"]')) {
                            return {found: true, method: 'direct', rowText: el.textContent.trim().substring(0, 50)};
                        }
                    }
                    return {found: false};
                }
                """,
                {"qty": qty},
            )

            if not click_res.get("found"):
                print(f"[Wing] {qty}개 옵션 이미지 행 미발견")
                errors.append(f"{qty}개 옵션 이미지 행 미발견")
                continue

            method = click_res.get("method", "")
            print(f"[Wing] {qty}개 이미지 행 클릭 ({method}): {click_res.get('rowText','')}")
            await asyncio.sleep(0.5)

            url_filled = False

            # method='direct' : 행 내에 이미 input 이 존재 → JS 로 직접 URL 주입
            if method == "direct":
                js_direct: dict = await pg.evaluate(
                    """
                    (args) => {
                        const {qty, imgUrl} = args;
                        const qtyStr = String(qty) + '개';

                        const nativeSetter = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        )?.set;
                        function setInp(inp, v) {
                            if (nativeSetter) nativeSetter.call(inp, String(v));
                            else inp.value = String(v);
                            inp.dispatchEvent(new Event('input',  {bubbles:true}));
                            inp.dispatchEvent(new Event('change', {bubbles:true}));
                            inp.dispatchEvent(new Event('blur',   {bubbles:true}));
                        }

                        const candidates = Array.from(
                            document.querySelectorAll(
                                'td, [class*="row"], [class*="cell"], [class*="option-item"]'
                            )
                        );
                        for (const el of candidates) {
                            if (!el.textContent.includes(qtyStr)) continue;
                            const row = el.closest('tr,[class*="row"],[class*="item"]')
                                     || el.parentElement;
                            if (!row) continue;
                            /* 텍스트/url 타입 input 우선, 없으면 첫 번째 input */
                            const inp = row.querySelector(
                                'input[type="url"],input[type="text"]'
                            ) || row.querySelector('input');
                            if (inp) {
                                setInp(inp, imgUrl);
                                return {found: true, type: inp.type,
                                        placeholder: inp.placeholder || ''};
                            }
                        }
                        return {found: false};
                    }
                    """,
                    {"qty": qty, "imgUrl": img_url},
                )
                if js_direct.get("found"):
                    url_filled = True
                    print(f"[Wing] {qty}개 이미지 URL 직접 주입 "
                          f"(type={js_direct.get('type')}, "
                          f"placeholder={js_direct.get('placeholder')}): "
                          f"{img_url[:60]}...")
                else:
                    print(f"[Wing] {qty}개 이미지 URL 직접 주입 실패 — 셀렉터 폴백 시도")

            # method='button' 또는 direct 실패 → 모달/인라인 셀렉터로 탐색
            if not url_filled:
                url_selectors = [
                    "input[placeholder*='URL']",
                    "input[placeholder*='url']",
                    "input[placeholder*='이미지 주소']",
                    "input[placeholder*='이미지 URL']",
                    "input[type='url']",
                    "[class*='modal'] input[type='text']",
                    "[class*='dialog'] input[type='text']",
                    "[class*='popup'] input[type='text']",
                ]
                for u_sel in url_selectors:
                    if await pg.locator(u_sel).count() > 0:
                        try:
                            await pg.locator(u_sel).first.fill(img_url)
                            await asyncio.sleep(0.3)
                            url_filled = True
                            print(f"[Wing] {qty}개 이미지 URL 입력 ({u_sel}): {img_url[:60]}...")
                            break
                        except Exception:
                            continue

            if not url_filled:
                print(f"[Wing] {qty}개 이미지 URL 입력 필드 미발견")
                errors.append(f"{qty}개 이미지 URL 입력 필드 미발견")
                continue

            # 확인/등록 버튼 (모달이 열렸을 경우만 해당)
            for c_sel in [
                "button:has-text('확인')",
                "button:has-text('등록')",
                "button:has-text('적용')",
                "button:has-text('삽입')",
                "[class*='modal'] button:has-text('확인')",
            ]:
                if await pg.locator(c_sel).count() > 0:
                    await pg.locator(c_sel).first.click()
                    await asyncio.sleep(0.8)
                    print(f"[Wing] {qty}개 이미지 등록 확인")
                    break

        await self._shot("26_after_option_images")
        return errors

    # ── 상세설명 이미지 등록 ───────────────────────────────────────

    async def _set_detail_image(self, detail_image_url: str) -> list[str]:
        """
        Wing 상세설명 에디터에 이미지 URL 삽입.
        - 상세설명 영역 스크롤 → 이미지 삽입 버튼 → URL 입력 → 확인
        """
        errors: list[str] = []
        pg = self._pg

        if not detail_image_url:
            return errors

        # 상세설명 섹션 스크롤 (보통 페이지 하단)
        await pg.evaluate("window.scrollTo(0, 3000)")
        await asyncio.sleep(0.8)
        await self._shot("27_detail_section")

        # 이미지 삽입 버튼 탐색
        img_btn_clicked: bool = await pg.evaluate("""
            () => {
                /* 상세설명 섹션 찾기 */
                let detailSection = null;
                for (const el of document.querySelectorAll('*')) {
                    const t = el.textContent.trim();
                    if ((t === '상세설명' || t === '상품 상세설명') &&
                        (el.tagName === 'LABEL' || el.tagName === 'SPAN' ||
                         el.tagName === 'H3'    || el.tagName === 'H4')) {
                        detailSection = el.closest('section,[class*="section"],[class*="block"],form')
                                     || el.parentElement?.parentElement;
                        break;
                    }
                }

                const scope = detailSection || document;

                /* 이미지 삽입 버튼 탐색 (에디터 툴바) */
                for (const el of scope.querySelectorAll('button,[role="button"],[title]')) {
                    const txt   = el.textContent.trim();
                    const title = (el.getAttribute('title') || '').toLowerCase();
                    const cls   = (el.className || '').toLowerCase();
                    if (txt === '이미지' || txt.includes('이미지 삽입') ||
                        title.includes('image') || title.includes('이미지') ||
                        cls.includes('image-btn') || cls.includes('img-btn')) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
        """)

        if not img_btn_clicked:
            print("[Wing] 상세설명 이미지 삽입 버튼 미발견 - 건너뜀")
            errors.append("상세설명 이미지 버튼 미발견")
            return errors

        await asyncio.sleep(0.8)

        # URL 탭 클릭 (에디터가 파일/URL 탭을 분리하는 경우)
        for url_tab_sel in [
            "button:has-text('URL')",
            "[role='tab']:has-text('URL')",
            "label:has-text('URL')",
        ]:
            if await pg.locator(url_tab_sel).count() > 0:
                await pg.locator(url_tab_sel).first.click()
                await asyncio.sleep(0.4)
                print("[Wing] 이미지 URL 탭 클릭")
                break

        # URL 입력
        url_filled = False
        for u_sel in [
            "input[placeholder*='URL']",
            "input[placeholder*='url']",
            "input[placeholder*='이미지 주소']",
            "input[type='url']",
            "[class*='modal'] input[type='text']",
            "[class*='dialog'] input[type='text']",
        ]:
            if await pg.locator(u_sel).count() > 0:
                await pg.locator(u_sel).first.fill(detail_image_url)
                await asyncio.sleep(0.3)
                url_filled = True
                print(f"[Wing] 상세설명 이미지 URL 입력: {detail_image_url[:60]}...")
                break

        if not url_filled:
            errors.append("상세설명 이미지 URL 입력 필드 미발견")
            return errors

        # 확인/삽입 버튼
        for c_sel in [
            "button:has-text('확인')",
            "button:has-text('삽입')",
            "button:has-text('등록')",
            "button:has-text('적용')",
            "[class*='modal'] button:has-text('확인')",
        ]:
            if await pg.locator(c_sel).count() > 0:
                await pg.locator(c_sel).first.click()
                await asyncio.sleep(1.0)
                print("[Wing] 상세설명 이미지 삽입 완료")
                break

        await self._shot("28_after_detail_image")
        return errors

    # ── 메인 실행 ─────────────────────────────────────────────────

    async def edit_product(self, params: WingEditParams) -> WingEditResult:
        """
        Wing 상품 수정 메인 함수.

        수행 순서 (반자동화):
        1. 로그인
        2. 수정 페이지 이동
        3. ⏸️  사용자 대기 — Wing 브라우저에서 수량·개당 용량 직접 입력
           (자동화 불안정 구간: React Controlled Input 타이밍 문제)
           GUI [Wing 계속 ▶] 클릭 시 재개
        4. 옵션 테이블 렌더링 폴링 (최대 30초)
        5. 판매가·최저가 자동 입력
        6. 옵션별 이미지 자동 등록
        7. 상세설명 이미지 자동 등록
        8. 저장
        """
        all_errors: list[str] = []

        if not await self._ensure_login():
            return WingEditResult(success=False, message="Wing 로그인 실패")

        if not await self._goto_edit(params.seller_product_id):
            return WingEditResult(
                success=False,
                message=f"상품 {params.seller_product_id} 수정 페이지 이동 실패",
            )

        # ── Step 3: 사용자 직접 입력 대기 ────────────────────────────
        # 수량·개당 용량 차원 입력은 React Controlled Input 구조 및
        # Wing 페이지 로딩 타이밍 문제로 자동화가 불안정 →
        # 사용자가 Playwright 브라우저 창에서 직접 입력하고 GUI 버튼으로 신호 전달
        qty_str  = ", ".join(f"{b.qty}개" for b in sorted(params.bundles, key=lambda b: b.qty))
        vol_hint = (
            f"개당 용량: {params.volume:g}{params.volume_unit}"
            if params.volume > 0 else "개당 용량: 해당 없음"
        )
        await self._wait_for_user(
            f"Wing 브라우저에서 아래 두 가지를 직접 입력하세요:\n"
            f"  ① 수량  : {qty_str} 각각 추가\n"
            f"  ② {vol_hint}  추가\n"
            f"완료 후 GUI의 [Wing 계속 ▶] 버튼을 클릭하면 자동으로 재개됩니다."
        )
        await self._shot("21_after_user_input")

        # ── Step 4: 옵션 테이블 렌더링 폴링 (최대 30초) ──────────────
        # 차원 입력 직후 React 가 옵션 행 테이블을 렌더링하는 데 수초 소요
        print("[Wing] 옵션 테이블 렌더링 대기 중...")
        await asyncio.sleep(0.5)
        for _poll in range(60):           # 0.5s × 60 = 최대 30초
            _inp_cnt: int = await self._pg.evaluate(
                "() => document.querySelectorAll('.option-pane-table-body input').length"
            )
            if _inp_cnt > 0:
                print(f"[Wing] 옵션 테이블 준비 완료 (input {_inp_cnt}개, {_poll * 0.5:.1f}초 대기)")
                break
            await asyncio.sleep(0.5)
        else:
            print("[Wing] 옵션 테이블 타임아웃 (30초) — 가격 입력 시도 계속")
        await self._shot("22_option_table_ready")

        # ── Step 5: 판매가·최저가 자동 입력 ─────────────────────────
        await self._dismiss_dialogs()
        await asyncio.sleep(0.3)
        await self._pg.evaluate("window.scrollTo(0, 1100)")
        await asyncio.sleep(0.4)
        all_errors.extend(await self._set_all_row_prices(params.bundles))
        await self._shot("23_after_row_prices")

        # ── Step 6: 옵션별 이미지 자동 등록 ──────────────────────────
        all_errors.extend(await self._set_option_images(params.bundles))

        # ── Step 7: 상세설명 이미지 자동 등록 ────────────────────────
        if params.detail_image_url:
            all_errors.extend(await self._set_detail_image(params.detail_image_url))

        # ── Step 8: 저장 ──────────────────────────────────────────────
        # draft=True  → '임시 저장' 버튼 클릭 (Wing 임시저장 상태)
        # draft=False → '상품등록' 버튼 클릭 (즉시 판매 시작)
        saved = await self._save(draft=params.draft)
        if not saved:
            all_errors.append("저장 버튼 클릭 실패")

        if all_errors:
            return WingEditResult(
                success=saved,
                message="부분 완료 - " + " / ".join(all_errors),
                errors=all_errors,
            )
        return WingEditResult(success=True, message="Wing 수정 완료")


    # ── 임시저장 목록 일괄 판매요청 ──────────────────────────────────

    async def bulk_publish_all(
        self,
        skip_names: list[str] | None = None,
        log_cb=None,
        progress_cb=None,
        product_data: dict | None = None,
        gemini_api_key: str = "",
    ) -> dict:
        """
        product_data: {product_name: {"gtin": str, "bundles": {qty: {"weight": str}}}}
        """
        """
        Wing 임시저장 목록에서 판매요청 처리.
        전략: 행 구조 탐색 대신 '판매요청' 버튼을 직접 찾아 클릭 (DOM 구조 무관).
        """
        def log(msg: str):
            if log_cb: log_cb(msg)
            else: print(msg)

        # _ensure_login / _do_login 에서도 GUI 로그 출력되도록 연결
        self._log_cb = log

        # ── 중복 실행 방지 ───────────────────────────────────────────────
        if _BULK_GUARD["running"]:
            log("⚠️ 자동판매 이미 실행 중 — 중복 실행 방지됨. 이전 작업 완료 후 재시도하세요.")
            return {"published": 0, "skipped": 0, "errors": ["자동판매 이미 실행 중 — 이전 작업 완료 후 재시도"]}
        _BULK_GUARD["running"] = True
        try:
          return await self._bulk_publish_inner(
              skip_names=skip_names, log_cb=log_cb, progress_cb=progress_cb,
              product_data=product_data, gemini_api_key=gemini_api_key,
          )
        finally:
            _BULK_GUARD["running"] = False

    async def _bulk_publish_inner(
        self,
        skip_names: list[str] | None = None,
        log_cb=None,
        progress_cb=None,
        product_data: dict | None = None,
        gemini_api_key: str = "",
    ) -> dict:
        def log(msg: str):
            if log_cb: log_cb(msg)
            else: print(msg)

        skip_names = [n.strip() for n in (skip_names or []) if n.strip()]
        pg = self._pg
        BULK_BTN_KW = ["판매요청", "판매 요청", "승인요청", "판매신청", "판매 신청"]

        if not await self._ensure_login():
            return {"published": 0, "skipped": 0, "errors": ["Wing 로그인 실패"]}

        # ── 1단계: 상품 조회/수정 메뉴 클릭으로 목록 진입 ───────────
        log("[Wing 판매요청] 상품 목록 페이지 이동 중...")

        # 먼저 Wing 홈으로 이동 (SPA 초기화)
        try:
            await pg.goto(WING_URL, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
        except Exception:
            pass

        # 네비게이션 메뉴에서 "상품 조회/수정" 링크 클릭
        menu_clicked: bool = await pg.evaluate("""
            () => {
                const TARGETS = ['상품 조회/수정', '상품조회/수정', '상품조회수정'];
                for (const el of document.querySelectorAll('a, button, [role="menuitem"]')) {
                    if (!el.offsetParent) continue;
                    const t = el.textContent.trim();
                    if (TARGETS.some(kw => t.includes(kw))) {
                        el.click();
                        return true;
                    }
                }
                return false;
            }
        """)
        if menu_clicked:
            log("[Wing 판매요청] '상품 조회/수정' 메뉴 클릭")
        else:
            # 직접 URL로 이동
            log("[Wing 판매요청] 메뉴 미발견 — URL 직접 이동")
            try:
                await pg.goto(
                    f"{WING_URL}/tenants/seller-web/vendor-inventory/list",
                    wait_until="domcontentloaded", timeout=30_000,
                )
            except Exception as e:
                return {"published": 0, "skipped": 0, "errors": [f"목록 페이지 이동 실패: {e}"]}

        await asyncio.sleep(2)
        # ── xauth 리다이렉트 감지 → 재로그인 시도 ────────────────────────
        if self._is_auth_url(pg.url):
            log(f"[Wing 판매요청] ⚠️ xauth 리다이렉트 감지 — 세션 만료. 재로그인 시도...")
            if self._session_path.exists():
                self._session_path.unlink()
            ok = await self._do_login()
            if not ok:
                return {"published": 0, "skipped": 0,
                        "errors": ["Wing 세션 만료 — 재로그인 실패. 브라우저에서 직접 로그인 후 재시도"]}
            # 로그인 후 목록 재이동
            try:
                await pg.goto(
                    f"{WING_URL}/tenants/seller-web/vendor-inventory/list",
                    wait_until="domcontentloaded", timeout=30_000,
                )
                await asyncio.sleep(2)
            except Exception as e:
                return {"published": 0, "skipped": 0, "errors": [f"재로그인 후 목록 이동 실패: {e}"]}
            if self._is_auth_url(pg.url):
                return {"published": 0, "skipped": 0,
                        "errors": ["재로그인 후에도 xauth 차단 — Wing 브라우저에서 수동으로 로그인 완료 후 재시도하세요"]}

        # 상품 목록 페이지 로딩 완료 대기
        # Wing SPA: 통계 카드들이 숫자를 표시할 때까지 대기 (스피너 사라짐)
        log("[Wing 판매요청] 페이지 데이터 로딩 대기 중...")
        try:
            await pg.wait_for_function(
                """() => {
                    // '임시저장' 카드가 보이면 페이지 로딩 완료
                    const els = Array.from(document.querySelectorAll('*'));
                    return els.some(el =>
                        el.offsetParent !== null &&
                        el.children.length === 0 &&
                        el.textContent.trim() === '임시저장'
                    );
                }""",
                timeout=20_000,
            )
            log("[Wing 판매요청] 페이지 로딩 완료 (임시저장 카드 확인)")
        except Exception:
            await asyncio.sleep(6)
            log("[Wing 판매요청] 로딩 대기 시간 초과 — 강제 진행")

        log(f"[Wing 판매요청] 현재 URL: {pg.url}")
        await self._shot("bulk_01_list")

        # ── 2단계: 임시저장 카드 숫자 읽기 + 클릭 ──────────────────
        # Wing 상단 통계 카드에서 임시저장 개수를 먼저 읽어 0이면 즉시 종료.
        # 클릭은 더 넓은 탐색(8단계 부모, 클릭 핸들러 있는 요소 포함)으로 시도.
        _card_result: list = await pg.evaluate("""
            () => {
                // "임시저장" 텍스트를 포함하는 leaf 요소 탐색
                const leaves = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.offsetParent !== null &&
                    el.children.length === 0 &&
                    el.textContent.trim() === '임시저장'
                );
                if (!leaves.length) return [null, false];

                // 카드 숫자 읽기: leaf에서 위로 4단계 범위 innerText에서 숫자 추출
                let draftCount = null;
                for (const leaf of leaves) {
                    let p = leaf.parentElement;
                    for (let i = 0; i < 4; i++) {
                        if (!p) break;
                        const txt = p.innerText || '';
                        const m = txt.match(/(\\d+)/);
                        if (m) { draftCount = parseInt(m[1]); break; }
                        p = p.parentElement;
                    }
                    if (draftCount !== null) break;
                }

                // 클릭: 8단계 부모에서 a/button/onClick/card 계열 요소 탐색
                for (const leaf of leaves) {
                    let target = leaf;
                    for (let i = 0; i < 8; i++) {
                        if (!target.parentElement) break;
                        target = target.parentElement;
                        const tag = target.tagName.toLowerCase();
                        const cls = (target.className || '').toString();
                        const hasClick = typeof target.onclick === 'function'
                            || target.getAttribute('onclick');
                        if (tag === 'a' || tag === 'button' || hasClick ||
                            cls.includes('card') || cls.includes('item') ||
                            cls.includes('status') || cls.includes('badge') ||
                            cls.includes('filter') || cls.includes('tab')) {
                            target.click();
                            return [draftCount, true];
                        }
                    }
                    // 적절한 부모 못 찾으면 4번째 부모 클릭 (fallback)
                    let fb = leaf;
                    for (let i = 0; i < 4; i++) { if (fb.parentElement) fb = fb.parentElement; }
                    fb.click();
                    return [draftCount, true];
                }
                return [draftCount, false];
            }
        """)
        _draft_count = _card_result[0]   # int or null
        draft_card_clicked: bool = _card_result[1]

        log(f"[Wing 판매요청] 임시저장 카드 — 개수: {_draft_count}, 클릭: {draft_card_clicked}")

        # 카드 숫자가 0 → 임시저장 상품 없음, 즉시 종료
        if _draft_count == 0:
            log("[Wing 판매요청] 임시저장 상품 0개 — 건너뜀")
            return {"published": 0, "skipped": 0, "errors": []}

        if draft_card_clicked:
            log("[Wing 판매요청] 임시저장 카드 클릭 — URL 변경 대기 중...")
            _filter_url: str = ""
            for _fw in range(16):
                await asyncio.sleep(0.5)
                if "productStatus" in pg.url or "TEMP_SAVE" in pg.url or "DRAFT" in pg.url:
                    _filter_url = pg.url
                    log(f"[Wing 판매요청] 필터 URL 확인: {_filter_url}")
                    break
            else:
                log("[Wing 판매요청] 카드 클릭 후 URL 미변경 — URL 직접 이동으로 전환")

            # 확정된 URL로 재이동해서 DRAFT 필터 결과가 완전히 로딩되도록 보장
            _goto_url = _filter_url or f"{WING_URL}/vendor-inventory/list?productStatus=DRAFT"
            try:
                await pg.goto(_goto_url, wait_until="networkidle", timeout=25_000)
            except Exception:
                await pg.goto(_goto_url, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(2)
            log(f"[Wing 판매요청] DRAFT 필터 페이지 재로드 완료: {pg.url}")
        else:
            log("[Wing 판매요청] 임시저장 카드 클릭 실패 — URL 직접 필터 시도")
            for _status in ["DRAFT", "TEMP_SAVE"]:
                try:
                    _furl = f"{WING_URL}/vendor-inventory/list?productStatus={_status}"
                    await pg.goto(_furl, wait_until="networkidle", timeout=25_000)
                    await asyncio.sleep(2)
                    if not self._is_auth_url(pg.url):
                        log(f"[Wing 판매요청] 직접 필터 URL 이동: {_furl}")
                        break
                except Exception:
                    pass

        await self._shot("bulk_02_draft_tab")

        published = 0
        skipped   = 0
        errors: list[str] = []
        published_names: list[str] = []   # 성공한 상품명 목록 (이력 저장용)

        # ── 임시저장 상품 링크(vendorInventoryId) 수집 ───────────────
        # Wing 목록 페이지의 상품 행은 DOM에 지연 렌더링됨 → 스크롤로 강제 로딩
        log("[Wing 판매요청] 상품 목록 로딩 대기 중 (스크롤)...")
        for _ in range(6):
            await pg.evaluate("() => window.scrollBy(0, 600)")
            await asyncio.sleep(1)
        await pg.evaluate("() => window.scrollTo(0, 0)")
        await asyncio.sleep(1)

        # 목록 페이지에서 "임시저장" 상태 텍스트 존재 여부를 먼저 확인
        # → 0개면 50개 상품 하나씩 방문하는 낭비 방지
        _page_has_draft: bool = await pg.evaluate("""
            () => {
                const bodyText = document.body ? document.body.innerText : '';
                // "검색 결과가 없습니다" 또는 "상품이 없습니다" 메시지 확인
                const EMPTY_MSGS = ['검색 결과가 없습니다', '상품이 없습니다', '등록된 상품이 없습니다',
                                    'No results', 'No products'];
                if (EMPTY_MSGS.some(m => bodyText.includes(m))) return false;
                // 목록 내 임시저장 상태 배지 확인
                const leaves = Array.from(document.querySelectorAll('*')).filter(el =>
                    el.offsetParent !== null && el.children.length === 0
                );
                return leaves.some(el => el.textContent.trim() === '임시저장');
            }
        """)
        if not _page_has_draft:
            log("[Wing 판매요청] 목록 페이지에 임시저장 상품 없음 — 종료")
            await self._shot("bulk_99_done")
            return {"published": 0, "skipped": 0, "errors": []}

        inv_ids: list[str] = await pg.evaluate("""
            () => {
                // 메인 목록 테이블/컨테이너 안의 링크만 수집 (사이드바 등 제외)
                // Wing 목록 행: modify 링크만 포함 (view 링크 제외)
                const links = Array.from(document.querySelectorAll('a[href*="vendor-inventory/modify"]'));
                const ids = links
                    .map(a => { const m = a.href.match(/vendorInventoryId=(\\d+)/); return m?.[1]; })
                    .filter(Boolean);
                return [...new Set(ids)];
            }
        """)

        total = len(inv_ids)
        log(f"[Wing 판매요청] 발견된 임시저장 상품 ID: {inv_ids} (총 {total}개)")
        if progress_cb:
            try: progress_cb(0, total)
            except Exception: pass

        if not inv_ids:
            # 링크 방식 실패 시 현재 URL에서 productStatus=DRAFT 확인 후 로그
            body_preview: str = await pg.evaluate(
                "() => document.body.innerText.substring(0, 400)"
            )
            log(f"[Wing 판매요청] 상품 ID 미발견. 페이지 본문:\n{body_preview}")
            await self._shot("bulk_99_done")
            return {"published": 0, "skipped": 0,
                    "errors": ["임시저장 상품 링크를 찾지 못했습니다. "
                               "Wing 페이지에서 직접 확인해 주세요."]}

        # ── 각 상품 수정 페이지 → 판매요청 버튼 클릭 ────────────────
        PUBLISH_KW = ["상품등록", "판매요청", "저장 및 판매요청", "승인요청", "판매신청"]

        for idx, inv_id in enumerate(inv_ids, 1):
            # skip_names 체크 (이름은 나중에 페이지에서 확인)
            if progress_cb:
                try: progress_cb(idx, total)
                except Exception: pass
            edit_url = f"{WING_URL}/vendor-inventory/modify?vendorInventoryId={inv_id}"
            log(f"[Wing 판매요청] [{idx}/{total}] 상품 {inv_id} 수정 페이지 이동 중...")

            try:
                await pg.goto(edit_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                log(f"[Wing 판매요청] ⚠️ 페이지 이동 실패 ({inv_id}): {e}")
                errors.append(f"페이지 이동 실패: {inv_id}")
                continue

            # 페이지 로딩 대기
            try:
                await pg.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass
            await asyncio.sleep(3)

            # ── 상품 상태 확인: 임시저장이 아니면 건너뜀 ──────────────
            _prod_status: str = await pg.evaluate("""
                () => {
                    // Wing 수정 페이지의 상태 배지/텍스트 탐색
                    for (const el of document.querySelectorAll('*')) {
                        if (!el.offsetParent || el.children.length > 0) continue;
                        const t = el.textContent.trim();
                        if (['심사중', '판매중', '판매종료', '판매중지', '반려'].includes(t))
                            return t;
                    }
                    return '';
                }
            """)
            if _prod_status:
                log(f"[Wing 판매요청] ⏭ 임시저장 아님 (상태: {_prod_status}) — 건너뜀 ({inv_id})")
                skipped += 1
                continue

            # ── 로켓그로스 판매방식 선택 팝업 사전 처리 ─────────────────
            # Wing이 "판매 방식에 로켓그로스가 추가되었습니다" 팝업을 추가함.
            # 이 팝업에서 판매자배송을 선택하지 않으면 상품등록이 실제로 반영 안 됨.
            _rg_result = await pg.evaluate("""
                () => {
                    const body = document.body.innerText || '';
                    if (!body.includes('로켓그로스')) return 'no_popup';
                    // 판매자배송 버튼/라디오/레이블 클릭
                    let sel = false;
                    for (const el of document.querySelectorAll('button, label, input[type="radio"]')) {
                        if (!el.offsetParent) continue;
                        const t = (el.textContent || el.getAttribute('value') || '').trim();
                        if (t === '판매자배송') { el.click(); sel = true; break; }
                    }
                    // 팝업 내 확인 버튼 클릭
                    for (const btn of document.querySelectorAll('button')) {
                        if (!btn.offsetParent) continue;
                        const t = btn.textContent.trim();
                        if (t === '확인' || t === '닫기') {
                            let p = btn.parentElement;
                            for (let d = 0; d < 10 && p && p !== document.body; d++) {
                                if (p.textContent.includes('로켓그로스')) {
                                    btn.click();
                                    return sel ? '판매자배송 선택 후 확인' : '확인만 클릭';
                                }
                                p = p.parentElement;
                            }
                        }
                    }
                    return sel ? '판매자배송만 선택' : 'popup_found_no_btn';
                }
            """)
            if _rg_result != 'no_popup':
                log(f"[Wing 판매요청] ℹ️ 로켓그로스 팝업 처리: {_rg_result} ({inv_id})")
                await asyncio.sleep(1)

            # ── 상품등록 버튼 완전 활성화 대기 (최대 8초 폴링) ──────────
            # Wing SPA 렌더링이 느릴 때 버튼이 disabled 상태로 클릭되어
            # "필수입력사항 입력하세요" 오류가 뜨는 타이밍 문제 방지
            for _wi_btn in range(16):
                _btn_ready: bool = await pg.evaluate("""
                    () => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        const b = btns.find(b => b.textContent.trim() === '상품등록');
                        return b ? (!b.disabled && b.offsetParent !== null) : false;
                    }
                """)
                if _btn_ready:
                    break
                await asyncio.sleep(0.5)
            else:
                log(f"[Wing 판매요청] ⚠️ 상품등록 버튼 활성화 대기 초과 ({inv_id}) — 강제 진행")

            # ── 루프 내 세션 만료 감지 → 재로그인 후 해당 상품 재이동 ──
            if self._is_auth_url(pg.url):
                log(f"[Wing 판매요청] ⚠️ 세션 만료 감지 (상품 {inv_id}) — 재로그인 시도...")
                ok = await self._do_login()
                if not ok:
                    errors.append(f"세션 만료 재로그인 실패: {inv_id}")
                    continue
                try:
                    await pg.goto(edit_url, wait_until="domcontentloaded", timeout=30_000)
                    await pg.wait_for_load_state("networkidle", timeout=10_000)
                    await asyncio.sleep(3)
                except Exception as e:
                    log(f"[Wing 판매요청] ⚠️ 재로그인 후 페이지 이동 실패 ({inv_id}): {e}")
                    errors.append(f"재로그인 후 이동 실패: {inv_id}")
                    continue

            await self._shot(f"bulk_edit_{inv_id}")

            # 상품명 확인 (input 필드 우선 → 실제 상품명 텍스트 추출)
            prod_name: str = await pg.evaluate("""
                () => {
                    // 1순위: 상품명 input 필드 value
                    const inp = document.querySelector(
                        'input[name*="name"], input[placeholder*="상품명"], '
                        + 'input[id*="productName"], textarea[name*="name"]'
                    );
                    if (inp && inp.value && inp.value.trim().length > 2) {
                        return inp.value.trim().substring(0, 60);
                    }
                    // 2순위: 상품명 전용 클래스
                    const cls = document.querySelector(
                        '[class*="product-name"] input, [class*="productName"] input, '
                        + '[class*="product_name"] input'
                    );
                    if (cls && cls.value && cls.value.trim().length > 2) {
                        return cls.value.trim().substring(0, 60);
                    }
                    return '';
                }
            """)
            log(f"[Wing 판매요청] 상품명: {prod_name or '(확인 불가)'}")

            # skip_names 체크
            should_skip = any(
                sn and (sn in prod_name or prod_name in sn)
                for sn in skip_names
            )
            if should_skip:
                log(f"[Wing 판매요청] 건너뜀: {prod_name[:40]}")
                skipped += 1
                continue

            # ── 필수 속성 자동 입력 (UPC/바코드, 번들사이즈, 개당 용량) ──────
            # Wing 상품 수정 페이지 전체 input을 대상으로 UPC/바코드 필드 탐색.
            # 기존 'table tbody tr' 구조 전제를 제거 — 필터 패널·기본정보 섹션 등
            # tr 밖에 있는 input도 탐색 대상에 포함.
            try:
                # ── 상품명 매칭: [:20] 부분 일치 대신 정규화 후 최장 공통 부분열 기반 ──
                _pdata = {}
                if product_data and prod_name:
                    def _norm_name(s: str) -> str:
                        import re as _re2
                        return _re2.sub(r'\s+', '', s).lower()

                    _prod_norm = _norm_name(prod_name)
                    _best_score = 0
                    for _pname, _pinfo in product_data.items():
                        if not _pname:
                            continue
                        _pname_norm = _norm_name(_pname)
                        # 완전 일치 → 즉시 확정
                        if _pname_norm == _prod_norm:
                            _pdata = _pinfo
                            _best_score = 1.0
                            break
                        # 긴 쪽이 짧은 쪽을 포함하면 높은 점수
                        _longer  = _prod_norm if len(_prod_norm) >= len(_pname_norm) else _pname_norm
                        _shorter = _pname_norm if len(_prod_norm) >= len(_pname_norm) else _prod_norm
                        if _shorter and _shorter in _longer:
                            _score = len(_shorter) / len(_longer)
                        else:
                            # 공통 앞부분 길이 / 짧은 쪽 길이
                            _common = 0
                            for _ca, _cb in zip(_prod_norm, _pname_norm):
                                if _ca == _cb:
                                    _common += 1
                                else:
                                    break
                            _score = _common / max(len(_shorter), 1)
                        if _score > _best_score:
                            _best_score = _score
                            _pdata = _pinfo

                _gtin = _pdata.get("gtin", "") if _pdata else ""
                _model_number = ""  # 품번/모델번호 (Gemini 검색 결과)

                # ── GTIN 없을 때 Gemini 폴백 (Google Search 그라운딩) ──────────
                if not _gtin and gemini_api_key and prod_name:
                    try:
                        # Wing 페이지에서 브랜드/제조사 추출 → 검색 정확도 향상
                        _brand_on_page: str = await pg.evaluate("""
                            () => {
                                const selectors = [
                                    'input[name*="brand"]', 'input[placeholder*="브랜드"]',
                                    'input[placeholder*="제조사"]', 'input[name*="manufacturer"]',
                                ];
                                for (const sel of selectors) {
                                    const el = document.querySelector(sel);
                                    if (el && el.value && el.value.trim().length > 1)
                                        return el.value.trim();
                                }
                                for (const kw of ['브랜드', '제조사', '브랜드명']) {
                                    for (const el of document.querySelectorAll('td, th, label, span')) {
                                        if (el.textContent.trim() === kw) {
                                            const sib = el.nextElementSibling;
                                            const inp = sib ? sib.querySelector('input') || sib : null;
                                            const val = inp ? (inp.value || inp.textContent || '').trim() : '';
                                            if (val && val.length > 1 && val.length < 60) return val;
                                        }
                                    }
                                }
                                return '';
                            }
                        """) or ""
                        _search_ctx = f"{_brand_on_page} {prod_name}".strip() if _brand_on_page else prod_name

                        from google import genai as _genai
                        from google.genai import types as _gtypes
                        _gclient = _genai.Client(api_key=gemini_api_key)
                        _g_resp = _gclient.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=(
                                f"상품명: {_search_ctx}\n"
                                "Google 검색을 통해 이 상품의 UPC, EAN, 또는 GTIN 바코드 번호를 찾아주세요. "
                                "바코드 숫자만 반환하세요 (8~14자리). 찾을 수 없으면 빈 문자열만 반환하세요."
                            ),
                            config=_gtypes.GenerateContentConfig(
                                tools=[_gtypes.Tool(google_search=_gtypes.GoogleSearch())]
                            ),
                        )
                        _g_text = (_g_resp.text or "").strip()
                        import re as _re_bc
                        _bc_match = _re_bc.search(r'(?<!\d)(\d{8,14})(?!\d)', _g_text)
                        if _bc_match:
                            _gtin = _bc_match.group(1)
                            log(f"[Wing 판매요청] 🤖 Gemini 바코드 폴백: {_gtin}")
                        else:
                            log(f"[Wing 판매요청] ℹ️ Gemini 바코드 폴백 결과 없음")
                    except Exception as _ge:
                        log(f"[Wing 판매요청] ⚠️ Gemini 바코드 폴백 실패: {_ge}")

                # ── 품번/모델번호 필요 여부 탐지 → Gemini 검색 ────────────────
                if gemini_api_key and prod_name:
                    try:
                        _needs_model: bool = await pg.evaluate("""
                            () => {
                                const keywords = [
                                    '품번', '모델번호', '제품번호', '모델명',
                                    'model', 'part number', 'manufacturer part',
                                    'parent manufacturer', 'global trade',
                                ];
                                const allInputs = Array.from(document.querySelectorAll(
                                    'input[type="text"], input:not([type])'
                                )).filter(inp => inp.offsetParent !== null && !inp.value);
                                for (const inp of allInputs) {
                                    let ctx = (
                                        (inp.placeholder || '') + ' ' +
                                        (inp.getAttribute('aria-label') || '') + ' ' +
                                        (inp.id || '') + ' ' + (inp.name || '')
                                    ).toLowerCase();
                                    let el = inp.parentElement;
                                    for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                                        if (el.children.length <= 8)
                                            ctx += ' ' + (el.textContent || '').toLowerCase();
                                    }
                                    if (keywords.some(k => ctx.includes(k))) return true;
                                }
                                return false;
                            }
                        """) or False

                        if _needs_model:
                            from google import genai as _genai_m
                            from google.genai import types as _gtypes_m
                            _gclient_m = _genai_m.Client(api_key=gemini_api_key)
                            _gm_resp = _gclient_m.models.generate_content(
                                model="gemini-2.5-flash",
                                contents=(
                                    f"상품명: {prod_name}\n"
                                    "Google 검색을 통해 이 상품의 모델번호 또는 제품번호(품번)를 찾아주세요. "
                                    "모델번호만 간결하게 반환하세요. 찾을 수 없으면 빈 문자열만 반환하세요."
                                ),
                                config=_gtypes_m.GenerateContentConfig(
                                    tools=[_gtypes_m.Tool(google_search=_gtypes_m.GoogleSearch())]
                                ),
                            )
                            _model_number = (_gm_resp.text or "").strip()
                            if _model_number:
                                log(f"[Wing 판매요청] 🤖 Gemini 품번 폴백: {_model_number[:60]}")
                    except Exception as _me:
                        log(f"[Wing 판매요청] ⚠️ 품번 Gemini 폴백 실패: {_me}")

                # ── UPC 입력 전 페이지 렌더링 대기 ──────────────────────────
                # Wing SPA는 기본 정보 섹션이 스크롤 후 마운트되는 경우가 있음
                if _gtin:
                    await pg.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.5)
                    # UPC/바코드 input이 나타날 때까지 최대 5초 폴링
                    for _wi in range(10):
                        _upc_visible: bool = await pg.evaluate("""
                            () => {
                                try {
                                    return Array.from(
                                        document.querySelectorAll('input[type="text"], input:not([type]), input[type="number"]')
                                    ).some(inp => {
                                        if (!inp.offsetParent) return false;
                                        const ctx = (
                                            (inp.placeholder || '') + ' ' +
                                            (inp.closest('label,div,td,li,section')?.textContent || '')
                                        ).toLowerCase();
                                        return ctx.includes('upc') || ctx.includes('바코드') ||
                                               ctx.includes('barcode') || ctx.includes('gtin');
                                    });
                                } catch(e) { return false; }
                            }
                        """)
                        if _upc_visible:
                            break
                        await asyncio.sleep(0.5)
                    else:
                        log(f"[Wing 판매요청] ℹ️ UPC 입력 필드 미발견 (5초 대기) — GTIN: {_gtin}")

                _filled = await pg.evaluate("""
                    (args) => {
                        try {
                            const {gtin, modelNumber} = args;
                            let filled = 0;

                            /* ── React native setter (controlled input 우회) ── */
                            const nativeSetter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value'
                            )?.set;
                            function setAndDispatch(inp, val) {
                                try {
                                    if (nativeSetter) nativeSetter.call(inp, String(val));
                                    else inp.value = String(val);
                                    inp.dispatchEvent(new Event('input',  {bubbles: true}));
                                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                                    inp.dispatchEvent(new Event('blur',   {bubbles: true}));
                                } catch(e) {}
                            }

                            /* ── 라벨 컨텍스트 수집 헬퍼 ──
                               input 기준으로 주변 텍스트(label, 부모 div, aria-label 등)를
                               모아서 필드 종류를 판단한다.
                               tr 구조 밖(필터 패널, 기본정보 섹션)도 모두 포함. */
                            function getContext(inp) {
                                try {
                                    const parts = [];
                                    parts.push(inp.placeholder || '');
                                    parts.push(inp.getAttribute('aria-label') || '');
                                    parts.push(inp.name || '');
                                    parts.push(inp.id   || '');
                                    /* 가장 가까운 label 탐색 */
                                    const lid = inp.id;
                                    if (lid) {
                                        const lbl = document.querySelector('label[for="' + lid + '"]');
                                        if (lbl) parts.push(lbl.textContent);
                                    }
                                    /* 부모를 최대 6단계 올라가며 텍스트 수집 */
                                    let el = inp.parentElement;
                                    for (let i = 0; i < 6 && el; i++, el = el.parentElement) {
                                        /* 너무 큰 컨테이너는 오탐 위험 → 자식 수 ≤ 8 인 노드만 */
                                        if (el.children.length <= 8) {
                                            parts.push(el.textContent || '');
                                        }
                                        /* label 태그이면 즉시 추가 */
                                        if (el.tagName === 'LABEL') {
                                            parts.push(el.textContent || '');
                                            break;
                                        }
                                    }
                                    return parts.join(' ').toLowerCase();
                                } catch(e) { return ''; }
                            }

                            /* ── 페이지 전체 input 탐색 (tr 제약 없음) ── */
                            const allInputs = Array.from(
                                document.querySelectorAll(
                                    'input[type="text"], input:not([type]), input[type="number"]'
                                )
                            ).filter(inp => {
                                if (!inp.offsetParent) return false;
                                /* ⚠️ 옵션 조합 테이블 입력 제외 — 가격·재고 필드에
                                   엉뚱한 번들수·용량값이 들어가 "옵션 항목을 확인해주세요"
                                   오류 발생 방지 */
                                if (inp.closest(
                                    '.option-pane-table-body, .option-pane-table,' +
                                    '[class*="option-pane-table"]'
                                )) return false;
                                return true;
                            });  /* 화면에 보이는 것만, 옵션 조합 테이블 제외 */

                            for (const inp of allInputs) {
                                const ctx = getContext(inp);

                                /* ① UPC / 바코드 / GTIN */
                                if (gtin && !inp.value &&
                                    (ctx.includes('upc') || ctx.includes('바코드') ||
                                     ctx.includes('barcode') || ctx.includes('gtin'))) {
                                    setAndDispatch(inp, gtin);
                                    filled++;
                                    continue;
                                }

                                /* ② 품번 / 모델번호 / Parent Manufacturer Part Number */
                                if (modelNumber && !inp.value &&
                                    (ctx.includes('품번') || ctx.includes('모델번호') ||
                                     ctx.includes('제품번호') || ctx.includes('모델명') ||
                                     ctx.includes('part number') || ctx.includes('manufacturer part') ||
                                     ctx.includes('parent manufacturer') || ctx.includes('global trade'))) {
                                    setAndDispatch(inp, modelNumber);
                                    filled++;
                                    continue;
                                }
                            }

                            /* ── 속성 테이블 행(tr) 내 번들사이즈·용량 입력 ──
                               기존 로직 유지: tr 구조에서 옵션명 텍스트를 읽어 번들 수·용량 추출
                               ⚠️ 옵션 조합 테이블(option-pane) 행은 반드시 건너뜀 */
                            const rows = document.querySelectorAll(
                                'table tbody tr, [class*="attribute"] tr, [class*="Attribute"] tr'
                            );
                            for (const row of rows) {
                                try {
                                    /* 옵션 조합 테이블 행 제외 — 가격 입력 필드 오염 방지 */
                                    if (row.closest('[class*="option-pane"]')) continue;
                                    const optCell = row.querySelector('td:first-child, th:first-child');
                                    const optText = optCell ? optCell.textContent.trim() : '';
                                    const bundleM = optText.match(/(\\d+)\\s*개/);
                                    const bundleNum = bundleM ? bundleM[1] : '';
                                    const weightM = optText.match(/(\\d+(?:\\.\\d+)?)\\s*(g|kg|ml|mL|L|mg)/i);
                                    const weightVal = weightM ? weightM[0] : '';

                                    const inputs = row.querySelectorAll(
                                        'input[type="text"], input:not([type]), input[type="number"]'
                                    );
                                    for (const inp of inputs) {
                                        const ctx = getContext(inp);
                                        if (bundleNum && !inp.value &&
                                            (ctx.includes('번들') || ctx.includes('bundle'))) {
                                            setAndDispatch(inp, bundleNum);
                                            filled++;
                                        } else if (bundleNum && !inp.value &&
                                            (ctx.includes('총 수량') || ctx.includes('총수량') ||
                                             ctx.includes('total'))) {
                                            setAndDispatch(inp, bundleNum);
                                            filled++;
                                        } else if (weightVal && !inp.value &&
                                            (ctx.includes('용량') || ctx.includes('중량') ||
                                             ctx.includes('weight') || ctx.includes('volume'))) {
                                            setAndDispatch(inp, weightVal);
                                            filled++;
                                        }
                                    }
                                } catch(e) {}
                            }

                            return {filled, inputCount: allInputs.length};
                        } catch(e) {
                            return {filled: 0, inputCount: 0, error: String(e)};
                        }
                    }
                """, {"gtin": _gtin, "modelNumber": _model_number})

                _n_filled = _filled.get("filled", 0) if isinstance(_filled, dict) else 0
                _n_inputs = _filled.get("inputCount", 0) if isinstance(_filled, dict) else 0
                _js_err   = _filled.get("error", "") if isinstance(_filled, dict) else ""

                if _js_err:
                    log(f"[Wing 판매요청] ⚠️ 속성 입력 JS 오류: {_js_err}")
                elif _n_filled:
                    log(f"[Wing 판매요청] ✅ 필수 속성 {_n_filled}개 자동 입력 완료 (탐색 input: {_n_inputs}개)")
                    await asyncio.sleep(1)
                else:
                    log(f"[Wing 판매요청] ℹ️ 속성 입력 없음 (탐색 input: {_n_inputs}개, GTIN: '{_gtin or '없음'}')")

            except Exception as _ae:
                log(f"[Wing 판매요청] ⚠️ 속성 자동 입력 실패: {_ae}")

            # 네트워크 요청 모니터링 — 모든 POST/PUT/PATCH 캡처
            api_calls: list[dict] = []
            def _on_request(req):
                if req.method in ("POST", "PUT", "PATCH"):
                    api_calls.append({"method": req.method, "url": req.url[:150]})
            def _on_response(resp):
                if resp.request.method in ("POST", "PUT", "PATCH"):
                    api_calls.append({"status": resp.status, "url": resp.url[:150]})
            pg.on("request", _on_request)
            pg.on("response", _on_response)

            # 고정(fixed/sticky) 하단 바의 '상품등록' 버튼 클릭
            clicked_btn = ""
            before_url = pg.url
            all_btn_texts: list[str] = []  # NameError 방지 — 항상 초기화

            try:
                all_btn_texts = await pg.evaluate("""
                    () => Array.from(document.querySelectorAll('button'))
                              .map(b => b.textContent.trim()).filter(Boolean)
                """)
            except Exception:
                pass

            # ── 미선택 드롭다운 속성 감지 → 등록 불가 상품 조기 건너뜀 ─────
            # Wing 커스텀 드롭다운이 "속성값을 선택하세요" 버튼으로 표시됨.
            # 이 상태에서 상품등록 클릭 시 에러 팝업이 먼저 뜨고 모달 클릭이
            # 30초 타임아웃으로 실패하므로 미리 감지해서 건너뜀.
            if '속성값을 선택하세요' in all_btn_texts:
                _dd_cnt = all_btn_texts.count('속성값을 선택하세요')
                log(f"[Wing 판매요청] ⚠️ 미선택 드롭다운 속성 {_dd_cnt}개 감지 ({inv_id})"
                    f" — 필수 속성을 Wing에서 직접 선택 후 재시도 필요, 건너뜀")
                errors.append(f"드롭다운 미선택({prod_name[:25]}): {_dd_cnt}개 — 수동확인필요")
                await self._shot(f"bulk_dropdown_miss_{inv_id}")
                continue

            # ── 옵션 조합 테이블 가격 로딩 대기 (최대 10초) ──────────────────
            # Wing SPA는 임시저장 가격을 비동기 API로 불러옴.
            # 가격이 로딩되기 전에 상품등록 클릭 시 "옵션 항목을 확인해주세요" 발생.
            for _ow in range(20):   # 0.5s × 20 = 10초
                _prices_ok: bool = await pg.evaluate("""
                    () => {
                        const tbody = document.querySelector('.option-pane-table-body');
                        if (!tbody) return true;           // 옵션 없는 상품은 통과
                        if (!tbody.querySelector('tr')) return true;  // 행 없으면 통과
                        // 가격 input에 값이 있으면 로딩 완료
                        return Array.from(tbody.querySelectorAll('input'))
                            .some(i => i.offsetParent !== null && i.value.trim() !== '');
                    }
                """)
                if _prices_ok:
                    break
                await asyncio.sleep(0.5)
            else:
                log(f"[Wing 판매요청] ⚠️ 옵션 가격 로딩 대기 초과 ({inv_id}) — 강제 진행")

            _FIND_SUBMIT_JS = """
                () => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    // 1순위: fixed/sticky 조상을 가진 상품등록 버튼
                    for (const btn of btns) {
                        if (btn.textContent.trim() !== '상품등록') continue;
                        let el = btn;
                        while (el) {
                            const pos = getComputedStyle(el).position;
                            if (pos === 'fixed' || pos === 'sticky') return btn;
                            el = el.parentElement;
                        }
                    }
                    // 2순위: 임시 저장 버튼 근방
                    const draftIdx = btns.findIndex(b => b.textContent.trim() === '임시 저장');
                    if (draftIdx >= 0) {
                        for (let i = draftIdx + 1; i < Math.min(draftIdx + 5, btns.length); i++) {
                            if (btns[i].textContent.trim() === '상품등록') return btns[i];
                        }
                    }
                    // 3순위: visible 상태인 아무 상품등록 버튼
                    const anyBtn = btns.find(
                        b => b.textContent.trim() === '상품등록'
                          && !b.disabled
                          && b.offsetParent !== null
                    );
                    return anyBtn || null;
                }
            """
            submit_handle = await pg.evaluate_handle(_FIND_SUBMIT_JS)
            el = submit_handle.as_element()

            # 버튼 미발견 시 최대 2회 재시도 (페이지 느린 로딩 대응)
            if not el:
                for _btn_retry in range(2):
                    await asyncio.sleep(3)
                    log(f"[Wing 판매요청] ⚠️ 상품등록 버튼 미발견 — {_btn_retry+1}회 재탐색 ({inv_id})")
                    submit_handle = await pg.evaluate_handle(_FIND_SUBMIT_JS)
                    el = submit_handle.as_element()
                    if el:
                        break

            if el:
                try:
                    await el.click()
                    clicked_btn = "상품등록"
                    log("[Wing 판매요청] 하단 고정 '상품등록' 버튼 클릭")
                except Exception as e:
                    log(f"[Wing 판매요청] 클릭 오류: {e}")

            if pub_result := {"clicked": bool(clicked_btn), "btnText": clicked_btn}:
                pass  # 아래에서 처리

            if clicked_btn:
                log(f"[Wing 판매요청] ✅ '{clicked_btn}' 버튼 클릭")
                await self._shot(f"bulk_after_click_{inv_id}")

                # ══════════════════════════════════════════════════════
                # 팝업 처리 핵심 원칙:
                #  ① CSS 클래스 기반 탐색 금지 → Wing 클래스명 불일치 위험
                #  ② 텍스트 기반 탐색: "확인해주세요" 등 에러 키워드로 판별
                #  ③ JS .click() 직접 호출: 모달 backdrop 도 우회
                #
                # 흐름:
                #  1) 상품등록 클릭 후 0.3s 간격 폴링 (최대 3초)
                #     → 에러 팝업 먼저 감지 → 즉시 확인 클릭
                #     → 확인 팝업 감지     → 즉시 클릭
                #  2) 에러 팝업이 먼저 떴으면 → 3초 후 footer 재클릭 → 확인 팝업 처리
                #  3) 3초 대기 후 최종 에러 확인 → 성공/실패 판정
                # ══════════════════════════════════════════════════════

                # ─ JS: 에러 팝업 감지 & 확인 버튼 즉시 클릭 ─────────────
                # DOM 트리를 역방향으로 탐색해 에러 문맥 안의 확인 버튼을 클릭.
                # 클래스명 불문, 텍스트로만 판별 → Wing 클래스 변경에 무관
                _JS_DISMISS_ERR = """
                    () => {
                        const ERR_KW = [
                            '확인해주세요', '입력항목을 모두', '옵션 항목을 확인',
                            '필수 입력', '등록불가', '잘못된 값'
                        ];
                        for (const btn of document.querySelectorAll('button')) {
                            if (!btn.offsetParent) continue;
                            const t = btn.textContent.trim();
                            if (t !== '확인' && t !== '닫기') continue;
                            /* 부모 체인(최대 8단계)에서 에러 텍스트 포함 컨테이너 탐색 */
                            let p = btn.parentElement;
                            for (let d = 0; d < 8 && p && p !== document.body; d++) {
                                if (ERR_KW.some(k => p.textContent.includes(k))) {
                                    /* 로켓그로스 팝업이면 판매자배송 먼저 선택 */
                                    if (p.textContent.includes('로켓그로스')) {
                                        for (const el of p.querySelectorAll('button, label, input[type="radio"]')) {
                                            if (!el.offsetParent) continue;
                                            const et = (el.textContent || el.getAttribute('value') || '').trim();
                                            if (et === '판매자배송') { el.click(); break; }
                                        }
                                    }
                                    const msg = p.textContent.replace(/\s+/g,' ').trim().substring(0, 120);
                                    btn.click();
                                    return msg;
                                }
                                p = p.parentElement;
                            }
                        }
                        return '';
                    }
                """

                # ─ JS: 확인(등록) 팝업 클릭 — 에러 팝업 제외 ──────────────
                # footer의 고정 "상품등록" 버튼과 에러 팝업을 모두 제외하고
                # 판매요청 확인 팝업의 버튼만 클릭
                _JS_CLICK_CONFIRM = """
                    () => {
                        const ERR_KW = [
                            '확인해주세요', '입력항목을 모두', '옵션 항목을 확인',
                            '필수 입력', '등록불가'
                        ];
                        for (const btn of document.querySelectorAll('button')) {
                            if (!btn.offsetParent) continue;
                            const t = btn.textContent.trim();
                            if (t !== '상품등록' && t !== '확인') continue;
                            /* footer 고정 버튼 제외 (position: fixed/sticky) */
                            if (t === '상품등록') {
                                let el = btn; let isFixed = false;
                                while (el && el !== document.body) {
                                    const pos = getComputedStyle(el).position;
                                    if (pos === 'fixed' || pos === 'sticky') { isFixed = true; break; }
                                    el = el.parentElement;
                                }
                                if (isFixed) continue;
                            }
                            /* 에러 팝업 컨텍스트 제외 */
                            let p = btn.parentElement; let isErr = false;
                            for (let d = 0; d < 8 && p && p !== document.body; d++) {
                                if (ERR_KW.some(k => p.textContent.includes(k))) { isErr = true; break; }
                                p = p.parentElement;
                            }
                            if (isErr) continue;
                            btn.click();
                            return true;
                        }
                        return false;
                    }
                """

                # ─ JS: footer 상품등록 버튼 클릭 ───────────────────────────
                _JS_CLICK_FOOTER = """
                    () => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        for (const btn of btns) {
                            if (btn.textContent.trim() !== '상품등록') continue;
                            let el = btn;
                            while (el && el !== document.body) {
                                const pos = getComputedStyle(el).position;
                                if (pos === 'fixed' || pos === 'sticky') { btn.click(); return true; }
                                el = el.parentElement;
                            }
                        }
                        /* fallback: '임시 저장' 바로 뒤 '상품등록' */
                        const di = btns.findIndex(b => b.textContent.trim() === '임시 저장');
                        if (di >= 0) {
                            for (let i = di+1; i < Math.min(di+5, btns.length); i++) {
                                if (btns[i].textContent.trim() === '상품등록') { btns[i].click(); return true; }
                            }
                        }
                        return false;
                    }
                """

                # ── Step 1: 에러 팝업 / 확인 팝업 빠른 감지 (0.3s 폴링, 최대 3s) ──
                _err_first = False   # 에러 팝업이 먼저 떴으면 True
                _skip_item = False   # 이 상품 건너뜀 플래그

                for _pw in range(10):   # 0.3s × 10 = 3초
                    await asyncio.sleep(0.3)
                    # ① 에러 팝업 우선 확인
                    _dismissed = await pg.evaluate(_JS_DISMISS_ERR)
                    if _dismissed:
                        _err_first = True
                        log(f"[Wing 판매요청] ⚠️ 에러 팝업 즉시 감지 → 확인 클릭 ({inv_id}) 내용: {_dismissed}")
                        break
                    # ② 정상 확인 팝업 확인
                    _confirmed = await pg.evaluate(_JS_CLICK_CONFIRM)
                    if _confirmed:
                        log("[Wing 판매요청] 확인 팝업 클릭 ✅")
                        break
                else:
                    log("[Wing 판매요청] ⚠️ 확인/에러 팝업 미감지 — 팝업 없이 진행")

                # ── Step 2: 에러 팝업이 먼저 떴으면 → 3초 대기 → footer 재클릭 ──
                if _err_first:
                    log("[Wing 판매요청] 🔄 3초 대기 후 상품등록 재시도...")
                    await asyncio.sleep(3)

                    _retry_clicked = await pg.evaluate(_JS_CLICK_FOOTER)
                    if not _retry_clicked:
                        log(f"[Wing 판매요청] ❌ 재시도 footer 버튼 미발견 ({inv_id})")
                        errors.append(f"등록실패(버튼미발견)({prod_name[:25]}): 에러팝업후재시도불가")
                        await self._shot(f"bulk_fail_{inv_id}")
                        _skip_item = True
                    else:
                        log("[Wing 판매요청] 🔄 재시도 — 상품등록 버튼 클릭")
                        # 재시도 후 팝업 폴링 (0.3s × 10 = 3초)
                        for _rw in range(10):
                            await asyncio.sleep(0.3)
                            # 에러 팝업 다시 뜨면 즉시 닫고 실패 처리
                            _err_again = await pg.evaluate(_JS_DISMISS_ERR)
                            if _err_again:
                                log(f"[Wing 판매요청] ❌ 재시도 후에도 에러 팝업 ({inv_id}) 내용: {_err_again}")
                                errors.append(f"등록실패(에러지속)({prod_name[:25]}): 옵션항목오류지속")
                                await self._shot(f"bulk_fail_{inv_id}")
                                _skip_item = True
                                break
                            _conf2 = await pg.evaluate(_JS_CLICK_CONFIRM)
                            if _conf2:
                                log("[Wing 판매요청] 🔄 재시도 확인 팝업 클릭 ✅")
                                break
                        else:
                            log("[Wing 판매요청] ⚠️ 재시도 확인 팝업 미감지 — 팝업 없이 진행")

                # ── 네트워크 로그 정리 ───────────────────────────────────
                try:
                    pg.remove_listener("request", _on_request)
                    pg.remove_listener("response", _on_response)
                except Exception:
                    pass
                for c in api_calls[-10:]:
                    log(f"[Wing API] {c}")
                log(f"[Wing 판매요청] URL: {pg.url[-60:]}")

                # ── API 201 성공 감지: 에러 팝업 여부와 무관하게 실제 등록 완료 확인 ─
                _api_registered = any(c.get("status") == 201 for c in api_calls)
                if _api_registered:
                    # 에러 팝업이 있었어도 실제 등록됐으면 오류 목록에서 제거
                    if _skip_item and errors and any(
                        k in errors[-1] for k in ("등록실패(에러지속)", "등록실패(버튼미발견)")
                    ):
                        errors.pop()
                    published += 1
                    if prod_name:
                        published_names.append(prod_name)
                    log(f"[Wing 판매요청] ✅ API 201 감지 → 실제 등록 완료 ({inv_id})")
                elif _skip_item:
                    # 에러로 이미 처리됨 → 다음 상품
                    pass
                else:
                    # ── Step 3: 등록 완료 대기 (3초) + 최종 에러 확인 ────────
                    await asyncio.sleep(3)

                    _err_final: str = await pg.evaluate("""
                        () => {
                            const ERR_KW = [
                                '필수 입력', '입력하세요', '입력 하세요',
                                '등록불가', '등록 불가', '오류가 발생', '실패했습니다',
                                '확인해주세요', '확인하세요', '잘못된 값',
                            ];
                            /* toast / snack / alert 계열 */
                            const SEL = [
                                '[class*="toast"]', '[class*="snack"]',
                                '[class*="alert"]:not([class*="success"])',
                                '[role="alert"]', '[role="alertdialog"]',
                                '[class*="error"]',
                            ];
                            for (const sel of SEL) {
                                for (const el of document.querySelectorAll(sel)) {
                                    if (!el.offsetParent) continue;
                                    const txt = el.textContent.trim();
                                    if (txt && ERR_KW.some(kw => txt.includes(kw)))
                                        return txt.substring(0, 150);
                                }
                            }
                            /* 아직 살아있는 에러 팝업 재확인 (텍스트 기반) */
                            const ERR_KW2 = ['확인해주세요', '입력항목을 모두', '옵션 항목을 확인'];
                            for (const btn of document.querySelectorAll('button')) {
                                if (!btn.offsetParent) continue;
                                if (btn.textContent.trim() !== '확인') continue;
                                let p = btn.parentElement;
                                for (let d = 0; d < 8 && p && p !== document.body; d++) {
                                    if (ERR_KW2.some(k => p.textContent.includes(k)))
                                        return p.textContent.trim().substring(0, 150);
                                    p = p.parentElement;
                                }
                            }
                            return '';
                        }
                    """) or ""

                    if not _err_final:
                        published += 1
                        if prod_name:
                            published_names.append(prod_name)
                        log(f"[Wing 판매요청] ✅ 등록 성공 ({inv_id})")
                    else:
                        log(f"[Wing 판매요청] ❌ 등록 실패 ({inv_id}): {_err_final[:80]}")
                        errors.append(f"등록실패({prod_name[:20]}): {_err_final[:50]}")
                        await self._shot(f"bulk_fail_{inv_id}")
            else:
                visible_btns = [t for t in all_btn_texts if t][:8]
                msg = f"판매요청 버튼 미발견 (ID:{inv_id}, 페이지버튼:{visible_btns})"
                log(f"[Wing 판매요청] ⚠️ {msg}")
                errors.append(msg)

            await self._shot(f"bulk_done_{inv_id}")

        await self._shot("bulk_99_done")
        log(f"[Wing 판매요청] 완료 — 판매요청: {published}개, 건너뜀: {skipped}개, 오류: {len(errors)}개")

        # ── 자동등록 완료 텔레그램 알림 ──────────────────────────────
        try:
            from modules.notifier import send_notification as _sn
            _err_cnt = len(errors)
            _notif = (
                f"🛒 <b>[쿠팡 자동등록 완료]</b>\n"
                f"성공: {published}개 / 건너뜀: {skipped}개"
                + (f" / 오류: {_err_cnt}개" if _err_cnt else "")
            )
            _sn(_notif)
        except Exception as _ne:
            print(f"[Wing] 텔레그램 알림 실패 (무시): {_ne}")

        return {
            "published":       published,
            "skipped":         skipped,
            "errors":          errors,
            "published_names": published_names,   # 이력 저장용
        }


# ── 동기 래퍼 ─────────────────────────────────────────────────────

def run_wing_edit_sync(
    username: str, password: str,
    params: WingEditParams,
    headless: bool = False,
) -> WingEditResult:
    """asyncio 이벤트 루프를 직접 생성해 edit_product 를 동기적으로 실행.

    실행 중 _current_automator 에 인스턴스를 등록해 GUI 에서
    signal_wing_continue() 를 통해 게이트를 해제할 수 있도록 한다.
    """
    global _current_automator

    async def _inner():
        global _current_automator
        async with WingAutomator(username, password, headless=headless) as wa:
            _current_automator = wa
            try:
                return await wa.edit_product(params)
            finally:
                _current_automator = None

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_inner())
    finally:
        loop.close()


def run_bulk_publish_sync(
    username: str,
    password: str,
    skip_names: list[str] | None = None,
    log_cb=None,
    progress_cb=None,
    product_data: dict | None = None,
    headless: bool = False,
    gemini_api_key: str = "",
) -> dict:
    """
    asyncio 이벤트 루프를 직접 생성해 bulk_publish_all 을 동기적으로 실행.
    GUI의 executor 스레드에서 호출하면 됨.
    """
    async def _inner():
        async with WingAutomator(username, password, headless=headless) as wa:
            return await wa.bulk_publish_all(
                skip_names=skip_names, log_cb=log_cb, progress_cb=progress_cb,
                product_data=product_data, gemini_api_key=gemini_api_key,
            )

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_inner())
    finally:
        loop.close()
