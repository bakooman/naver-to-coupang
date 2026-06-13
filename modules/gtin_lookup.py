"""
상품 바코드(GTIN/EAN) 자동 조회 모듈

조회 순서:
  1. UPC Item DB (upcitemdb.com) — 무료 공개 API, 국제 유통 제품 특히 강함
     일 100회 무료

  2. Open Food Facts — 식품류 전문 오픈 DB

  3. Naver Shopping 검색 — 한국 내수 상품 바코드 추출 (ZIC, S-OIL, Kixx 등)
     Naver 쇼핑 카탈로그 API: search.shopping.naver.com
     → 상품 상세 JSON에서 barcode / catalogBarcode 필드 추출

  4. barcodelookup.com 웹 검색 — 국제 fallback
     HTML 파싱으로 EAN/UPC 추출

실패 시 "" 반환 → Excel 바코드 컬럼 공란 (의미없는 번호보다 공란이 안전)

Wing 바코드 컬럼 요구사항:
  - 8~14자리 숫자 (EAN-8, EAN-13, UPC-A, GTIN-14)
  - Luhn 체크디짓 검증 통과 필요

[GS1 Korea 조회 불가 이유]
  gs1kr.org 검색은 로그인 + CAPTCHA 필요 → 자동화 불가
  → Naver Shopping 카탈로그가 한국 내수 바코드를 가장 많이 보유
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import requests

# curl_cffi: Naver 봇 차단 우회용 (선택적 — 없어도 다른 소스 사용)
try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_OK = True
except ImportError:
    _cffi_requests = None
    _CFFI_OK = False

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_GTIN_RE = re.compile(r'^\d{8,14}$')

# ── 한글 브랜드/키워드 → 영문 변환 테이블 (국제 DB 검색용) ──────────
# 한글로만 표기된 수입 브랜드명/상품명을 영문으로 변환해 UPC Item DB 등에서 검색
_KR_EN_MAP: dict[str, str] = {
    # 식품 브랜드
    "켈로그": "Kellogg",
    "팝타르트": "Pop-Tarts",
    "스모어": "Smores",
    "하리보": "Haribo",
    "오레오": "Oreo",
    "너즈": "Nerds",
    "프링글스": "Pringles",
    "도리토스": "Doritos",
    "레이즈": "Lays",
    "스니커즈": "Snickers",
    "허쉬": "Hershey",
    "킷캣": "KitKat",
    "엠앤엠": "M&Ms",
    "마스": "Mars",
    "트윅스": "Twix",
    "리즈": "Reeses",
    "네슬레": "Nestle",
    "크래프트": "Kraft",
    "하인즈": "Heinz",
    "캠벨": "Campbell",
    "퀘이커": "Quaker",
    "제너럴밀스": "General Mills",
    "레드불": "Red Bull",
    "몬스터": "Monster Energy",
    "게토레이": "Gatorade",
    "코카콜라": "Coca-Cola",
    "펩시": "Pepsi",
    "스프라이트": "Sprite",
    "닥터페퍼": "Dr Pepper",
    "마운틴듀": "Mountain Dew",
    # 생활/뷰티 브랜드
    "도브": "Dove",
    "질레트": "Gillette",
    "타이드": "Tide",
    "다우니": "Downy",
    "팸퍼스": "Pampers",
    "뉴트로지나": "Neutrogena",
    "바세린": "Vaseline",
    "니베아": "Nivea",
    "콜게이트": "Colgate",
    "리스테린": "Listerine",
    "오랄비": "Oral-B",
    "헤드앤숄더": "Head Shoulders",
    "팬틴": "Pantene",
    # 과자/제과 브랜드
    "딕만스": "Dickmanns",
    "밀카": "Milka",
    "토블론": "Toblerone",
    "페레로": "Ferrero",
    "로쉐": "Rocher",
    "킨더": "Kinder",
    "린트": "Lindt",
    "고디바": "Godiva",
    "발로나": "Valrhona",
    "리터": "Ritter Sport",
    "리터스포트": "Ritter Sport",
    "버터핑거": "Butterfinger",
    "베이비루스": "Baby Ruth",
    "웰치스": "Welchs",
    "스키틀즈": "Skittles",
    "스타버스트": "Starburst",
    "워터스": "Walkers",
    "맥비티": "McVities",
    "아이다호": "Idaho",
    "그래놀라": "granola",
    # 상품 키워드
    "초코": "chocolate",
    "초콜릿": "chocolate",
    "마시멜로": "marshmallow",
    "마시멜로우": "marshmallow",
    "쿠키": "cookies",
    "젤리": "gummy",
    "캔디": "candy",
    "비타민": "vitamin",
    "프로틴": "protein",
    "샴푸": "shampoo",
    "세제": "detergent",
    "스낵": "snack",
    "크래커": "crackers",
    "웨이퍼": "wafer",
    "케이크": "cake",
    "파이": "pie",
    "시리얼": "cereal",
    "오트밀": "oatmeal",
    "그래놀라바": "granola bar",
    "너트": "nuts",
    "아몬드": "almond",
    "캐슈": "cashew",
    "피스타치오": "pistachio",
    "건과류": "dried fruit",
    "건포도": "raisins",
    "팝콘": "popcorn",
    "칩": "chips",
    "구미": "gummy",
    "소다": "soda",
    "탄산": "sparkling",
    "주스": "juice",
    "아이스크림": "ice cream",
    "샌드위치": "sandwich",
    "버거": "burger",
}


# ── 유효성 검증 ────────────────────────────────────────────────────

def _is_valid_gtin(code: str) -> bool:
    """8~14자리 숫자 + Luhn 체크디짓 검증."""
    code = str(code).strip().replace("-", "").replace(" ", "")
    if not _GTIN_RE.match(code):
        return False
    padded = code.zfill(14)
    total = 0
    for i, ch in enumerate(padded[:-1]):
        n = int(ch)
        total += n * 3 if i % 2 == 0 else n
    check = (10 - (total % 10)) % 10
    return check == int(padded[-1])


def _normalize_gtin(code: str) -> str:
    """EAN-13 기준으로 정규화 (앞 0 제거하지 않음)."""
    return str(code).strip().replace("-", "").replace(" ", "")


# ── 검색 쿼리 생성 ─────────────────────────────────────────────────

def _build_query(product_name: str, brand: str = "") -> str:
    """
    상품명 + 브랜드 → 검색 쿼리 생성.
    한글 제거 + 핵심 영문/숫자 토큰만 남김 (국제 DB 검색 최적화).
    브랜드가 상품명에 이미 포함되어 있으면 중복 추가 안 함.
    """
    # ── 상품명 전처리: 쉼표 뒤 단위/수량 부분 먼저 제거 (예: ", 96g, 1개" 제거)
    # "일본 롯데 샤샤 초콜릿, 69g, 1개" → "일본 롯데 샤샤 초콜릿"
    name_cleaned = re.split(r',\s*\d', product_name)[0].strip()

    # 한글 제거 전에 단위·수량 먼저 제거 (한글 단위가 포함된 숫자 처리)
    # 예: "10개입" → "" (개·입이 한글이므로 한글 제거 전 처리)
    name_cleaned = re.sub(r'\d+\s*[개입팩봉캔병박스]', ' ', name_cleaned)
    # 한글 제거 (국제 DB는 영문 기준)
    name_en = re.sub(r'[가-힣]+', ' ', name_cleaned)
    # 남은 단위·수량 제거 (영문 단위)
    name_en = re.sub(r'\d+\s*(L|ml|cc|kg|g)\b', ' ', name_en, flags=re.I)
    # 특수문자 → 공백
    name_en = re.sub(r'[^\w\s]', ' ', name_en)
    # 순수 숫자 토큰 제거 (한글 단위 떨어진 수량 숫자, 예: "10", "5")
    tokens = [t for t in name_en.split() if len(t) >= 2 and not t.isdigit()]

    # 영문 수량 단위 토큰 제거 (예: "24EA", "12PCS", "6CT", "16OZ" → 제거)
    # 한글 제거 후에도 남는 수량 표기를 정리
    tokens = [
        t for t in tokens
        if not re.match(r'^\d*\s*(EA|PCS|PC|CT|CTK|OZ|FL|LB|LBS|ML|QT|GAL|BOX|SET|PACK|PKG)$', t, re.I)
    ]

    # 브랜드 (한글 제거)
    brand_en = re.sub(r'[가-힣]+', '', brand).strip()

    # 브랜드가 이미 상품명에 포함되어 있으면 중복 추가 안 함
    brand_lower = brand_en.lower()
    name_joined_lower = " ".join(tokens).lower()
    already_in_name = bool(brand_lower) and (brand_lower in name_joined_lower)

    # ── 한글 전용 상품명 fallback: 영문 토큰이 0개일 때 _KR_EN_MAP 번역 시도 ──
    # 예) "켈로그 팝타르트 스모어" → ["Kellogg", "Pop-Tarts", "Smores"]
    if not tokens:
        for word in name_cleaned.split():
            word_s = word.strip()
            mapped = _KR_EN_MAP.get(word_s)
            if not mapped:
                # 부분 매칭 (예: "팝타르트141g" → "Pop-Tarts")
                for kr, en in _KR_EN_MAP.items():
                    if kr in word_s:
                        mapped = en
                        break
            if mapped:
                tokens.append(mapped)

    # ── 한글 브랜드 → 영문 변환 fallback ──
    if not brand_en and brand:
        brand_clean = brand.strip()
        brand_en = _KR_EN_MAP.get(brand_clean, "")
        if not brand_en:
            for kr, en in _KR_EN_MAP.items():
                if kr in brand_clean:
                    brand_en = en
                    break

    # 브랜드가 이미 포함 여부 재판단 (번역 후)
    name_joined_lower_final = " ".join(tokens).lower()
    brand_lower_final = brand_en.lower()
    already_in_name = bool(brand_lower_final) and (brand_lower_final in name_joined_lower_final)

    final_tokens = []
    if brand_en and not already_in_name:
        final_tokens.append(brand_en)
    final_tokens.extend(tokens[:5])   # 상품명 앞 5토큰

    query = " ".join(final_tokens)[:80].strip()
    return query


# ── Source 1: UPC Item DB ──────────────────────────────────────────

def _relevance_check(query: str, title: str, min_shared: int = 1) -> bool:
    """
    검색 쿼리와 반환된 상품 제목 간 관련성 검증.

    쿼리 토큰 중 최소 min_shared 개 이상이 제목에 포함되어야 함.
    단, 쿼리가 너무 짧거나(2자 이하 단일 토큰) 브랜드만 있는 경우는
    오탐 방지를 위해 더 엄격하게 검사.
    """
    q_tokens = set(re.sub(r'[^\w]', ' ', query.lower()).split())
    t_tokens = set(re.sub(r'[^\w]', ' ', title.lower()).split())

    # 숫자 토큰(용량, 규격 등)은 공유 카운트에서 제외 (너무 일반적)
    q_tokens = {t for t in q_tokens if not t.isdigit() and len(t) >= 2}

    if not q_tokens:
        return True   # 쿼리 토큰이 없으면 통과 (판단 불가)

    shared = q_tokens & t_tokens
    # 쿼리가 단일 짧은 토큰이면 제목에 반드시 포함 필요
    if len(q_tokens) == 1:
        return bool(shared)

    return len(shared) >= min_shared


def _lookup_upcitemdb(query: str, timeout: int = 8) -> str:
    """
    UPC Item DB 무료 API로 상품명 → GTIN 역조회.
    https://www.upcitemdb.com/api/explorer#!/lookup/get_trial_search
    """
    try:
        resp = requests.get(
            "https://api.upcitemdb.com/prod/trial/search",
            params={"s": query},
            headers=_HEADERS,
            timeout=timeout,
        )
        if resp.status_code == 429:
            print("[GTIN] UPC Item DB 일일 한도 초과 (100회/일)")
            return ""
        if resp.status_code == 404:
            # 404 = 검색 결과 없음 (API 비표준 동작 — 오류 아님)
            print(f"[GTIN] UPC Item DB 결과 없음: '{query}'")
            return ""
        resp.raise_for_status()
        data = resp.json()
    except Exception as ex:
        print(f"[GTIN] UPC Item DB 요청 실패: {ex}")
        return ""

    items = data.get("items", [])
    for item in items:
        title = item.get("title", "")
        # 관련성 검증: 반환 상품명이 쿼리와 무관하면 스킵
        if not _relevance_check(query, title):
            print(f"[GTIN] UPC Item DB 관련성 낮음 → 스킵: '{title[:50]}'")
            continue
        # EAN 우선, 없으면 UPC
        for field in ("ean", "upc"):
            raw = str(item.get(field, "")).strip()
            if raw and _is_valid_gtin(_normalize_gtin(raw)):
                gtin = _normalize_gtin(raw)
                print(f"[GTIN] UPC Item DB 성공: '{query}' → {gtin} ({title[:40]})")
                return gtin

    print(f"[GTIN] UPC Item DB 결과 없음: '{query}'")
    return ""


# ── Source 2: Open Food Facts (식품류 fallback) ────────────────────

def _lookup_openfoodfacts(query: str, timeout: int = 8) -> str:
    """Open Food Facts — 식품류 전용 오픈 DB."""
    try:
        resp = requests.get(
            "https://world.openfoodfacts.org/cgi/search.pl",
            params={
                "search_terms": query,
                "search_simple": 1,
                "action": "process",
                "json": 1,
                "page_size": 5,
            },
            headers=_HEADERS,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as ex:
        print(f"[GTIN] Open Food Facts 요청 실패: {ex}")
        return ""

    for product in data.get("products", []):
        raw = str(product.get("code", "")).strip()
        if raw and _is_valid_gtin(_normalize_gtin(raw)):
            name = product.get("product_name", "")
            if not _relevance_check(query, name):
                print(f"[GTIN] Open Food Facts 관련성 낮음 → 스킵: '{name[:50]}'")
                continue
            gtin = _normalize_gtin(raw)
            print(f"[GTIN] Open Food Facts 성공: '{query}' → {gtin} ({name[:40]})")
            return gtin

    print(f"[GTIN] Open Food Facts 결과 없음: '{query}'")
    return ""


# ── Source 3: Naver Shopping 카탈로그 (한국 내수 브랜드) ──────────

_NAVER_SHOP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://search.shopping.naver.com/",
}

# 바코드 형식 패턴 (HTML 내 텍스트 추출용)
_BARCODE_TEXT_RE = re.compile(r'\b(\d{8,14})\b')


def _lookup_naver_shopping(query: str, timeout: int = 10) -> str:
    """
    Naver Shopping 검색 → 상품 상세 JSON에서 바코드 추출.

    한국 내수 브랜드(ZIC, S-OIL, Kixx 등)는 국제 DB에 없지만
    Naver 쇼핑 카탈로그에는 바코드 정보가 포함된 경우가 있음.

    전략:
      1) Naver 쇼핑 검색 API → 상위 카탈로그 ID 추출
      2) 카탈로그 상세 API → barcode / catalogBarcode 필드 추출
    """
    # ── Step 1: 쇼핑 검색 ──────────────────────────────────────────
    # curl_cffi 있으면 봇 차단 우회, 없으면 일반 requests (418 반환 가능)
    try:
        _req = _cffi_requests if _CFFI_OK else requests
        _kw  = {"impersonate": "chrome131"} if _CFFI_OK else {}
        search_resp = _req.get(
            "https://search.shopping.naver.com/api/search",
            params={
                "query": query,
                "sort":  "rel",
                "page":  1,
                "pageSize": 5,
                "viewType": "list",
            },
            headers=_NAVER_SHOP_HEADERS,
            timeout=timeout,
            **_kw,
        )
        if search_resp.status_code == 418:
            if not _CFFI_OK:
                print("[GTIN] Naver Shopping 봇 차단 (418) — curl_cffi 없음. pip install curl-cffi 설치 권장")
            return ""
        if search_resp.status_code != 200:
            return _lookup_naver_shopping_html(query, timeout)
        data = search_resp.json()
    except Exception as ex:
        print(f"[GTIN] Naver Shopping API 실패: {ex}")
        return _lookup_naver_shopping_html(query, timeout)

    # ── Step 2: 결과에서 바코드 직접 추출 ────────────────────────
    products = (
        data.get("shoppingResult", {}).get("products", [])
        or data.get("products", [])
        or data.get("items", [])
    )

    for prod in products:
        # 직접 바코드 필드
        for field in ("barcode", "catalogBarcode", "ean", "gtin", "isbn",
                      "modelCode", "manufacturerCode"):
            raw = str(prod.get(field) or "").strip()
            if raw and _is_valid_gtin(_normalize_gtin(raw)):
                gtin = _normalize_gtin(raw)
                print(f"[GTIN] Naver Shopping 직접 추출: '{query}' → {gtin}")
                return gtin

        # 카탈로그 ID로 상세 조회
        catalog_id = prod.get("nvMid") or prod.get("catalogId") or prod.get("id")
        if catalog_id:
            gtin = _fetch_naver_catalog_barcode(str(catalog_id), timeout)
            if gtin:
                print(f"[GTIN] Naver Catalog ({catalog_id}): '{query}' → {gtin}")
                return gtin

    print(f"[GTIN] Naver Shopping 결과 없음: '{query}'")
    return ""


def _fetch_naver_catalog_barcode(catalog_id: str, timeout: int = 8) -> str:
    """Naver 카탈로그 상세 API → 바코드 추출."""
    endpoints = [
        f"https://search.shopping.naver.com/catalog/api/products/{catalog_id}",
        f"https://search.shopping.naver.com/api/products/{catalog_id}",
    ]
    for url in endpoints:
        try:
            resp = requests.get(url, headers=_NAVER_SHOP_HEADERS, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            # 재귀 탐색 (깊이 5)
            result = _deep_barcode_search(data, depth=0)
            if result:
                return result
        except Exception:
            continue
    return ""


def _lookup_naver_shopping_html(query: str, timeout: int = 10) -> str:
    """
    Naver Shopping 검색 HTML 파싱 fallback.
    __NEXT_DATA__ 또는 window.__PRELOADED_STATE__ 에서 바코드 추출.
    """
    try:
        resp = requests.get(
            "https://search.shopping.naver.com/search/all",
            params={"query": query, "sort": "rel"},
            headers={**_NAVER_SHOP_HEADERS, "Accept": "text/html,*/*"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return ""
        html = resp.text
    except Exception as ex:
        print(f"[GTIN] Naver Shopping HTML fallback 실패: {ex}")
        return ""

    # __NEXT_DATA__ 파싱
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            result = _deep_barcode_search(data, depth=0)
            if result:
                print(f"[GTIN] Naver Shopping HTML(__NEXT_DATA__): '{query}' → {result}")
                return result
        except Exception:
            pass

    return ""


def _deep_barcode_search(obj, depth: int) -> str:
    """
    JSON 객체를 재귀 탐색해 유효한 GTIN 값을 반환.
    depth 제한으로 무한 탐색 방지.
    """
    if depth > 6:
        return ""
    if isinstance(obj, dict):
        for k in ("barcode", "catalogBarcode", "ean", "gtin", "isbn",
                  "barcodeNumber", "productBarcode"):
            raw = str(obj.get(k) or "").strip()
            if raw and _is_valid_gtin(_normalize_gtin(raw)):
                return _normalize_gtin(raw)
        for v in obj.values():
            r = _deep_barcode_search(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for item in obj[:10]:   # 리스트는 앞 10개만
            r = _deep_barcode_search(item, depth + 1)
            if r:
                return r
    return ""


# ── Source 4: barcodelookup.com (국제 fallback) ───────────────────

def _lookup_barcodelookup(query: str, timeout: int = 10) -> str:
    """
    barcodelookup.com 웹 검색 → EAN/UPC 추출.

    제한: 검색 빈도 제한 있음, HTML 파싱 기반이라 구조 변경에 취약.
    한국 내수 제품 커버리지는 낮지만 ZIC(지크) 일부 제품은 등록되어 있음.
    """
    _bl_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://www.barcodelookup.com/",
    }
    try:
        resp = requests.get(
            "https://www.barcodelookup.com/search",
            params={"search": query},
            headers=_bl_headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            print(f"[GTIN] barcodelookup.com HTTP {resp.status_code}")
            return ""
        html = resp.text
    except Exception as ex:
        print(f"[GTIN] barcodelookup.com 요청 실패: {ex}")
        return ""

    # barcode 링크 패턴: /12345678901234 형태
    candidates = re.findall(r'/(\d{8,14})(?:\b|")', html)
    for raw in candidates:
        if _is_valid_gtin(_normalize_gtin(raw)):
            gtin = _normalize_gtin(raw)
            print(f"[GTIN] barcodelookup.com: '{query}' → {gtin}")
            return gtin

    print(f"[GTIN] barcodelookup.com 결과 없음: '{query}'")
    return ""


# ── Public API ────────────────────────────────────────────────────

def _build_query_variants(product_name: str, brand: str) -> list[str]:
    """
    쿼리 변형 목록 생성 — 다양한 각도로 시도해 히트율 최대화.
    중복 제거 후 반환.
    """
    variants: list[str] = []

    # 기본 쿼리 (한글 제거 + 핵심 영문)
    main = _build_query(product_name, brand)
    if main:
        variants.append(main)

    # 브랜드 + 상품명 앞 3토큰 (더 짧게)
    if main:
        tokens = main.split()
        if len(tokens) > 3:
            short = " ".join(tokens[:3])
            if short not in variants:
                variants.append(short)

    # 영문 모델번호 추출 (알파벳+숫자 혼합 토큰, 예: "OW8501D", "ZIC5W30")
    model_tokens = re.findall(r'[A-Za-z]+\d+\w*|\d+[A-Za-z]+\w*', product_name)
    if model_tokens and brand:
        model_q = f"{re.sub(r'[가-힣]+', '', brand).strip()} {' '.join(model_tokens[:2])}".strip()
        if model_q and model_q not in variants:
            variants.append(model_q)

    # 브랜드만 + 숫자 규격 (예: "Tide 152")
    nums = re.findall(r'\d{2,}', product_name)
    brand_en = re.sub(r'[가-힣]+', '', brand).strip()
    if brand_en and nums:
        brand_num_q = f"{brand_en} {nums[0]}"
        if brand_num_q not in variants:
            variants.append(brand_num_q)

    # 중복 제거하되 순서 유지
    seen: set[str] = set()
    deduped: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


def lookup(product_name: str, brand: str = "", timeout: int = 8) -> str:
    """
    상품명+브랜드 → GTIN 조회.

    조회 순서 (각 단계에서 쿼리 변형 다중 시도):
      1. UPC Item DB (국제 상품 강함)
      2. Open Food Facts (식품류)
      3. Naver Shopping 카탈로그 (한국 내수 브랜드)
      4. barcodelookup.com (국제 fallback)

    Returns:
        유효한 GTIN 문자열 (8~14자리), 없으면 ""
    """
    naver_query = f"{brand} {product_name}".strip()[:80]

    # 쿼리 변형 목록 생성
    query_variants = _build_query_variants(product_name, brand)

    if not query_variants:
        # 영문 쿼리 생성 실패 → Naver만 바로 시도
        print(f"[GTIN] 영문 쿼리 생성 불가 — Naver Shopping 직접 조회")
        return _lookup_naver_shopping(naver_query, timeout)

    print(f"[GTIN] 쿼리 변형 {len(query_variants)}개: {query_variants}")

    # 1차: UPC Item DB (쿼리 변형 순서대로)
    for q in query_variants:
        if len(q.replace(" ", "")) <= 3:
            continue
        result = _lookup_upcitemdb(q, timeout)
        if result:
            return result

    # 2차: Open Food Facts (식품류)
    for q in query_variants:
        if len(q.replace(" ", "")) <= 3:
            continue
        result = _lookup_openfoodfacts(q, timeout)
        if result:
            return result

    # 3차: Naver Shopping (한국 내수 — 항상 한글 포함 원본 쿼리 사용)
    result = _lookup_naver_shopping(naver_query, timeout)
    if result:
        return result
    # Naver에서도 영문 쿼리 변형 시도
    for q in query_variants:
        result = _lookup_naver_shopping(q, timeout)
        if result:
            return result

    # 4차: barcodelookup.com (기본 쿼리)
    for q in query_variants[:2]:   # 처음 2개만 — 너무 많은 요청 방지
        result = _lookup_barcodelookup(q, timeout)
        if result:
            return result

    return ""


def lookup_with_retry(
    product_name: str,
    brand: str = "",
    max_retries: int = 2,
    delay: float = 1.5,
) -> str:
    """재시도 포함 GTIN 조회 (네트워크 일시 오류 대비)."""
    for attempt in range(max_retries):
        result = lookup(product_name, brand)
        if result:
            return result
        if attempt < max_retries - 1:
            print(f"[GTIN] 재시도 {attempt + 1}/{max_retries - 1} ({delay}초 후)")
            time.sleep(delay)
    return ""
