"""
Module 1 - Naver SmartStore Crawler

3-stage fallback:
  1) requests  -> HTML parse  (__PRELOADED_STATE__ / __NEXT_DATA__)
  2) Playwright ->
        a) Network response intercept (Naver 내부 API 응답 캡처)
        b) page.evaluate() 로 JS 변수 직접 추출
        c) HTML 파싱 fallback
  3) Naver REST API 직접 호출 (여러 엔드포인트 순차 시도)
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import urllib3
import requests
from bs4 import BeautifulSoup

# Bright Data 등 SSL 인터셉션 프록시 사용 시 InsecureRequestWarning 억제
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config.settings import Settings


# ================================================================
# Data models
# ================================================================

@dataclass
class DeliveryInfo:
    base_fee:    int           = 0
    fee_type:    str           = "FREE"
    bundle_unit: Optional[int] = None
    bundle_fee:  Optional[int] = None

    def effective_fee(self, qty: int) -> int:
        import math as _m
        if self.fee_type == "FREE":
            return 0
        if self.bundle_unit and self.bundle_fee:
            times = _m.ceil(qty / self.bundle_unit)
            result = times * self.bundle_fee
            print(
                f"[배송비계산] 기준수량n={self.bundle_unit} / 수집수량={qty} / "
                f"부과횟수=ceil({qty}/{self.bundle_unit})={times} / 최종배송비={result:,}원"
            )
            return result
        return self.base_fee


@dataclass
class NaverOption:
    """네이버 스마트스토어 옵션 1개."""
    name:      str   # 옵션명 (예: "올리브 오일 엑스트라 버진 No.1 500ml")
    add_price: int   # 추가금액 (기본가 대비 +N원, 보통 0)
    stock:     int   # 재고 수량
    image_url: str = ""  # 옵션별 대표 이미지 URL


@dataclass
class ProductData:
    url:              str
    product_id:       str
    store_name:       str
    name:             str
    price:            int
    delivery:         DeliveryInfo
    image_url:        str
    local_image_path: Optional[str]    = None
    raw_json:         dict             = field(default_factory=dict)
    barcode:          str              = ""
    naver_options:    list[NaverOption] = field(default_factory=list)  # 네이버 상품 옵션
    naver_category:   str              = ""  # 네이버 카테고리 전체 경로 (예: "식품>사탕/캔디>기타사탕")
    detail_images:    list[str]        = field(default_factory=list)  # 상품 상세페이지 이미지 URL 목록
    brand:            str              = ""  # 네이버 브랜드명


# ================================================================
# Crawler
# ================================================================

class NaverStoreCrawler:

    # ── Bright Data Web Unlocker API ──────────────────────────────
    BRIGHTDATA_API_URL = "https://api.brightdata.com/request"
    BRIGHTDATA_ZONE    = "web_unlocker1"

    # Chrome 131 (2024-11) 실제 브라우저와 동일한 헤더셋
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "Referer": "https://smartstore.naver.com/",
    }

    # curl_cffi 에만 쓸 최소 보충 헤더 (impersonate 가 나머지를 채움)
    _CFFI_EXTRA = {
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer":         "https://smartstore.naver.com/",
    }

    # Naver 내부 API 응답에서 상품 데이터임을 판단하는 URL 키워드
    _API_KEYWORDS = [
        "/products/", "channelProduct", "saleProduct",
        "productDetail", "productNo", "/catalog/",
    ]

    # 네이버 쇼핑 검색 URL 패턴 (search.shopping.naver.com)
    _NAVER_SEARCH_RE = re.compile(
        r"search\.shopping\.naver\.com/",
        re.IGNORECASE,
    )
    # SmartStore 상품 URL 추출용 정규식
    _SMARTSTORE_URL_RE = re.compile(
        r"https?://smartstore\.naver\.com/([^/?#\"'\s]+)/products/(\d+)"
    )

    def __init__(self, settings: Settings):
        self.settings  = settings

        # ── Bright Data Web Unlocker API 키 ──────────────────────
        self.api_key: str = getattr(settings, "BRIGHTDATA_API_KEY", "") or ""

        # ── Residential Proxy (구 방식, 미사용 시 빈값) ───────────
        self.proxy_url: Optional[str] = (
            getattr(settings, "RESIDENTIAL_PROXY_URL", "") or None
        )

        # ── requests.Session (이미지 다운로드 등 직접 요청용) ──────
        self.session = requests.Session()
        self.session.headers.update(self._HEADERS)
        self.session.verify = False   # SSL 인터셉션 허용

        if self.proxy_url:
            _px = {"http": self.proxy_url, "https": self.proxy_url}
            self.session.proxies.update(_px)

        # 초기화 로그
        if self.api_key:
            print(f"[Crawler] Bright Data Web Unlocker API: 활성화 (zone={self.BRIGHTDATA_ZONE})")
        elif self.proxy_url:
            print(f"[Crawler] Residential proxy: {self._mask_proxy(self.proxy_url)}")
        else:
            print("[Crawler] 직접 연결 (프록시/API 없음)")

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------

    @staticmethod
    def _mask_proxy(url: str) -> str:
        """user:pass@host:port → user:***@host:port (로그 보안)."""
        return re.sub(r"(://[^:@/]+:)[^@]+(@)", r"\1***\2", url)

    @staticmethod
    def _parse_proxy_auth(proxy_url: str) -> dict:
        """
        proxy URL → Playwright proxy dict.
        Playwright 은 server + username + password 를 분리해 전달해야 함.
        URL 에 credentials 를 통째로 넣으면 일부 버전에서 인증 실패.
        """
        from urllib.parse import urlparse
        p = urlparse(proxy_url)
        result: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            result["username"] = p.username
        if p.password:
            result["password"] = p.password
        return result

    def _check_ip_leak(self) -> None:
        """
        크롤링 직전 IP 노출 진단.
        프록시가 올바르게 적용됐으면 로컬 IP 가 아닌 프록시 IP 가 찍혀야 함.
        """
        CHECK_URLS = [
            "https://api.ipify.org?format=json",
            "https://httpbin.org/ip",
        ]
        def _get_ip(resp_json: dict) -> str:
            return resp_json.get("ip") or resp_json.get("origin", "?")

        tag = "[PROXY]" if self.proxy_url else "[DIRECT - no proxy]"

        # ── curl_cffi 경로 ────────────────────────────────────────
        _cf_ok = False
        try:
            from curl_cffi import requests as cf  # type: ignore
            _px = {"http": self.proxy_url, "https": self.proxy_url} if self.proxy_url else None
            for _url in CHECK_URLS:
                try:
                    r = cf.get(_url, impersonate="chrome124", proxies=_px, timeout=20, verify=False)
                    if r.status_code == 200:
                        ip = _get_ip(r.json())
                        print(f"[IP-CHECK] curl_cffi  -> {ip}  {tag}")
                        _cf_ok = True
                        break
                except Exception as _e:
                    print(f"[IP-CHECK] curl_cffi {_url} error: {_e}")
                    continue
        except ImportError:
            print("[IP-CHECK] curl_cffi not installed")
        if not _cf_ok:
            print("[IP-CHECK] curl_cffi IP check failed")

        # ── requests 경로 ────────────────────────────────────────
        _rq_ok = False
        for _url in CHECK_URLS:
            try:
                r = self.session.get(_url, timeout=20)
                if r.status_code == 200:
                    ip = _get_ip(r.json())
                    print(f"[IP-CHECK] requests   -> {ip}  {tag}")
                    _rq_ok = True
                    break
            except Exception as _e:
                print(f"[IP-CHECK] requests {_url} error: {_e}")
                continue
        if not _rq_ok:
            print("[IP-CHECK] requests IP check failed")

    # ------------------------------------------------------------
    # Bright Data Web Unlocker API
    # ------------------------------------------------------------

    def _web_unlocker_fetch(
        self,
        url: str,
        *,
        accept_json: bool = False,
        extra_headers: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Bright Data Web Unlocker API 를 통해 타겟 URL 콘텐츠를 반환한다.

        - WAF / CAPTCHA / 봇 탐지를 Bright Data 측에서 자동 우회
        - accept_json=True 시 Accept: application/json 헤더 추가
        - 성공 시 응답 본문(HTML 또는 JSON 문자열) 반환, 실패 시 None
        """
        if not self.api_key:
            return None

        req_headers: dict = {}
        if accept_json:
            req_headers["Accept"] = "application/json, */*"
        if extra_headers:
            req_headers.update(extra_headers)

        payload: dict = {
            "zone":    self.BRIGHTDATA_ZONE,
            "url":     url,
            "format":  "raw",       # Bright Data API 필수 필드
            "country": "kr",        # 한국 IP 경유 (Naver 접근 최적화)
        }
        if req_headers:
            payload["headers"] = req_headers

        try:
            resp = requests.post(
                self.BRIGHTDATA_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                },
                json=payload,
                timeout=90,
                verify=False,
            )
            sc = resp.status_code
            # resp.content 우선 사용 (gzip 등 인코딩 이슈 방지)
            raw_bytes = resp.content
            txt = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
            ct = resp.headers.get("content-type", "")
            print(f"[WU] {url} -> HTTP {sc}  len={len(txt)}  ct={ct[:60]}")
            if sc == 200:
                if not txt:
                    print("[WU] WARNING: HTTP 200이지만 빈 응답")
                    print(f"[WU] 응답 헤더: {dict(list(resp.headers.items())[:8])}")
                    return None
                # Bright Data가 JSON 래핑으로 응답할 경우 body 필드 추출
                if ct and "application/json" in ct:
                    try:
                        j = json.loads(txt)
                        if isinstance(j, dict):
                            body = j.get("body") or j.get("content") or j.get("html") or ""
                            if body:
                                print(f"[WU] JSON 래핑 응답 감지 → body 추출 len={len(body)}")
                                return body
                    except Exception:
                        pass
                return txt
            # 오류 내용 앞부분만 출력
            print(f"[WU] error body: {txt[:300]}")
            return None
        except Exception as e:
            print(f"[WU] request failed: {e}")
            return None

    # ------------------------------------------------------------
    # Public
    # ------------------------------------------------------------

    async def crawl(self, url: str) -> ProductData:
        store, pid = self._parse_url(url)
        print(f"[Crawler] store={store}  id={pid}")
        loop = asyncio.get_running_loop()

        # ── IP 노출 진단 (동기 IO → executor) ─────────────────────
        await loop.run_in_executor(None, self._check_ip_leak)

        # ── brand.naver.com → smartstore.naver.com 자동 변환 시도 ──
        # brand.naver.com은 SmartStore 채널이므로 동일 store/pid로 접근 가능.
        # brand WAF를 우회하면서 기존 smartstore 크롤러를 재사용.
        crawl_url = url
        if "brand.naver.com" in url:
            ss_equiv = f"https://smartstore.naver.com/{store}/products/{pid}"
            print(f"[Crawler] brand URL → smartstore 동등 URL 우선 시도: {ss_equiv}")
            raw_ss = await loop.run_in_executor(None, self._try_requests, ss_equiv)
            if raw_ss:
                print(f"[Crawler] brand→smartstore 변환 성공 (WAF 우회)")
                product = self._build(url, store, pid, raw_ss)
                product.local_image_path = await self._download_image(
                    product.image_url, product.product_id
                )
                return product
            print(f"[Crawler] smartstore 동등 URL 실패 → brand URL 직접 시도")

        # Stage 1: curl_cffi / requests (동기 → executor)
        raw = await loop.run_in_executor(None, self._try_requests, crawl_url)

        if raw is None:
            if self.api_key:
                # WU API 설정됨 → Naver REST API 엔드포인트(WU 경유) 시도
                print("[Crawler] stage 1 실패 + WU API 설정됨 → Naver API 직행")
                raw = await loop.run_in_executor(None, self._try_api, pid, store)

                # WU API도 실패(빈 응답/429 등)하면 nodriver → playwright 폴백
                if raw is None:
                    print("[Crawler] WU API 실패 → nodriver 폴백 시도")
                    raw = await self._try_nodriver(crawl_url)
                    if raw is None:
                        print("[Crawler] nodriver 실패 → Playwright 폴백 시도")
                        raw = await self._try_playwright(crawl_url)
            else:
                # Stage 2: nodriver (WebDriver 미사용 -탐지 우회율 최고)
                print("[Crawler] stage 1 실패 -> nodriver")
                raw = await self._try_nodriver(crawl_url)

                # Stage 3: Playwright (nodriver 불가 시 fallback)
                if raw is None:
                    print("[Crawler] nodriver 실패 -> Playwright")
                    raw = await self._try_playwright(crawl_url)

                # Stage 4: Naver REST API (동기 → executor)
                if raw is None:
                    print("[Crawler] stage 3 실패 -> Naver API")
                    raw = await loop.run_in_executor(None, self._try_api, pid, store)

        if raw is None:
            raise RuntimeError(
                f"모든 크롤링 시도 실패: {url}\n"
                f"IP 레이트리밋(429)이면 10~30분 후 재시도하세요."
            )

        product = self._build(url, store, pid, raw)

        product.local_image_path = await self._download_image(
            product.image_url, product.product_id
        )
        return product

    # ------------------------------------------------------------
    # URL parsing & Naver Shopping search expansion
    # ------------------------------------------------------------

    @classmethod
    def is_naver_search_url(cls, url: str) -> bool:
        """네이버 쇼핑 검색 결과 URL 여부 판단."""
        return bool(cls._NAVER_SEARCH_RE.search(url))

    @staticmethod
    def _parse_url(url: str) -> tuple[str, str]:
        m = re.search(r"smartstore\.naver\.com/([^/?#]+)/products/(\d+)", url)
        if m:
            return m.group(1), m.group(2)
        m = re.search(r"brand\.naver\.com/([^/?#]+)/products/(\d+)", url)
        if m:
            return m.group(1), m.group(2)
        raise ValueError(
            f"SmartStore/Brand 상품 URL이 아닙니다.\n"
            f"올바른 형식: https://smartstore.naver.com/스토어명/products/상품번호\n"
            f"         또는 https://brand.naver.com/브랜드명/products/상품번호\n"
            f"입력값: {url}"
        )

    async def extract_smartstore_urls(self, search_url: str) -> list[str]:
        """
        네이버 쇼핑 검색 결과 URL에서 SmartStore 상품 URL 목록을 추출한다.

        시도 순서:
          1) Bright Data Web Unlocker API (HTML 파싱)
          2) Playwright (headless=True → False 재시도)

        반환: ["https://smartstore.naver.com/store/products/id", ...]
        """
        def _extract(html: str) -> list[str]:
            seen: set[str] = set()
            result: list[str] = []
            for m in self._SMARTSTORE_URL_RE.finditer(html):
                store = m.group(1)
                pid   = m.group(2)
                # 스토어 ID로 보이지 않는 경로 조각 필터링
                if len(store) < 2 or store in ("main", "i", "v1", "v2"):
                    continue
                canon = f"https://smartstore.naver.com/{store}/products/{pid}"
                if canon not in seen:
                    seen.add(canon)
                    result.append(canon)
            return result

        loop = asyncio.get_running_loop()
        print(f"[Crawler] 네이버 쇼핑 검색 URL 감지: {search_url[:80]}")

        # ── 1) Web Unlocker API ──────────────────────────────────────────
        if self.api_key:
            html = await loop.run_in_executor(
                None, lambda: self._web_unlocker_fetch(search_url)
            )
            if html:
                found = _extract(html)
                if found:
                    print(
                        f"[Crawler] SmartStore URL {len(found)}개 추출 (Web Unlocker)"
                    )
                    return found
                print("[Crawler] Web Unlocker 응답에 SmartStore URL 없음 → Playwright 시도")

        # ── 2) Playwright ────────────────────────────────────────────────
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("[Crawler] playwright 미설치 — SmartStore URL 추출 불가")
            return []

        for headless in (True, False):
            try:
                async with async_playwright() as p:
                    args = [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--ignore-certificate-errors",
                    ]
                    if not headless:
                        args += ["--window-position=-32000,-32000",
                                 "--window-size=1280,800"]
                    browser = None
                    for cfg in (
                        {"channel": "chrome",   "headless": headless},
                        {"channel": "chromium", "headless": headless},
                        {"headless": headless},
                    ):
                        try:
                            browser = await p.chromium.launch(**cfg, args=args)
                            break
                        except Exception:
                            continue
                    if browser is None:
                        continue

                    ctx = await browser.new_context(
                        user_agent=self._HEADERS["User-Agent"],
                        locale="ko-KR",
                        ignore_https_errors=True,
                    )
                    page = await ctx.new_page()
                    await page.goto(
                        search_url, wait_until="domcontentloaded", timeout=30_000
                    )
                    # 동적 콘텐츠 로딩 대기
                    try:
                        await page.wait_for_function(
                            "() => document.querySelectorAll('a[href]').length > 20",
                            timeout=10_000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # 추가 스크롤로 더 많은 상품 로드
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(1)

                    html = await page.content()
                    await browser.close()

                found = _extract(html)
                if found:
                    print(
                        f"[Crawler] SmartStore URL {len(found)}개 추출 "
                        f"(Playwright headless={headless})"
                    )
                    return found
                print(f"[Crawler] Playwright(headless={headless}): SmartStore URL 없음")

            except Exception as exc:
                print(f"[Crawler] Playwright 검색 추출 오류(headless={headless}): {exc}")

        print("[Crawler] 네이버 쇼핑 검색에서 SmartStore URL을 찾지 못했습니다.")
        return []

    # ------------------------------------------------------------
    # Stage 1 : Web Unlocker API → curl_cffi → requests (fallback 순)
    # ------------------------------------------------------------

    def _try_requests(self, url: str) -> Optional[dict]:
        clean = url.split("?")[0]   # 트래킹 파라미터 제거
        _is_brand = "brand.naver.com" in url
        _referer = "https://brand.naver.com/" if _is_brand else "https://smartstore.naver.com/"
        _cffi_extra = {**self._CFFI_EXTRA, "Referer": _referer}

        # ── 1-A: Bright Data Web Unlocker API (WAF 자동 우회) ────────
        if self.api_key:
            html = self._web_unlocker_fetch(clean)
            if html and len(html) >= 1000:
                data = self._parse_html(html)
                if data:
                    print("[Crawler] stage 1 OK (Web Unlocker API)")
                    return data
                print("[Crawler] stage 1 WU: HTML 수신했으나 상품 데이터 없음")
            elif html:
                print(f"[Crawler] stage 1 WU: 응답 너무 짧음 len={len(html)} (봇 탐지 or 오류 페이지)")
            # WU 실패 시 curl_cffi 로 폴백

        # ── 1-B: curl_cffi (Chrome TLS 지문 위장, 직접 연결) ─────────
        try:
            from curl_cffi import requests as cf  # type: ignore
            _cf_proxies = (
                {"http": self.proxy_url, "https": self.proxy_url}
                if self.proxy_url else None
            )
            resp = cf.get(
                clean,
                impersonate="chrome124",
                headers=_cffi_extra,
                proxies=_cf_proxies,
                timeout=20,
                verify=False,
            )
            sc = resp.status_code
            print(f"[Crawler] stage 1 curl_cffi (chrome124): status={sc}  len={len(resp.text)}")
            if sc == 200 and len(resp.text) >= 1000:
                data = self._parse_html(resp.text)
                if data:
                    print("[Crawler] stage 1 OK (curl_cffi)")
                    return data
                print("[Crawler] stage 1 curl_cffi: HTML 수신했으나 상품 데이터 없음")
            elif sc == 429:
                print("[Crawler] stage 1 curl_cffi: 429 - IP 레이트리밋")
            elif sc == 490:
                print("[Crawler] stage 1 curl_cffi: 490 - Akamai WAF 차단")
        except ImportError:
            print("[Crawler] curl_cffi 미설치 -> requests fallback")
        except Exception as e:
            print(f"[Crawler] stage 1 curl_cffi error: {e}")

        # ── 1-C: requests 직접 연결 (최후 폴백) ─────────────────────
        try:
            resp = self.session.get(clean, timeout=15, headers={"Referer": _referer})
            sc = resp.status_code
            print(f"[Crawler] stage 1 requests: status={sc}  len={len(resp.text)}")
            resp.raise_for_status()
            if len(resp.text) < 1000:
                print("[Crawler] stage 1 requests: 응답이 너무 짧음 (봇 탐지 페이지)")
                return None
            data = self._parse_html(resp.text)
            if data:
                print("[Crawler] stage 1 OK (requests)")
            else:
                print("[Crawler] stage 1 requests: HTML 수신했으나 상품 데이터 없음")
            return data
        except Exception as e:
            print(f"[Crawler] stage 1 error: {e}")
            return None

    # ------------------------------------------------------------
    # ------------------------------------------------------------
    # Stage 2 : nodriver  (WebDriver 미사용 -가장 강력한 봇 탐지 우회)
    # ------------------------------------------------------------

    async def _try_nodriver(self, url: str) -> Optional[dict]:
        """
        nodriver: CDP를 직접 제어해 WebDriver 흔적을 남기지 않음.
        Playwright/Selenium보다 탐지율이 현저히 낮음.
        Chrome 창은 모니터 바깥(-32000)에 위치시켜 사용자 화면에 보이지 않게 함.
        """
        try:
            import nodriver as uc  # type: ignore
        except ImportError:
            print("[Crawler] nodriver 미설치 -> Playwright fallback")
            return None

        clean_url = url.split("?")[0]
        browser = None
        try:
            _nd_args = [
                "--window-position=-32000,-32000",
                "--window-size=1280,800",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--ignore-certificate-errors",          # Bright Data SSL 인터셉션 허용
                "--ignore-certificate-errors-spki-list",
            ]
            if self.proxy_url:
                _nd_args.append(f"--proxy-server={self.proxy_url}")

            browser = await uc.start(
                headless=False,
                browser_args=_nd_args,
            )
            print("[Crawler] nodriver Chrome 시작")

            # naver.com 먼저 방문 (세션 쿠키 확보)
            tab = await browser.get("https://www.naver.com")
            await tab.sleep(2)
            print("[Crawler] nodriver: naver.com 사전 방문 완료")

            # 상품 페이지 이동
            tab = await browser.get(clean_url)
            await tab.sleep(3)

            # 페이지 상태 확인
            try:
                title    = await tab.evaluate("document.title")
                body_len = await tab.evaluate(
                    "document.body ? document.body.innerText.length : 0"
                )
                print(f"[Crawler] nodriver: title={title!r}  bodyLen={body_len}")
                if isinstance(body_len, (int, float)) and int(body_len) < 500:
                    print("[Crawler] nodriver: 봇 탐지 페이지 (body 짧음)")
                    return None
            except Exception:
                pass

            # JS 변수 직접 추출
            for expr in ["window.__PRELOADED_STATE__", "window.__NEXT_DATA__"]:
                try:
                    data = await tab.evaluate(expr)
                    if isinstance(data, dict) and data:
                        print(f"[Crawler] nodriver: {expr} 추출 성공")
                        return data
                except Exception:
                    pass

            # 네트워크 인터셉트 없이 HTML 파싱 fallback
            html = await tab.get_content()
            result = self._parse_html(html)
            if result:
                print("[Crawler] nodriver: HTML 파싱 성공")
            else:
                print(f"[Crawler] nodriver: HTML len={len(html)}, 데이터 없음")
            return result

        except Exception as exc:
            print(f"[Crawler] nodriver 오류: {exc}")
            return None
        finally:
            if browser:
                try:
                    browser.stop()
                except Exception:
                    pass

    # ------------------------------------------------------------
    # Stage 3 : Playwright
    # ------------------------------------------------------------

    async def _try_playwright(self, url: str) -> Optional[dict]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            print("[Crawler] playwright not installed")
            return None

        # headless=True 먼저, 봇 탐지 시 headless=False(오프스크린) 재시도
        for headless in [True, False]:
            if not headless:
                print("[Crawler] headless 봇 탐지됨 -> visible browser 재시도 (off-screen)")
            for install_attempt in range(2):
                try:
                    data = await self._pw_fetch(url, async_playwright, headless=headless)
                    if data:
                        print(f"[Crawler] stage 2 OK (headless={headless})")
                        return data
                    break  # 데이터 없음(봇 탐지) → 다음 headless 모드로
                except Exception as e:
                    msg = str(e)
                    if ("Executable doesn't exist" in msg or
                            "playwright install" in msg) and install_attempt == 0:
                        print("[Crawler] browser missing -> auto-install")
                        ok = await asyncio.get_running_loop().run_in_executor(
                            None, self._install_chromium
                        )
                        if ok:
                            continue
                    print(f"[Crawler] Playwright error (headless={headless}): {e}")
                    break

        return None

    async def _pw_fetch(
        self, url: str, async_playwright, *, headless: bool = True
    ) -> Optional[dict]:
        """
        봇 탐지 우회 + 네트워크 인터셉트 + JS 변수 추출

        headless=False 시 창을 모니터 바깥(−32000,−32000)에 띄워
        사용자 화면에 보이지 않도록 처리한다.
        """
        captured: list[dict] = []

        async with async_playwright() as p:
            # 공통 Chromium 플래그
            args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
                "--ignore-certificate-errors",           # Bright Data SSL 인터셉션 허용
                "--ignore-certificate-errors-spki-list",
                "--disable-web-security",
            ]
            if not headless:
                # 창을 화면 밖에 위치시켜 사용자에게 보이지 않게 함
                args += ["--window-position=-32000,-32000", "--window-size=1280,800"]

            browser = None
            for cfg in [
                {"channel": "chrome",   "headless": headless},
                {"channel": "chromium", "headless": headless},
                {"headless": headless},
            ]:
                try:
                    browser = await p.chromium.launch(**cfg, args=args)
                    print(f"[Crawler] browser: {cfg}")
                    break
                except Exception:
                    continue
            if browser is None:
                raise RuntimeError("No browser available")

            # Playwright 프록시: server + username + password 분리 전달
            # (URL 에 credentials 를 통째로 넣으면 인증 실패 발생)
            _pw_proxy = self._parse_proxy_auth(self.proxy_url) if self.proxy_url else None
            ctx = await browser.new_context(
                user_agent=self._HEADERS["User-Agent"],
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1280, "height": 800},
                proxy=_pw_proxy,
                ignore_https_errors=True,               # Bright Data SSL 인터셉션 허용
                extra_http_headers={
                    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,image/apng,*/*;"
                        "q=0.8,application/signed-exchange;v=b3;q=0.7"
                    ),
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "sec-ch-ua": self._HEADERS["sec-ch-ua"],
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "sec-fetch-site": "none",
                    "sec-fetch-user": "?1",
                    "upgrade-insecure-requests": "1",
                },
            )
            page = await ctx.new_page()

            # ── 봇 탐지 우회: playwright-stealth 2.x API ──────────────────
            _stealth_ok = False
            try:
                from playwright_stealth import Stealth  # type: ignore  (v2.x)
                _stealth = Stealth(
                    navigator_languages_override=("ko-KR", "ko"),
                    navigator_platform_override="Win32",
                    navigator_user_agent_override=self._HEADERS["User-Agent"],
                )
                await _stealth.apply_stealth_async(page)
                _stealth_ok = True
                print("[Crawler] playwright-stealth 2.x applied (ko-KR, Win32)")
            except Exception as _se:
                print(f"[Crawler] playwright-stealth error: {_se}")

            if not _stealth_ok:
                await page.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
                    Object.defineProperty(navigator, 'languages',
                        { get: () => ['ko-KR','ko','en-US','en'] });
                    window.chrome = {
                        runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}
                    };
                    const _origPerms = navigator.permissions.query.bind(navigator.permissions);
                    navigator.permissions.query = (p) =>
                        p.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : _origPerms(p);
                """)

            # ── Step 1: 네이버 홈 방문 (쿠키/세션 확보) ──────────────────
            try:
                await page.goto(
                    "https://www.naver.com",
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                await asyncio.sleep(3)
                _pre_title = await page.title()
                _pre_url   = page.url
                print(f"[Crawler] step1 naver.com: title={_pre_title!r}  url={_pre_url[:60]}")
                if "login" in _pre_url.lower() or "login" in _pre_title.lower():
                    print("[Crawler] WARNING: naver.com -> login redirect (IP flagged?)")
            except Exception as _pre_err:
                print(f"[Crawler] naver.com pre-visit failed: {_pre_err}")

            # ── Step 2: 스토어/브랜드 홈 방문 (자연스러운 경로) ──────────
            if "brand.naver.com" in url:
                _store = url.split("brand.naver.com/")[-1].split("/")[0]
                _store_home = f"https://brand.naver.com/{_store}"
            else:
                _store = url.split("smartstore.naver.com/")[-1].split("/")[0]
                _store_home = f"https://smartstore.naver.com/{_store}"
            try:
                await page.goto(
                    _store_home,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                await asyncio.sleep(2)
                _store_title = await page.title()
                print(f"[Crawler] step2 store home: title={_store_title!r}")
            except Exception as _se2:
                print(f"[Crawler] store home visit failed: {_se2}")

            # ── 네트워크 응답 인터셉트 ────────────────────────────────────
            async def on_response(resp):
                try:
                    if resp.status != 200:
                        return
                    if "json" not in resp.headers.get("content-type", ""):
                        return
                    if not any(kw in resp.url for kw in self._API_KEYWORDS):
                        return
                    body = await resp.body()
                    data = json.loads(body)
                    if isinstance(data, dict) and len(data) > 1:
                        captured.append(data)
                        print(f"[Crawler] API captured: ...{resp.url[-60:]}")
                except Exception:
                    pass

            page.on("response", lambda r: asyncio.ensure_future(on_response(r)))

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

                # 콘텐츠 로딩 대기
                try:
                    await page.wait_for_function(
                        "() => document.body && document.body.innerText.length > 1000",
                        timeout=15_000,
                    )
                except Exception:
                    pass
                await asyncio.sleep(2)

                # 페이지 상태 디버그
                try:
                    dbg = await page.evaluate(
                        "() => ({"
                        "  title: document.title,"
                        "  preloaded: typeof window.__PRELOADED_STATE__,"
                        "  nextData:  typeof window.__NEXT_DATA__,"
                        "  bodyLen:   document.body ? document.body.innerText.length : 0"
                        "})"
                    )
                    print(f"[Crawler] page: {dbg}")
                    if dbg.get("bodyLen", 0) < 500:
                        print("[Crawler] bot-detection page (body too short) -> None")
                        return None
                except Exception:
                    pass

                # 1) 캡처된 API 응답 우선
                for data in captured:
                    node = self._find_product_node(data)
                    if self._get_price(node) > 0 or self._get_name(node) != "Unknown":
                        print("[Crawler] product found in intercepted API response")
                        return data

                # 2) JS 변수 직접 추출
                for expr in [
                    "window.__PRELOADED_STATE__",
                    "window.__NEXT_DATA__",
                    "window.__NEXT_DATA__ && window.__NEXT_DATA__.props "
                    "&& window.__NEXT_DATA__.props.pageProps",
                ]:
                    try:
                        data = await page.evaluate(expr)
                        if data and isinstance(data, dict):
                            return data
                    except Exception:
                        pass

                # 3) HTML 파싱 fallback
                html = await page.content()
                result = self._parse_html(html)
                if not result:
                    print(f"[Crawler] HTML len={len(html)}, no product data found")
                return result

            finally:
                await browser.close()

    @staticmethod
    def _install_chromium() -> bool:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True, text=True, timeout=300,
            )
            if r.returncode == 0:
                print("[Crawler] Chromium installed")
                return True
            print(f"[Crawler] install failed: {r.stderr[:300]}")
            return False
        except Exception as e:
            print(f"[Crawler] install error: {e}")
            return False

    # ------------------------------------------------------------
    # Stage 4 : Naver REST API (Web Unlocker API 우선, curl_cffi 폴백)
    # ------------------------------------------------------------

    def _try_api(self, product_id: str, store_name: str, is_brand: bool = False) -> Optional[dict]:
        import time

        if is_brand:
            endpoints = [
                f"https://brand.naver.com/main/v1/products/no/{product_id}",
                f"https://brand.naver.com/i/v1/products/{product_id}",
                f"https://brand.naver.com/main/v1/stores/{store_name}/products/{product_id}",
                f"https://smartstore.naver.com/main/v1/products/no/{product_id}",
                f"https://smartstore.naver.com/i/v1/products/{product_id}",
            ]
        else:
            endpoints = [
                f"https://smartstore.naver.com/main/v1/products/no/{product_id}",
                f"https://smartstore.naver.com/i/v1/products/{product_id}",
                f"https://smartstore.naver.com/main/v1/stores/{store_name}/products/{product_id}",
                f"https://smartstore.naver.com/main/v2/products/{product_id}",
            ]

        # ── 4-A: Bright Data Web Unlocker API (JSON 엔드포인트 우회) ──
        if self.api_key:
            for ep in endpoints:
                txt = self._web_unlocker_fetch(ep, accept_json=True)
                if not txt:
                    continue
                try:
                    data = json.loads(txt)
                    if data:
                        print(f"[Crawler] stage 4 OK (Web Unlocker JSON): {ep}")
                        return {"product": data} if "product" not in data else data
                except json.JSONDecodeError:
                    # JSON 아닐 수도 있음 (HTML 응답 시) → HTML 파싱 시도
                    parsed = self._parse_html(txt)
                    if parsed:
                        print(f"[Crawler] stage 4 OK (Web Unlocker HTML): {ep}")
                        return parsed

        # ── 4-B: curl_cffi 직접 연결 폴백 ─────────────────────────────
        _cf_session = None
        try:
            from curl_cffi.requests import Session as CfSession  # type: ignore
            _cf_session = CfSession(impersonate="chrome124")
            _cf_session.verify = False
        except Exception:
            pass

        _proxy_kw: dict = (
            {"proxies": {"http": self.proxy_url, "https": self.proxy_url}}
            if self.proxy_url else {}
        )
        _headers = {**self._HEADERS, "Accept": "application/json, */*"}

        for ep in endpoints:
            for attempt in range(2):
                try:
                    getter = _cf_session or self.session
                    resp = getter.get(ep, headers=_headers, timeout=15, **_proxy_kw)
                    print(f"[Crawler] stage 4 cffi: {resp.status_code} {ep[-60:]}")
                    if resp.status_code == 200:
                        data = resp.json()
                        if data:
                            print(f"[Crawler] stage 4 OK (curl_cffi): {ep}")
                            return {"product": data} if "product" not in data else data
                        break
                    elif resp.status_code == 429:
                        wait = 5 * (attempt + 1)
                        print(f"[Crawler] 429 rate-limit, waiting {wait}s ...")
                        time.sleep(wait)
                    else:
                        break
                except Exception as e:
                    print(f"[Crawler] stage 4 cffi error ({ep[-50:]}): {e}")
                    break

        if _cf_session:
            try:
                _cf_session.close()
            except Exception:
                pass
        return None

    # ------------------------------------------------------------
    # HTML → dict (4가지 패턴)
    # ------------------------------------------------------------

    @staticmethod
    def _parse_html(html: str) -> Optional[dict]:
        """
        파싱 우선순위:
          1) window.__PRELOADED_STATE__  - 배송비/옵션 포함 완전한 데이터
          2) JSON.parse('...') 형태
          3) <script id="__NEXT_DATA__">
          4) application/json 스크립트 태그
          5) OG / Kakao Commerce 메타 태그 (최후 수단, 배송비 없음)
        """
        soup = BeautifulSoup(html, "lxml")

        # ── 1) window.__PRELOADED_STATE__ — JS undefined 제거 후 raw_decode ─────
        m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{)", html)
        if m:
            try:
                # { 위치부터 추출 후 JS 전용 문법 → JSON 호환으로 변환
                text_from = html[m.start(1):]
                # undefined → null  (JavaScript 는 유효하나 JSON 은 무효)
                text_clean = re.sub(r':\s*undefined\b', ': null', text_from)
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(text_clean, 0)
                if isinstance(data, dict):
                    print("[Parser] __PRELOADED_STATE__ 파싱 성공")
                    # HTML 텍스트에서 "N개 마다 부과" 힌트 주입 (JSON 키 탐색 실패 시 사용)
                    _bm_pre = re.search(r"(\d+)\s*개\s*마다\s*부과", html)
                    if _bm_pre:
                        data["_html_bundle_unit"] = int(_bm_pre.group(1))
                    return data
            except json.JSONDecodeError as _jde:
                print(f"[Parser] __PRELOADED_STATE__ raw_decode 실패: {_jde}")

        # ── 2) JSON.parse('...') 형태 ────────────────────────────────────────
        m = re.search(
            r"window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\((['\"])(.+?)\1\)",
            html, re.DOTALL,
        )
        if m:
            raw_str = m.group(2)
            for decoder_fn in [
                lambda s: json.loads(s),
                lambda s: json.loads(s.encode().decode("unicode_escape")),
            ]:
                try:
                    result = decoder_fn(raw_str)
                    print("[Parser] __PRELOADED_STATE__ JSON.parse 파싱 성공")
                    if isinstance(result, dict):
                        _bm_pre2 = re.search(r"(\d+)\s*개\s*마다\s*부과", html)
                        if _bm_pre2:
                            result["_html_bundle_unit"] = int(_bm_pre2.group(1))
                    return result
                except Exception:
                    pass

        # ── 3) <script id="__NEXT_DATA__"> ───────────────────────────────────
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag and tag.string:
            try:
                data = json.loads(tag.string)
                props = data.get("props", {}).get("pageProps", {})
                print("[Parser] __NEXT_DATA__ 파싱 성공")
                return props if props else data
            except Exception:
                pass

        # ── 4) application/json 스크립트 태그 ────────────────────────────────
        for tag in soup.find_all("script", {"type": "application/json"}):
            try:
                data = json.loads(tag.string or "")
                if isinstance(data, dict) and any(
                    k in data for k in ("product", "salePrice", "productName")
                ):
                    print("[Parser] application/json 태그 파싱 성공")
                    return data
            except Exception:
                continue

        # ── 5) OG / Kakao Commerce 메타 태그 (최후 수단 — 배송비 없음) ──────────
        # 배송비 정보가 없으므로 __PRELOADED_STATE__ 파싱 실패 시에만 사용
        def _meta(prop: str) -> str:
            t = soup.find("meta", property=prop)
            return t["content"].strip() if t and t.get("content") else ""

        _og_title = _meta("og:title")
        _kk_price = _meta("kakao:commerce:price") or _meta("kakao:commerce:regular_price")
        _og_image = _meta("og:image") or _meta("kakao:commerce:product_image_url")

        _price_int = 0
        if _kk_price:
            try:
                _price_int = int(_kk_price)
            except ValueError:
                pass

        # og:title 형식: "상품명 : 스토어명" → 상품명만 추출
        _raw_name = _og_title
        if " : " in _raw_name:
            _raw_name = _raw_name.split(" : ")[0].strip()

        if _price_int > 0 and _raw_name:
            print(f"[Parser] meta-tag fallback: name={_raw_name[:40]}  price={_price_int}")
            print("[Parser] WARNING: 배송비 정보 없음 — __PRELOADED_STATE__ 파싱 실패")
            # HTML 텍스트에서 "N개 마다 부과" 패턴 추출 → _html_bundle_unit 힌트로 저장
            _html_bundle: dict = {}
            _bm = re.search(r"(\d+)\s*개\s*마다\s*부과", html)
            if _bm:
                _html_bundle["_html_bundle_unit"] = int(_bm.group(1))
            return {"product": {
                "productName":            _raw_name,
                "salePrice":              _price_int,
                "representativeImageUrl": _og_image,
            }, **_html_bundle}

        # HTML 텍스트에서 bundle hint 추출 후 raw에 병합 (최후 수단)
        _bm2 = re.search(r"(\d+)\s*개\s*마다\s*부과", html)
        if _bm2:
            return {"_html_bundle_unit": int(_bm2.group(1))}

        return None

    # ------------------------------------------------------------
    # ProductData 조립
    # ------------------------------------------------------------

    def _build(self, url: str, store: str, pid: str, raw: dict) -> ProductData:
        node          = self._find_product_node(raw)
        name          = self._get_name(node)
        sale_price    = self._get_price(node)
        delivery      = self._get_delivery(node, raw)
        img_url       = self._get_image(node) or self._get_image(raw)
        barcode       = self._get_barcode(node, raw)
        options       = self._get_options(raw)
        naver_cat     = self._get_naver_category(node, raw)
        brand         = self._get_brand(node, raw)

        discounted = self._find_discounted_price(raw, sale_price)
        if discounted and discounted < sale_price:
            print(f"[Crawler] price   : {sale_price:,} KRW (정상가) → {discounted:,} KRW (즉시할인가 적용)")
            price = discounted
        else:
            price = sale_price

        print(f"[Crawler] name    : {name}")
        print(f"[Crawler] price   : {price:,} KRW")
        print(f"[Crawler] ship    : {delivery.base_fee:,} KRW ({delivery.fee_type})")
        if delivery.bundle_unit:
            print(f"[Crawler] bundle  : +{delivery.bundle_fee:,} KRW / {delivery.bundle_unit} items")
        if barcode:
            print(f"[Crawler] barcode : {barcode}")
        if naver_cat:
            print(f"[Crawler] naver_cat: {naver_cat}")
        if brand:
            print(f"[Crawler] brand    : {brand}")
        # ── 옵션 없으면 WU API로 전용 option endpoint 호출 ──────────
        if not options and self.api_key:
            options = self._fetch_options_via_wu(pid)

        if options:
            print(f"[Crawler] options : {len(options)}개 — " +
                  ", ".join(f"{o.name}(+{o.add_price:,}원)" for o in options[:3]) +
                  ("..." if len(options) > 3 else ""))

        return ProductData(
            url=url, product_id=pid, store_name=store,
            name=name, price=price, delivery=delivery,
            image_url=img_url, raw_json=raw, barcode=barcode,
            naver_options=options, naver_category=naver_cat,
            brand=brand,
        )

    def _fetch_options_via_wu(self, pid: str) -> list[NaverOption]:
        """
        Naver SmartStore 전용 option API를 WU(Web Unlocker)로 직접 호출.
        /i/v1/products/{pid}/option-combinations  또는
        /i/v1/products/{pid}/option-simples  엔드포인트 사용.
        """
        endpoints = [
            f"https://smartstore.naver.com/i/v1/products/{pid}/option-combinations",
            f"https://smartstore.naver.com/i/v1/products/{pid}/option-simples",
        ]
        extra_hdrs = {
            "Accept": "application/json, */*",
            "Referer": f"https://smartstore.naver.com/",
        }
        for ep in endpoints:
            try:
                txt = self._web_unlocker_fetch(ep, accept_json=True, extra_headers=extra_hdrs)
                if not txt:
                    continue
                data = json.loads(txt)
                results: list[NaverOption] = []
                # 응답 형식: {"optionCombinations": [...]} or {"optionSimples": [...]} or 배열
                items_list = []
                if isinstance(data, list):
                    items_list = data
                elif isinstance(data, dict):
                    for k in ("optionCombinations", "optionSimples", "combinations",
                              "items", "options", "data"):
                        v = data.get(k)
                        if isinstance(v, list) and v:
                            items_list = v
                            break
                    if not items_list:
                        # 중첩 탐색
                        for v in data.values():
                            if isinstance(v, list) and v and isinstance(v[0], dict):
                                items_list = v
                                break

                for item in items_list:
                    if not isinstance(item, dict):
                        continue
                    parts = []
                    for nk in range(1, 5):
                        p = (item.get(f"optionName{nk}") or
                             item.get(f"name{nk}") or "").strip()
                        if p:
                            parts.append(p)
                    if not parts:
                        p = (item.get("optionName") or item.get("name") or
                             item.get("value") or item.get("title") or "").strip()
                        if p:
                            parts.append(p)
                    if not parts:
                        continue
                    add_price = int(item.get("addPrice", 0) or item.get("price", 0) or 0)
                    stock = int(item.get("stockQuantity", 999) or item.get("stock", 999) or 999)
                    img = (item.get("representImage") or item.get("imageUrl") or
                           item.get("image") or "")
                    if isinstance(img, dict):
                        img = img.get("url", "")
                    results.append(NaverOption(
                        name=" / ".join(parts),
                        add_price=add_price,
                        stock=max(stock, 0),
                        image_url=img or "",
                    ))

                # 재고 0 및 중복 제거
                seen: set[str] = set()
                filtered = []
                for o in results:
                    if o.stock == 0 or o.name in seen:
                        continue
                    seen.add(o.name)
                    filtered.append(o)

                if filtered:
                    print(f"[Crawler] WU option API 성공 ({ep.split('/')[-1]}): {len(filtered)}개")
                    return filtered

            except Exception as e:
                print(f"[Crawler] WU option API 실패 ({ep}): {e}")

        return []

    @staticmethod
    def _get_options(raw: dict) -> list[NaverOption]:
        """
        네이버 __PRELOADED_STATE__ 에서 옵션 조합 추출.
        optionCombinations / optionItems / stockList 등 다양한 키 대응.
        """
        results: list[NaverOption] = []

        # ── 1순위: selectedOptions.A 내부 직접 탐색 (Naver SmartStore 실제 구조) ──
        sel_a = (raw.get("selectedOptions") or {}).get("A") or {}
        for _direct_key in ("combinationOptions", "textOptions", "simpleOptions",
                            "standardOptions", "colorOptions", "sizeOptions"):
            _dv = sel_a.get(_direct_key)
            if isinstance(_dv, list) and _dv:
                for _di in _dv:
                    if not isinstance(_di, dict): continue
                    _n = (_di.get("optionName") or _di.get("name") or
                          _di.get("value") or _di.get("text") or "").strip()
                    if not _n: continue
                    _ap = int(_di.get("addPrice", 0) or _di.get("price", 0) or 0)
                    _st = int(_di.get("stockQuantity", 999) or _di.get("stock", 999) or 999)
                    _im = (_di.get("representImage") or _di.get("imageUrl") or
                           _di.get("image") or "")
                    if isinstance(_im, dict):
                        _im = _im.get("url", "")
                    results.append(NaverOption(name=_n, add_price=_ap,
                                               stock=_st, image_url=_im or ""))
                if results:
                    return results

        # ── 2순위: product.A.productOptions 내부 탐색 ──────────────
        for _pkey in ("product", "productDetail"):
            _prod = raw.get(_pkey) or {}
            if isinstance(_prod, dict):
                _prod = _prod.get("A") or _prod
            if isinstance(_prod, dict):
                _popts = _prod.get("productOptions") or {}
                for _pok in ("optionCombinations", "combinations", "optionItems"):
                    _pov = _popts.get(_pok) if isinstance(_popts, dict) else None
                    if isinstance(_pov, list) and _pov:
                        for _pi in _pov:
                            if not isinstance(_pi, dict): continue
                            parts = []
                            for n in range(1, 5):
                                p = (_pi.get(f"optionName{n}") or "").strip()
                                if p: parts.append(p)
                            if not parts:
                                p = (_pi.get("optionName") or _pi.get("name") or "").strip()
                                if p: parts.append(p)
                            if not parts: continue
                            _ap = int(_pi.get("addPrice", 0) or _pi.get("price", 0) or 0)
                            _st = int(_pi.get("stockQuantity", 999) or 999)
                            _im = _pi.get("representImage") or _pi.get("imageUrl") or ""
                            if isinstance(_im, dict): _im = _im.get("url", "")
                            results.append(NaverOption(
                                name=" / ".join(parts), add_price=_ap,
                                stock=_st, image_url=_im or ""
                            ))
                        if results:
                            return results

        def _search(d, depth=0):
            if depth > 10 or not isinstance(d, dict):
                return
            # ── 재귀 옵션 배열 탐색 ─────────────────────────
            for key in (
                "optionCombinations", "optionItems", "combinations",
                "stockList", "optionList",
                "productOptions", "optionGroups", "optionDetails",
                "productOptionDetails", "optionInfoList", "optionSimpleInfo",
                "simpleProductList", "allOptionCombinations",
                "combinationOptions", "textOptions", "simpleOptions",
                "standardOptions", "colorOptions", "sizeOptions",
            ):
                val = d.get(key)
                if isinstance(val, list) and val:
                    for item in val:
                        if not isinstance(item, dict):
                            continue
                        # 옵션명 조합 (optionName1 ~ optionName4)
                        parts = []
                        for n in range(1, 5):
                            p = (item.get(f"optionName{n}") or
                                 item.get(f"name{n}") or "").strip()
                            if p:
                                parts.append(p)
                        if not parts:
                            p = (item.get("optionName") or
                                 item.get("name") or
                                 item.get("title") or "").strip()
                            if p:
                                parts.append(p)
                        if not parts:
                            continue
                        opt_name = " / ".join(parts)
                        add_price = int(item.get("price", 0) or
                                        item.get("addPrice", 0) or
                                        item.get("optionPrice", 0) or 0)
                        stock = int(item.get("stockQuantity", 0) or
                                    item.get("stock", 0) or
                                    item.get("quantity", 999) or 999)
                        # 옵션 대표 이미지 추출
                        img_url = ""
                        for _ik in ("representImage", "imageUrl", "image", "img",
                                    "optionImage", "thumbnailImage"):
                            _iv = item.get(_ik)
                            if isinstance(_iv, str) and _iv.startswith("http"):
                                img_url = _iv
                                break
                            elif isinstance(_iv, dict):
                                _iv2 = _iv.get("url") or _iv.get("src") or ""
                                if _iv2.startswith("http"):
                                    img_url = _iv2
                                    break
                        results.append(NaverOption(
                            name=opt_name,
                            add_price=add_price,
                            stock=stock,
                            image_url=img_url,
                        ))
                    if results:
                        return  # 찾으면 더 이상 탐색 안 함
            # ── 재귀 탐색 ───────────────────────────────────────
            for v in d.values():
                if isinstance(v, dict):
                    _search(v, depth + 1)
                    if results:
                        return

        _search(raw)

        # 재고 0인 옵션 제거, 중복 제거
        seen: set[str] = set()
        filtered: list[NaverOption] = []
        for o in results:
            if o.stock == 0:
                continue
            if o.name in seen:
                continue
            seen.add(o.name)
            filtered.append(o)

        return filtered

    # ------------------------------------------------------------
    # 상품 노드 탐색  (동적 키 "A", "B" 등 대응)
    # ------------------------------------------------------------

    @staticmethod
    def _find_product_node(raw: dict) -> dict:
        """
        Naver __PRELOADED_STATE__ 구조 예:
          {"simpleProductForDetailPage": {"A": {"salePrice": ..., "name": ...}}}
          {"product": {"A": {"salePrice": ..., "productName": ...}}}
          {"ProductInfo": {"currentProduct": {"salePrice": ..., ...}}}
          {"props": {"pageProps": {"product": {...}}}}
        재귀 탐색으로 모든 형태를 처리한다.
        """
        SIGNALS = {"salePrice", "productName", "productNo", "name", "price"}

        def has(d: dict) -> bool:
            # SIGNAL 키가 존재하고 값이 실제로 있어야 함 (null / 0 / '' 제외)
            for k in SIGNALS:
                v = d.get(k)
                if v is not None and v != 0 and v != "":
                    return True
            return False

        # 우선순위 키 목록 (simpleProductForDetailPage 는 최상위 우선 — 실제 데이터 위치)
        PRIORITY = (
            "simpleProductForDetailPage",
            "product", "ProductInfo", "currentProduct",
            "productDetail", "catalog", "item",
            "pageProps", "props", "data",
        )
        PRIORITY_SET = set(PRIORITY)

        def search(d, depth: int) -> Optional[dict]:
            if not isinstance(d, dict) or depth > 7:
                return None
            if has(d):
                return d
            # 우선순위 키 먼저
            for k in PRIORITY:
                r = search(d.get(k), depth + 1)
                if r:
                    return r
            # 나머지 값 순회 (동적 키 "A", "B" 등 대응)
            for k, v in d.items():
                if k not in PRIORITY_SET:
                    r = search(v, depth + 1)
                    if r:
                        return r
            return None

        return search(raw, 0) or raw

    # ------------------------------------------------------------
    # 필드 추출
    # ------------------------------------------------------------

    @staticmethod
    def _get_name(node: dict) -> str:
        for k in ("name", "productName", "catalogName", "itemName", "displayName"):
            if node.get(k):
                return str(node[k]).strip()
        return "Unknown"

    @staticmethod
    def _get_price(node: dict) -> int:
        """node에서 즉시할인 적용 실제 판매가(검정색 가격)를 반환.

        우선순위:
          discountedSalePrice > sellingPrice > price > salePrice
        memberPrice/couponPrice 등 1회성 할인은 무시.
        """
        for k in ("discountedSalePrice", "sellingPrice", "price", "salePrice"):
            v = node.get(k)
            if v is not None:
                try:
                    val = int(v)
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    continue
        return 0

    @staticmethod
    def _find_discounted_price(raw: dict, sale_price: int, depth: int = 0) -> int:
        """raw_json 전체를 재귀 탐색해 판매자 즉시할인 적용가를 찾는다.

        목적:
          _get_price() 가 salePrice(정상가) 만 반환한 경우,
          다른 노드에 숨어있는 discountedSalePrice(실판매가)를 찾아 교정.

        ※ 포함하지 않는 가격:
          - customerPrice / benefitPrice : N+멤버십·쿠폰 할인가 (빨간 표시 가격)
          - lowestPrice                  : 네이버 쇼핑 최저가 비교 가격
          - immediateDiscountPrice       : 할인 금액(amount), 최종 가격 아님
          위 값들을 포함하면 멤버십 할인가(빨간색)로 잘못 수집됨.

        sale_price(정상가)보다 낮고 0보다 큰 값 중 가장 큰 것을 반환.
        못 찾으면 0 반환.
        """
        if depth > 7 or not isinstance(raw, dict):
            return 0

        # 판매자가 직접 설정한 즉시할인 최종가만 포함
        DISCOUNT_KEYS = (
            "discountedSalePrice",
            "sellingPrice",
        )
        best = 0
        for k, v in raw.items():
            if k in DISCOUNT_KEYS:
                try:
                    val = int(v)
                    if 0 < val < sale_price:
                        best = max(best, val)
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, dict):
                sub = NaverStoreCrawler._find_discounted_price(v, sale_price, depth + 1)
                if sub:
                    best = max(best, sub)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        sub = NaverStoreCrawler._find_discounted_price(item, sale_price, depth + 1)
                        if sub:
                            best = max(best, sub)
        return best

    @staticmethod
    def _get_delivery(node: dict, raw: dict) -> DeliveryInfo:
        info: dict = {}

        # ── 1) simpleProductForDetailPage (Naver __PRELOADED_STATE__ 주 경로) ──
        # 구조: simpleProductForDetailPage -> "A" / "B" (동적 키) -> productDeliveryInfo
        simple = raw.get("simpleProductForDetailPage")
        if isinstance(simple, dict):
            for variant in simple.values():
                if not isinstance(variant, dict):
                    continue
                dinfo = variant.get("productDeliveryInfo")
                if isinstance(dinfo, dict) and dinfo:
                    info = dinfo
                    break

        # ── 2) product/node 레벨 직접 탐색 (API 응답 / 기타 구조 대응) ─────────
        if not info:
            for src in (node, raw):
                for k in ("deliveryInfo", "productDeliveryInfo",
                          "deliveryFeeInfo", "deliveryLeadInfo",
                          "productDeliveryLeadInfo"):
                    c = src.get(k)
                    if isinstance(c, dict) and c:
                        info = c
                        break
                if info:
                    break

        fee_type = str(info.get("deliveryFeeType", "FREE"))

        # baseFee (__PRELOADED_STATE__) 또는 deliveryFee (Naver API 응답) 순으로 읽기
        base_fee = int(info.get("baseFee") or info.get("deliveryFee", 0) or 0)

        bundle_unit: Optional[int] = None
        bundle_fee:  Optional[int] = None

        # ── bundle 단위 탐색 공통 키 목록 ────────────────────────────
        _BUNDLE_UNIT_KEYS = (
            "deliveryFeeGroupCount", "perBundleCount", "unitQuantity",
            "quantityPerDelivery", "bundleCount", "deliveryUnitCount",
            "feeGroupCount", "groupCount", "unitCount",
        )

        print(f"[배송비파싱] fee_type={fee_type} base_fee={base_fee:,}원")
        if fee_type == "UNIT_QUANTITY_PAID":
            # 수량 단위마다 배송비 부과 (예: 12개마다 3,000원)
            raw_u = next((info.get(k) for k in _BUNDLE_UNIT_KEYS if info.get(k)), None)
            if raw_u is not None:
                bundle_unit = int(raw_u)
                bundle_fee  = base_fee
                print(f"[배송비파싱] bundle_unit JSON키 추출 성공: n={bundle_unit}")
            # 텍스트 필드에서 "N개마다" 패턴 추출
            if bundle_unit is None:
                _txt = str(info.get("differentialFeeByArea", "") or
                           info.get("feeByArea", "") or "")
                _m = re.search(r"(\d+)\s*개\s*마다", _txt)
                if _m:
                    bundle_unit = int(_m.group(1))
                    bundle_fee  = base_fee
                    print(f"[배송비파싱] bundle_unit 텍스트필드 추출 성공: n={bundle_unit}")
                else:
                    print(f"[배송비파싱] ⚠️ bundle_unit 추출 실패 — JSON키/텍스트 모두 미발견 (Playwright DOM 추출 시도 예정)")
        elif info.get("bundleDeliveryFeeYn") == "Y" or info.get("perBundleCount"):
            raw_u = info.get("perBundleCount")
            bundle_unit = int(raw_u) if raw_u is not None else None
            bundle_fee  = int(info.get("bundleDeliveryFee", 0) or 0)
            print(f"[배송비파싱] bundleDeliveryFeeYn 경로: n={bundle_unit} fee={bundle_fee}")

        # ── 폴백: raw 전체를 재귀 탐색 ───────────────────────────────
        # fee_type이 UNIT_QUANTITY_PAID가 아니더라도 JSON 어딘가에
        # bundle 단위 정보나 "N개마다" 텍스트가 있을 수 있음
        if bundle_unit is None and base_fee > 0:
            def _scan_bundle(obj, depth: int = 0) -> Optional[int]:
                if depth > 8:
                    return None
                if isinstance(obj, dict):
                    # 키 이름 직접 탐색
                    for k in _BUNDLE_UNIT_KEYS:
                        v = obj.get(k)
                        if v and str(v).isdigit() and int(v) >= 1:
                            return int(v)
                    # 문자열 값에서 "N개마다" / "N개 마다" 패턴 탐색
                    for v in obj.values():
                        if isinstance(v, str):
                            _m2 = re.search(r"(\d+)\s*개\s*마다", v)
                            if _m2:
                                return int(_m2.group(1))
                        res = _scan_bundle(v, depth + 1)
                        if res is not None:
                            return res
                elif isinstance(obj, list):
                    for item in obj:
                        res = _scan_bundle(item, depth + 1)
                        if res is not None:
                            return res
                return None

            # HTML 텍스트 힌트 우선 확인 (meta-tag fallback 시 저장됨)
            _html_hint = raw.get("_html_bundle_unit")
            if _html_hint:
                bundle_unit = int(_html_hint)
                bundle_fee  = base_fee
                if fee_type == "FREE":
                    fee_type = "PAID"
                print(f"[Crawler] bundle  : HTML 텍스트 힌트 — {bundle_unit}개마다 {bundle_fee:,}원")
            else:
                _scanned = _scan_bundle(raw)
                if _scanned is not None:
                    bundle_unit = _scanned
                    bundle_fee  = base_fee
                    if fee_type not in ("FREE",):
                        fee_type = "UNIT_QUANTITY_PAID"
                    print(f"[Crawler] bundle  : JSON 재귀탐색으로 발견 — {bundle_unit}개마다 {bundle_fee:,}원")

        return DeliveryInfo(
            base_fee=base_fee, fee_type=fee_type,
            bundle_unit=bundle_unit, bundle_fee=bundle_fee,
        )

    @staticmethod
    def _get_barcode(node: dict, raw: dict) -> str:
        """
        네이버 __PRELOADED_STATE__ 에서 바코드(GTIN/EAN) 추출.
        가능한 키를 순서대로 탐색하고, 8~14자리 숫자면 반환.
        """
        _BARCODE_KEYS = (
            "barcode", "barCode", "barcodeNumber", "ean", "gtin",
            "isbn", "upc", "catalogCode", "modelNo", "modelNumber",
        )
        _GTIN_RE = re.compile(r'^\d{8,14}$')

        def _extract(d: dict) -> str:
            for k in _BARCODE_KEYS:
                v = d.get(k)
                if v and isinstance(v, str):
                    cleaned = v.strip().replace("-", "").replace(" ", "")
                    if _GTIN_RE.match(cleaned):
                        return cleaned
                elif v and isinstance(v, (int, float)):
                    s = str(int(v))
                    if _GTIN_RE.match(s):
                        return s
            return ""

        # node 직접 탐색
        result = _extract(node)
        if result:
            return result

        # raw_json 전체에서 재귀 탐색 (최대 깊이 5)
        def _search(d, depth: int) -> str:
            if not isinstance(d, dict) or depth > 5:
                return ""
            r = _extract(d)
            if r:
                return r
            for v in d.values():
                if isinstance(v, dict):
                    r = _search(v, depth + 1)
                    if r:
                        return r
                elif isinstance(v, list):
                    for item in v:
                        r = _search(item, depth + 1)
                        if r:
                            return r
            return ""

        return _search(raw, 0)

    @staticmethod
    def _get_naver_category(node: dict, raw: dict) -> str:
        """
        Naver __PRELOADED_STATE__ 에서 상품 카테고리 전체 경로 추출.

        탐색 우선순위:
          1) wholeCategoryName  (예: "식품>사탕/캔디>기타사탕")
          2) category.wholeName / category.name
          3) searchCategory / categoryName
          4) raw 전체 재귀 탐색 (최대 깊이 5)
        """
        _CAT_KEYS = (
            "wholeCategoryName", "wholeCategoryId",
            "categoryName", "categoryPath", "searchCategory",
        )
        _CAT_OBJ_KEYS = ("category", "productCategory", "catalogCategory")

        def _from_dict(d: dict) -> str:
            # 직접 키 탐색
            for k in _CAT_KEYS:
                v = d.get(k)
                if v and isinstance(v, str) and len(v) > 1:
                    return v
            # category 오브젝트 탐색
            for k in _CAT_OBJ_KEYS:
                obj = d.get(k)
                if isinstance(obj, dict):
                    for ck in ("wholeName", "fullName", "name", "categoryName"):
                        v = obj.get(ck)
                        if v and isinstance(v, str) and len(v) > 1:
                            return v
            return ""

        # node 직접 시도
        result = _from_dict(node)
        if result:
            return result

        # raw 전체 재귀 탐색
        def _search(d, depth: int) -> str:
            if not isinstance(d, dict) or depth > 5:
                return ""
            r = _from_dict(d)
            if r:
                return r
            for v in d.values():
                if isinstance(v, dict):
                    r = _search(v, depth + 1)
                    if r:
                        return r
                elif isinstance(v, list):
                    for item in v:
                        r = _search(item if isinstance(item, dict) else {}, depth + 1)
                        if r:
                            return r
            return ""

        return _search(raw, 0)

    @staticmethod
    def _get_brand(node: dict, raw: dict) -> str:
        """
        네이버 __PRELOADED_STATE__ 에서 브랜드명 추출.

        탐색 우선순위:
          1) node 직접 키 (brandName, brand, maker, manufacturer)
          2) raw 전체 재귀 탐색
        """
        _BRAND_KEYS = (
            "brandName", "brand", "brandInfo",
            "maker", "manufacturer", "makerName",
            "manufacturerName", "productBrand",
        )

        def _from_dict(d: dict) -> str:
            for k in _BRAND_KEYS:
                v = d.get(k)
                if isinstance(v, str) and v.strip() and v.strip() not in ("", "없음", "해당없음"):
                    return v.strip()
                if isinstance(v, dict):
                    inner = v.get("name") or v.get("brandName") or ""
                    if inner and isinstance(inner, str):
                        return inner.strip()
            return ""

        result = _from_dict(node)
        if result:
            return result

        def _search(d, depth: int) -> str:
            if not isinstance(d, dict) or depth > 5:
                return ""
            r = _from_dict(d)
            if r:
                return r
            for v in d.values():
                if isinstance(v, dict):
                    r = _search(v, depth + 1)
                    if r:
                        return r
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            r = _search(item, depth + 1)
                            if r:
                                return r
            return ""

        return _search(raw, 0)

    @staticmethod
    def _get_image(node: dict) -> str:
        _IMG_KEYS = (
            "representativeImageUrl", "mainImageUrl", "imageUrl",
            "productImageUrl", "representImageUrl", "thumbnailImageUrl",
            "originalImageUrl", "channelProductImageUrl",
        )
        candidates: list = [node.get(k) for k in _IMG_KEYS]

        for lk in ("images", "productImages", "imageList"):
            items = node.get(lk) or []
            if isinstance(items, list) and items:
                first = items[0]
                candidates.append(
                    first if isinstance(first, str)
                    else (first.get("imageUrl") or first.get("url") or first.get("src") or "")
                )
            elif isinstance(items, dict):
                candidates.append(items.get("url") or items.get("imageUrl") or "")

        # productImage 오브젝트 대응
        pi = node.get("productImage")
        if isinstance(pi, str):
            candidates.append(pi)
        elif isinstance(pi, dict):
            candidates.append(pi.get("url") or pi.get("imageUrl") or "")

        def _apply_type(u: str) -> str:
            u = re.sub(r"type=\w+", "type=f640_640", u)
            if "type=" not in u:
                u += ("&" if "?" in u else "?") + "type=f640_640"
            return u

        for url in candidates:
            if url and isinstance(url, str) and url.startswith("http"):
                return _apply_type(url)

        # 노드 내 재귀 탐색 (brand store API 응답 대응)
        def _search(d, depth=0):
            if depth > 5 or not isinstance(d, dict):
                return ""
            for k in _IMG_KEYS:
                v = d.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in d.values():
                if isinstance(v, dict):
                    r = _search(v, depth + 1)
                    if r:
                        return r
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            r = _search(item, depth + 1)
                            if r:
                                return r
            return ""

        found = _search(node)
        if found:
            return _apply_type(found)
        return ""

    # ------------------------------------------------------------
    # 상세페이지 이미지 수집
    # ------------------------------------------------------------

    @staticmethod
    def _extract_detail_images_from_raw(raw: dict) -> list[str]:
        """
        raw_json에서 상품 상세페이지 이미지 URL을 추출한다.
        contentHtml / content 등 HTML 문자열 필드에서 <img src> 파싱.
        최대 30개 반환.
        """
        _CONTENT_KEYS = (
            "contentHtml", "contentsHtml", "content", "contents",
            "detailContents", "htmlContents", "detailHtml",
            "productContents", "productDescription",
            "detailContent", "catalogContent", "itemContents",
        )
        # 이미지 URL 배열을 담는 키
        _IMG_ARRAY_KEYS = (
            "detailImages", "detailImage", "catalogImages",
            "productImages", "imageList", "images",
            "descriptionImages", "contentsImages",
        )
        _IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)
        _URL_RE = re.compile(
            r'https?://[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s"\'<>\\]*)?',
            re.IGNORECASE
        )

        def _imgs_from_html(html_str: str) -> list[str]:
            if not html_str or not isinstance(html_str, str):
                return []
            found = []
            seen: set[str] = set()
            for m in _IMG_RE.finditer(html_str):
                src = m.group(1).strip()
                if src.startswith("http") and src not in seen:
                    seen.add(src)
                    found.append(src)
            # img 태그 없으면 bare URL 도 시도
            if not found:
                for m in _URL_RE.finditer(html_str):
                    src = m.group(0).rstrip("\"',;)}")
                    if src not in seen:
                        seen.add(src)
                        found.append(src)
            return found

        def _imgs_from_array(arr: list) -> list[str]:
            """이미지 URL 배열 또는 {imageUrl/url/src} 객체 배열 처리."""
            result = []
            seen: set[str] = set()
            for item in arr:
                if isinstance(item, str) and item.startswith("http"):
                    if item not in seen:
                        seen.add(item)
                        result.append(item)
                elif isinstance(item, dict):
                    for fk in ("imageUrl", "url", "src", "imgUrl", "path"):
                        v = item.get(fk, "")
                        if isinstance(v, str) and v.startswith("http") and v not in seen:
                            seen.add(v)
                            result.append(v)
                            break
            return result

        def _search(d: dict, depth: int) -> list[str]:
            if depth > 10 or not isinstance(d, dict):
                return []
            result: list[str] = []
            for k, v in d.items():
                if k in _CONTENT_KEYS:
                    if isinstance(v, str):
                        result.extend(_imgs_from_html(v))
                    elif isinstance(v, dict):
                        for sv in v.values():
                            result.extend(_imgs_from_html(sv) if isinstance(sv, str) else [])
                elif k in _IMG_ARRAY_KEYS:
                    if isinstance(v, list):
                        result.extend(_imgs_from_array(v))
                elif isinstance(v, dict):
                    result.extend(_search(v, depth + 1))
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            result.extend(_search(item, depth + 1))
            return result

        all_urls = _search(raw, 0)
        seen: set[str] = set()
        unique: list[str] = []
        for u in all_urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        return unique[:30]

    def _fetch_detail_html_direct(self, pid: str, store: str) -> Optional[str]:
        """
        네이버 상품 상세 HTML을 전용 엔드포인트에서 직접 획득.
        __PRELOADED_STATE__ 에는 detailContents 가 포함되지 않으므로
        별도 API / 채널상품 endpoint 를 순서대로 시도한다.
        """
        import time

        detail_endpoints = [
            f"https://smartstore.naver.com/main/v1/products/no/{pid}/detail-contents",
            f"https://smartstore.naver.com/main/v1/products/{pid}/detail-contents",
            f"https://smartstore.naver.com/i/v1/products/{pid}/detail-contents",
            f"https://smartstore.naver.com/main/v1/stores/{store}/products/{pid}/detail-contents",
        ]

        _headers_json = {**self._HEADERS, "Accept": "application/json, */*"}

        # ── BrightData Web Unlocker ──────────────────────────────────
        if self.api_key:
            for ep in detail_endpoints:
                txt = self._web_unlocker_fetch(ep, accept_json=True)
                if txt and len(txt) > 100:
                    print(f"[Crawler] detail-contents WU OK: {ep[-70:]}")
                    return txt
                # HTML 페이지 응답이면 img src 직접 추출 가능
                if txt and "<img" in txt:
                    return txt

        # ── curl_cffi 직접 연결 ──────────────────────────────────────
        try:
            from curl_cffi.requests import Session as CfSession  # type: ignore
            cf_sess = CfSession(impersonate="chrome124")
            cf_sess.verify = False
        except Exception:
            cf_sess = None

        _proxy_kw: dict = (
            {"proxies": {"http": self.proxy_url, "https": self.proxy_url}}
            if self.proxy_url else {}
        )

        for ep in detail_endpoints:
            try:
                getter = cf_sess or self.session
                resp = getter.get(ep, headers=_headers_json, timeout=15, **_proxy_kw)
                if resp.status_code == 200 and len(resp.text) > 100:
                    print(f"[Crawler] detail-contents cffi OK: {ep[-70:]}")
                    return resp.text
            except Exception as e:
                print(f"[Crawler] detail-contents cffi error {ep[-50:]}: {e}")
            time.sleep(0.3)

        return None

    @staticmethod
    def _extract_imgs_from_json_or_html(text: str) -> list[str]:
        """JSON 또는 HTML 문자열에서 이미지 URL 추출.
        <img src>, data-src, data-lazy-src, data-original 등 lazy-load 속성 모두 포함.
        """
        # img 태그 내 모든 속성에서 http(s) URL 추출 (src / data-src / data-original 등)
        _IMG_TAG_RE = re.compile(r'<img[^>]+>', re.IGNORECASE | re.DOTALL)
        _ATTR_URL_RE = re.compile(
            r'(?:src|data-src|data-lazy-src|data-original|data-url|data-image)[=\s]*["\']'
            r'(https?://[^"\']+)["\']',
            re.IGNORECASE
        )
        # bare URL fallback
        _URL_RE = re.compile(
            r'https?://[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s"\'<>\\]*)?',
            re.IGNORECASE
        )
        seen: set[str] = set()
        result: list[str] = []

        # 1) <img ...> 태그에서 src/data-src 계열 속성 추출
        for tag_m in _IMG_TAG_RE.finditer(text):
            tag = tag_m.group(0)
            for attr_m in _ATTR_URL_RE.finditer(tag):
                src = attr_m.group(1).strip()
                if src not in seen:
                    seen.add(src)
                    result.append(src)

        # 2) img 태그 밖에도 bare URL 있으면 추가 (JSON 응답)
        for m in _URL_RE.finditer(text):
            src = m.group(0).strip().rstrip("\"',;)}")
            if src not in seen:
                seen.add(src)
                result.append(src)

        return result

    def _fetch_product_page_html(self, url: str) -> Optional[str]:
        """상품 페이지 원문 HTML을 반환 (파싱 없이 raw text)."""
        clean = url.split("?")[0]
        _proxy_kw: dict = (
            {"proxies": {"http": self.proxy_url, "https": self.proxy_url}}
            if self.proxy_url else {}
        )

        # BrightData Web Unlocker
        if self.api_key:
            html = self._web_unlocker_fetch(clean)
            if html and len(html) > 5000:
                print(f"[Crawler] product page HTML (WU): len={len(html)}")
                return html

        # curl_cffi fallback
        try:
            from curl_cffi.requests import Session as CfSession  # type: ignore
            cf_sess = CfSession(impersonate="chrome124")
            cf_sess.verify = False
            resp = cf_sess.get(clean, headers=self._CFFI_EXTRA, timeout=20, **_proxy_kw)
            if resp.status_code == 200 and len(resp.text) > 5000:
                print(f"[Crawler] product page HTML (cffi): len={len(resp.text)}")
                return resp.text
        except Exception as e:
            print(f"[Crawler] product page HTML error: {e}")

        return None

    @staticmethod
    def _extract_detail_imgs_from_page_html(html: str) -> list[str]:
        """
        상품 페이지 원문 HTML에서 실제 상품 상세이미지만 추출.

        네이버 상세이미지는 아래 두 가지로 저장됨:
          A) shop-phinf.pstatic.net  — 상품상세 전용 CDN
          B) __PRELOADED_STATE__ 안에 JSON-escaped 형태로 포함된 <img> URL

        필터 기준:
          - shop-phinf.pstatic.net 또는 simg.pstatic.net 도메인
          - 아이콘 / 뱃지 추정 이미지 제외 (width/height ≤ 60 or url에 icon/logo 포함)
        """
        import html as _html_mod

        # JSON 이스케이프 해제 (\\u003c → <, \" → ")
        unescaped = _html_mod.unescape(html)
        # JSON 문자열 안의 \\/ → /
        unescaped = unescaped.replace("\\/", "/")

        _CDN_DOMAINS = ("shop-phinf.pstatic.net", "simg.pstatic.net",
                        "shopping-phinf.pstatic.net")

        # data-src / src / data-original 포함 모든 이미지 속성 추출
        _ALL_IMG_RE = re.compile(
            r'(?:src|data-src|data-lazy-src|data-original|data-url)[=\s]*["\']'
            r'(https?://[^"\']+)["\']',
            re.IGNORECASE
        )
        # bare URL (JSON 문자열 안에 있는 경우)
        _BARE_URL_RE = re.compile(
            r'https?://(?:' + '|'.join(d.replace('.', r'\.') for d in _CDN_DOMAINS) + r')'
            r'/[^\s"\'<>\\]+\.(?:jpg|jpeg|png|webp)(?:[?][^\s"\'<>\\]*)?',
            re.IGNORECASE
        )

        seen: set[str] = set()
        result: list[str] = []

        def _add(src: str):
            src = src.strip().rstrip("\"',;)}")
            if src in seen:
                return
            # CDN 도메인 필터
            if not any(d in src for d in _CDN_DOMAINS):
                return
            # 아이콘/로고 의심 URL 제거
            low = src.lower()
            if any(x in low for x in ("/icon", "/logo", "/badge", "/btn", "/arrow",
                                        "16x16", "32x32", "14x14")):
                return
            seen.add(src)
            result.append(src)

        for m in _ALL_IMG_RE.finditer(unescaped):
            _add(m.group(1))

        for m in _BARE_URL_RE.finditer(unescaped):
            _add(m.group(0))

        return result[:40]

    @staticmethod
    def _extract_channel_product_no(html: str) -> Optional[str]:
        """
        상품 페이지 HTML에서 channelProductNo 추출.
        네이버 __PRELOADED_STATE__:
          simpleProductForDetailPage: { "12345678": { detailContents: ... } }
          여기서 "12345678" 이 channelProductNo.
        """
        patterns = [
            r'"channelProductNo"\s*:\s*"?(\d{8,})"?',
            r'"channelNo"\s*:\s*"?(\d{8,})"?',
            r'channelProductNo["\s:=]+(\d{8,})',
            # simpleProductForDetailPage 아래 첫 번째 숫자 키
            r'"simpleProductForDetailPage"\s*:\s*\{"(\d{8,})"',
        ]
        for pat in patterns:
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    @staticmethod
    def _extract_detail_contents_from_preloaded(html: str) -> Optional[str]:
        """
        __PRELOADED_STATE__ JSON 안에서 detailContents HTML 직접 추출.
        simpleProductForDetailPage.{channelProductNo}.detailContents 구조 처리.
        """
        import html as _html_mod

        # simpleProductForDetailPage 블록 추출 (최대 500KB)
        m = re.search(r'"simpleProductForDetailPage"\s*:\s*(\{)', html)
        if not m:
            return None

        start = m.start(1)
        depth = 0
        end = start
        for i, ch in enumerate(html[start:start + 600_000]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = start + i + 1
                    break

        block = html[start:end]
        if not block:
            return None

        # detailContents 필드 찾기 (JSON escaped string)
        mc = re.search(r'"detailContents"\s*:\s*"((?:[^"\\]|\\.)*)"', block)
        if not mc:
            return None

        raw_str = mc.group(1)
        # JSON 이스케이프 해제: json.loads로 정확하게 처리 (한글 깨짐 방지)
        try:
            detail_html = json.loads(f'"{raw_str}"')
        except Exception:
            try:
                detail_html = raw_str.encode('raw_unicode_escape').decode('unicode_escape')
            except Exception:
                detail_html = raw_str
        detail_html = _html_mod.unescape(detail_html).replace("\\/", "/")
        print(f"[Crawler] detailContents from preloaded: len={len(detail_html)}")
        return detail_html if len(detail_html) > 100 else None

    def _fetch_detail_content_json(self, channel_no: str, pid: str) -> Optional[str]:
        """
        channelProductNo를 이용해 네이버 상세콘텐츠 JSON API 호출.
        반환값: detailContents HTML 문자열 (없으면 None).
        """
        import time

        # 네이버 채널상품 API 엔드포인트
        endpoints = [
            f"https://smartstore.naver.com/i/v1/products/{channel_no}",
            f"https://smartstore.naver.com/main/v1/products/no/{pid}",
        ]

        _headers_json = {**self._HEADERS, "Accept": "application/json, */*"}
        _proxy_kw: dict = (
            {"proxies": {"http": self.proxy_url, "https": self.proxy_url}}
            if self.proxy_url else {}
        )

        def _parse_response(text: str, label: str) -> Optional[str]:
            """JSON 또는 HTML 응답에서 detailContents HTML 추출."""
            if not text or len(text) < 200:
                return None

            # 1) JSON 응답 시도
            try:
                data = json.loads(text)

                def _search(d, depth=0):
                    if depth > 8 or not isinstance(d, dict):
                        return None
                    for k in ("detailContents", "detailContent", "contents", "content",
                              "htmlContents", "contentHtml", "catalogContents"):
                        v = d.get(k)
                        if isinstance(v, str) and len(v) > 200:
                            return v
                    for v in d.values():
                        r = _search(v, depth + 1) if isinstance(v, dict) else None
                        if r:
                            return r
                        if isinstance(v, list):
                            for item in v:
                                r = _search(item, depth + 1) if isinstance(item, dict) else None
                                if r:
                                    return r
                    return None

                result = _search(data)
                if result:
                    print(f"[Crawler] detail (JSON {label}): len={len(result)}")
                    return result
            except Exception:
                pass

            # 2) HTML 응답 시 — __PRELOADED_STATE__ 에서 detailContents 추출
            if "<html" in text[:500].lower() or "PRELOADED_STATE" in text:
                result = NaverStoreCrawler._extract_detail_contents_from_preloaded(text)
                if result:
                    print(f"[Crawler] detail (HTML-preloaded {label}): len={len(result)}")
                    return result

            return None

        # ── curl_cffi 직접 연결 (BrightData 우회 — 429 방지) ──────────────
        try:
            from curl_cffi.requests import Session as CfSession  # type: ignore
            cf_sess = CfSession(impersonate="chrome124")
            cf_sess.verify = False
            for ep in endpoints:
                try:
                    resp = cf_sess.get(ep, headers=_headers_json, timeout=20, **_proxy_kw)
                    if resp.status_code == 200:
                        result = _parse_response(resp.text, f"cffi {ep[-40:]}")
                        if result:
                            return result
                except Exception as e:
                    print(f"[Crawler] cffi {ep[-50:]}: {e}")
                time.sleep(0.5)
        except ImportError:
            pass

        # ── BrightData Web Unlocker (cffi 실패 시) ────────────────────────
        if self.api_key:
            for ep in endpoints:
                txt = self._web_unlocker_fetch(ep, accept_json=True)
                if txt:
                    result = _parse_response(txt, f"WU {ep[-40:]}")
                    if result:
                        return result
                time.sleep(0.5)

        return None

    async def fetch_detail_images(self, url: str) -> list[str]:
        """
        네이버 스마트스토어 상품의 상세페이지 이미지 URL 목록을 반환.
        단일등록 탭에서 사용자 이미지 선택 전에 호출된다.

        전략:
          1) 상품 페이지 HTML → channelProductNo 추출 → 상세 JSON API 호출
             → detailContents HTML에서 shop-phinf CDN 이미지 파싱
          2) PRELOADED_STATE raw_json 재귀 탐색 (fallback)
        """
        loop = asyncio.get_running_loop()
        store, pid = self._parse_url(url)

        # ── 전략 1: 상품 페이지 HTML → channelProductNo → detail API ───
        page_html = await loop.run_in_executor(None, self._fetch_product_page_html, url)

        if page_html:
            # ── 전략 1a: __PRELOADED_STATE__ 안의 detailContents 직접 추출 ──
            detail_html_pre = self._extract_detail_contents_from_preloaded(page_html)
            if detail_html_pre:
                imgs = self._extract_detail_imgs_from_page_html(detail_html_pre)
                if imgs:
                    print(f"[Crawler] fetch_detail_images (preloaded-detail): {len(imgs)}개")
                    return imgs

            # ── 전략 1b: channelProductNo → 상세 JSON API ────────────────
            channel_no = self._extract_channel_product_no(page_html)
            print(f"[Crawler] channelProductNo: {channel_no}  (productNo: {pid})")

            if channel_no:
                detail_html = await loop.run_in_executor(
                    None, self._fetch_detail_content_json, channel_no, pid
                )
                if detail_html:
                    imgs = self._extract_detail_imgs_from_page_html(detail_html)
                    if imgs:
                        print(f"[Crawler] fetch_detail_images (detail-API): {len(imgs)}개")
                        return imgs

        # ── 전략 2: PRELOADED_STATE raw_json ────────────────────────────
        raw = await loop.run_in_executor(None, self._try_requests, url)
        if raw is None:
            raw = await loop.run_in_executor(None, self._try_api, pid, store)

        if raw is not None:
            imgs = self._extract_detail_images_from_raw(raw)
            if imgs:
                print(f"[Crawler] fetch_detail_images (raw): {len(imgs)}개")
                return imgs

        print(f"[Crawler] fetch_detail_images: 이미지 없음 ({url[:60]})")
        return []

    # ------------------------------------------------------------
    # 이미지 다운로드
    # ------------------------------------------------------------

    async def _download_image(self, image_url: str, product_id: str) -> Optional[str]:
        if not image_url:
            print("[Crawler] no image URL")
            return None
        save_dir  = Path(self.settings.IMAGE_ORIGINAL_DIR)
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f"{product_id}.jpg"

        # 동기 IO → executor 에서 실행해 이벤트루프 블로킹 방지
        def _do_download() -> str:
            resp = self.session.get(image_url, timeout=20, stream=True)
            resp.raise_for_status()
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return str(save_path)

        loop = asyncio.get_running_loop()
        try:
            path = await loop.run_in_executor(None, _do_download)
            print(f"[Crawler] image saved: {path}")
            return path
        except Exception as e:
            print(f"[Crawler] image download failed: {e}")
            return None
