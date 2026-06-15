"""
Module 4 – Coupang WING API 상품 등록

인증: HMAC-SHA256 (쿠팡 Open API 표준)
주요 기능:
  - 카테고리 목록 조회 / 검색
  - 카테고리별 필수 속성 조회
  - 이미지 업로드 (쿠팡 CDN)
  - 상품 등록
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import strftime, gmtime
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings()

from config.settings import Settings


# ── 쿠팡 API 상수 ─────────────────────────────────────────────────
BASE_URL   = "https://api-gateway.coupang.com"
ALGORITHM  = "HmacSHA256"


# ── 데이터 클래스 ─────────────────────────────────────────────────

@dataclass
class CategoryItem:
    """카테고리 트리 노드"""
    id:       int
    name:     str
    parent_id: Optional[int] = None
    full_path: str            = ""   # "가전/TV/OLED TV"
    leaf:     bool            = False


@dataclass
class AttributeOption:
    id:   int
    name: str


@dataclass
class CategoryAttribute:
    """카테고리 필수/선택 속성"""
    attribute_type_id:   int
    attribute_type_name: str
    required:            bool
    options:             list[AttributeOption] = field(default_factory=list)
    input_type:          str = "TEXT"   # TEXT / SELECT
    allowed_units:       list[str] = field(default_factory=list)  # 허용 단위 목록 (예: ["개", "ml"])
    basic_unit:          str = ""    # basicUnit — 기본 단위 (없으면 "")
    group_number:        int = 0     # groupNumber — variation 그룹 번호 (0 = 비variation)


@dataclass
class RegistrationResult:
    success:       bool
    product_id:    Optional[int]  = None
    seller_product_id: Optional[int] = None
    message:       str            = ""
    raw:           dict           = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────
# 메인 클래스
# ────────────────────────────────────────────────────────────────

class CoupangRegistrar:

    def __init__(self, settings: Settings):
        self.access_key = getattr(settings, "COUPANG_ACCESS_KEY", "")
        self.secret_key = getattr(settings, "COUPANG_SECRET_KEY", "")
        self.vendor_id  = getattr(settings, "COUPANG_VENDOR_ID",  "")

        # 판매자 배송/반품 정보
        self.vendor_user_id               = getattr(settings, "VENDOR_USER_ID", self.vendor_id)
        self.outbound_shipping_place_code = getattr(settings, "OUTBOUND_SHIPPING_PLACE_CODE", 0)
        self.return_center_code           = getattr(settings, "RETURN_CENTER_CODE", "")
        self.return_charge_name           = getattr(settings, "RETURN_CHARGE_NAME", "반품")
        self.return_charge                = getattr(settings, "RETURN_CHARGE", 7000)
        self.return_zip_code              = getattr(settings, "RETURN_ZIP_CODE", "")
        self.return_address               = getattr(settings, "RETURN_ADDRESS", "")
        self.return_address_detail        = getattr(settings, "RETURN_ADDRESS_DETAIL", "")
        self.company_contact_number       = getattr(settings, "COMPANY_CONTACT_NUMBER", "")
        self.delivery_company_code        = getattr(settings, "DELIVERY_COMPANY_CODE", "CJGLS")

        # VPS SOCKS5 프록시 (쿠팡이 VPS IP를 화이트리스트로 등록)
        # SSH 터널 명령: ssh -N -D 1080 -i ssh_keys/key.pem ubuntu@VPS_HOST
        # 터널이 열리면 로컬 127.0.0.1:1080 으로 요청 → VPS IP로 나감
        self._proxy:    Optional[dict] = None
        self._vps_host: str            = getattr(settings, "VPS_HOST", "")
        if getattr(settings, "USE_SOCKS5", False):
            port       = getattr(settings, "SOCKS5_PORT", 1080)
            socks5_url = f"socks5h://127.0.0.1:{port}"
            self._proxy = {"http": socks5_url, "https": socks5_url}
            print(f"[Coupang] SOCKS5 터널 사용: 127.0.0.1:{port} → VPS {self._vps_host}")

        print(f"[Coupang] vendor={self.vendor_id}  key={self.access_key[:8]}...")

    # ────────────────────────────────────────────────────────────
    # HMAC-SHA256 인증 헤더 생성
    # ────────────────────────────────────────────────────────────

    def _auth_header(self, method: str, path: str, query: str = "") -> dict:
        """
        쿠팡 Open API 인증 헤더 생성.

        공식 Python SDK 방식 (구분자 없이 단순 연결):
          message  = datetime + method + path + querystring
          signature = HMAC-SHA256(secret_key, message)
          Authorization: CEA algorithm=HmacSHA256,
                             access-key={access_key},
                             signed-date={datetime},
                             signature={signature}
        """
        dt = strftime('%y%m%d', gmtime()) + 'T' + strftime('%H%M%S', gmtime()) + 'Z'
        message = dt + method + path + query   # ← \n 없이 그냥 연결
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "Authorization": (
                f"CEA algorithm={ALGORITHM}, "
                f"access-key={self.access_key}, "
                f"signed-date={dt}, "
                f"signature={signature}"
            ),
            "Content-Type": "application/json;charset=UTF-8",
        }

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(params or {})
        full_path = f"{path}?{query}" if query else path
        headers = self._auth_header("GET", path, query)
        url = BASE_URL + full_path
        r = requests.get(url, headers=headers,
                         proxies=self._proxy, verify=False, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        headers = self._auth_header("POST", path)
        url = BASE_URL + path
        r = requests.post(url, headers=headers,
                          json=body,
                          proxies=self._proxy, verify=False, timeout=60)
        r.raise_for_status()
        return r.json()

    # ────────────────────────────────────────────────────────────
    # 1. 카테고리 조회
    # ────────────────────────────────────────────────────────────

    def _fetch_cat_children(self, code: int) -> list[dict]:
        """
        지정 코드 노드의 직속 자식 목록만 반환.
        Rate-limit(429) 포함 최대 3회 재시도 + 지수 백오프.
        모든 예외를 실제 타입/메시지와 함께 출력.
        """
        api_path = (
            f"/v2/providers/seller_api/apis/api/v1/marketplace/meta"
            f"/display-categories/{code}"
        )
        for attempt in range(3):
            try:
                data = self._get(api_path)
                node = data.get("data") or {}
                if isinstance(node, dict):
                    return node.get("child") or []
                return []
            except Exception as e:
                # 실제 예외 타입과 메시지를 그대로 출력
                err_type = type(e).__name__
                err_msg  = str(e)[:200]

                # HTTPError 인 경우 HTTP 상태코드 추가
                if isinstance(e, requests.HTTPError) and e.response is not None:
                    status = e.response.status_code
                    if status == 429:
                        wait = 2 ** (attempt + 1)
                        print(f"[Coupang] 카테고리 {code} Rate-limit(429) → {wait}s 대기 후 재시도")
                        time.sleep(wait)
                        continue
                    err_msg = f"HTTP{status}: {e.response.text[:120]}"

                if attempt < 2:
                    print(f"[Coupang] 카테고리 {code} 실패(시도{attempt+1}/3) [{err_type}] {err_msg}")
                    time.sleep(0.4 * (attempt + 1))
                else:
                    print(f"[Coupang] 카테고리 {code} 최종 실패 [{err_type}] {err_msg}")
        return []

    # ── 카테고리 단계별 병렬 fetch 공통 헬퍼 ──────────────────────

    def _parallel_children(
        self,
        parents: list[tuple],          # [(p_id, p_name, p_full_path, *extras), ...]
        workers: int = 5,
        level_label: str = "?",
    ) -> tuple[list[tuple], int]:
        """
        parents 각 노드의 자식을 병렬로 조회.
        반환: (children_list, error_count)
          children_list 각 원소: (child_id, child_name, child_full_path, parent_id)
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        children: list[tuple[int, str, str, int]] = []
        errors = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(self._fetch_cat_children, p[0]): p
                for p in parents
            }
            for fut in as_completed(futs):
                p = futs[fut]
                p_id, p_name, p_full = p[0], p[1], p[2]
                try:
                    for c in fut.result():
                        cid   = c.get("displayItemCategoryCode")
                        cname = c.get("name", "")
                        if cid and cname:
                            cid  = int(cid)
                            full = f"{p_full} > {cname}"
                            children.append((cid, cname, full, p_id))
                except Exception:
                    errors += 1
        ok = len(children)
        print(f"[Coupang]  L{level_label}: {ok}개 로드  (실패 {errors}건)")
        return children, errors

    def get_categories(self, parent_code: int = 0) -> list[CategoryItem]:
        """
        쿠팡 카테고리 트리를 4단계(L1~L4)까지 병렬 로드.

        max_workers=5 로 Rate-limit 방지.
        _fetch_cat_children 내부에서 3회 재시도 + 지수 백오프.

        소요 시간 (SOCKS5 터널 기준):
          L1~L3: 약 30~60초
          L4:    약 3~5분 (L3 수에 따라 가변)
        """
        print("[Coupang] 카테고리 4단계 병렬 조회 시작 (L1→L4)...")
        result: list[CategoryItem] = []

        # ── L1: 루트 직속 자식 ────────────────────────────────────
        root_raw = self._fetch_cat_children(parent_code)
        if not root_raw:
            print(
                "[Coupang] ERROR: 루트 카테고리 조회 실패! "
                "가능한 원인:\n"
                "  1) SSH SOCKS5 터널 미연결 — 터널 상태 확인 필요\n"
                "  2) Coupang API 키 오류\n"
                "  3) 네트워크 문제"
            )
            return []

        level1: list[tuple[int, str, str]] = []   # (id, name, full_path)
        for c in root_raw:
            cid = c.get("displayItemCategoryCode")
            if cid and c.get("name"):
                cid = int(cid)
                level1.append((cid, c["name"], c["name"]))
                result.append(CategoryItem(
                    id=cid, name=c["name"], parent_id=None,
                    full_path=c["name"], leaf=False,
                ))
        print(f"[Coupang]  L1: {len(level1)}개 로드")

        # ── L2 ────────────────────────────────────────────────────
        level2_raw, _ = self._parallel_children(level1, workers=5, level_label="2")
        level2: list[tuple[int, str, str]] = []
        for cid, cname, full, p_id in level2_raw:
            level2.append((cid, cname, full))
            result.append(CategoryItem(
                id=cid, name=cname, parent_id=p_id,
                full_path=full, leaf=False,
            ))

        # ── L3 ────────────────────────────────────────────────────
        level3_raw, _ = self._parallel_children(level2, workers=5, level_label="3")
        level3: list[tuple[int, str, str]] = []
        for cid, cname, full, p_id in level3_raw:
            level3.append((cid, cname, full))
            result.append(CategoryItem(
                id=cid, name=cname, parent_id=p_id,
                full_path=full, leaf=False,    # L4 존재 여부 미확인
            ))

        # ── L4 (실제 등록 가능 leaf) ──────────────────────────────
        level4_raw, l4_err = self._parallel_children(level3, workers=5, level_label="4")
        for cid, cname, full, p_id in level4_raw:
            result.append(CategoryItem(
                id=cid, name=cname, parent_id=p_id,
                full_path=full, leaf=True,
            ))

        # L4 자식이 없는 L3 노드 → 실질적 leaf 로 보정 (set 사용 O(n))
        l4_parent_ids = {p_id for _, _, _, p_id in level4_raw}
        level3_ids    = {cid  for cid, *_         in level3_raw}
        for cat in result:
            if not cat.leaf and cat.id in level3_ids and cat.id not in l4_parent_ids:
                cat.leaf = True

        print(f"[Coupang] 총 {len(result)}개 카테고리 로드 완료")
        return result

    # ── 최소 캐시 유효성 기준 ─────────────────────────────────────
    _MIN_CACHE_SIZE = 200   # 이보다 적으면 불완전 캐시로 간주

    def get_categories_cached(self, force_refresh: bool = False) -> list[CategoryItem]:
        """
        카테고리 목록 반환. 로컬 JSON 캐시 우선 사용.

        캐시 파일: data/category_cache.json
        * force_refresh=True  → API 재조회 후 캐시 갱신
        * 캐시 항목이 _MIN_CACHE_SIZE 미만 → 불완전 캐시로 간주하고 재구축
        """
        cache_path = Path(__file__).resolve().parent.parent / "data" / "category_cache.json"

        if not force_refresh and cache_path.exists():
            try:
                with open(cache_path, encoding="utf-8") as f:
                    raw = json.load(f)
                cats = [
                    CategoryItem(
                        id=d["id"], name=d["name"],
                        parent_id=d.get("parent_id"),
                        full_path=d.get("full_path", d["name"]),
                        leaf=d.get("leaf", False),
                    )
                    for d in raw
                ]

                # 단계별 분포 계산
                depth_counts: dict[int, int] = {}
                for c in cats:
                    d = c.full_path.count(" > ")
                    depth_counts[d] = depth_counts.get(d, 0) + 1
                dist = "  ".join(f"L{d+1}={n}" for d, n in sorted(depth_counts.items()))
                max_depth = max(depth_counts.keys(), default=0)

                valid = (
                    len(cats) >= self._MIN_CACHE_SIZE   # 항목 수 충분
                    and max_depth >= 3                   # L4(depth=3) 이상 포함
                )
                if valid:
                    print(f"[Coupang] 카테고리 캐시 로드: 총 {len(cats)}개  ({dist})")
                    return cats
                else:
                    reason = (
                        f"항목 부족({len(cats)}개)" if len(cats) < self._MIN_CACHE_SIZE
                        else f"최대 깊이 L{max_depth+1}뿐 — L4 미포함"
                    )
                    print(f"[Coupang] 캐시 불완전({reason}) → API 재구축 시작")
            except Exception as e:
                print(f"[Coupang] 캐시 읽기 실패, API 재조회: {e}")

        cats = self.get_categories()

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    [{"id": c.id, "name": c.name, "parent_id": c.parent_id,
                      "full_path": c.full_path, "leaf": c.leaf}
                     for c in cats],
                    f, ensure_ascii=False,
                )
            print(f"[Coupang] 카테고리 캐시 저장: {len(cats)}개 → {cache_path.name}")
        except Exception as e:
            print(f"[Coupang] 캐시 저장 실패: {e}")

        return cats

    def search_categories_cached(
        self,
        keyword: str,
        force_refresh: bool = False,
    ) -> list[CategoryItem]:
        """
        키워드로 카테고리 검색 (로컬 캐시 우선).

        3단계 매칭 전략:
          1. 공백 제거 후 포함 여부  (기어오일 → 기어 오일 도 매칭)
          2. 모든 토큰이 full_path 에 포함  (기어 오일 → 두 단어 모두 포함)
          3. 단일 짧은 키워드는 name 에만 포함 여부 재확인
        """
        all_cats = self.get_categories_cached(force_refresh)

        kw_raw    = keyword.strip().lower()
        kw_nsp    = kw_raw.replace(" ", "")          # 공백 제거
        kw_tokens = [t for t in kw_raw.split() if t]  # 공백 분리 토큰

        def _matches(c: CategoryItem) -> bool:
            path_l   = c.full_path.lower()
            path_nsp = path_l.replace(" ", "")
            name_l   = c.name.lower()
            name_nsp = name_l.replace(" ", "")

            # 전략 1: 공백 제거 후 포함
            if kw_nsp in path_nsp or kw_nsp in name_nsp:
                return True
            # 전략 2: 모든 토큰이 full_path 에 포함
            if kw_tokens and all(tok in path_nsp for tok in kw_tokens):
                return True
            return False

        results = [c for c in all_cats if _matches(c)]
        leaf_first = sorted(results, key=lambda c: (not c.leaf, c.full_path))
        return leaf_first

    def get_category_children(self, code: int) -> list[dict]:
        """
        지정 카테고리의 직속 자식 목록 반환 (lazy L5 로딩용).

        반환 예시:
          [{'displayItemCategoryCode': 78889, 'name': '휘발유/가솔린', ...}, ...]
        자식이 없으면 빈 리스트.
        """
        return self._fetch_cat_children(code)

    def get_category_by_code(self, code: int) -> Optional[CategoryItem]:
        """단일 카테고리 코드 유효성 확인 및 정보 반환."""
        api_path = (
            f"/v2/providers/seller_api/apis/api/v1/marketplace/meta"
            f"/display-categories/{code}"
        )
        try:
            data = self._get(api_path)
            node = data.get("data") or {}
            if not isinstance(node, dict):
                return None
            cid   = node.get("displayItemCategoryCode")
            cname = node.get("name", "")
            if not cid:
                return None
            return CategoryItem(id=int(cid), name=cname, full_path=cname, leaf=True)
        except Exception:
            return None

    # ────────────────────────────────────────────────────────────
    # 2. 카테고리 필수 속성 조회
    # ────────────────────────────────────────────────────────────

    def get_category_attributes(self, category_id: int) -> list[CategoryAttribute]:
        """
        선택한 카테고리의 필수·선택 속성 목록 반환.

        API: GET /v2/.../meta/category-related-metas/display-category-codes/{code}
        """
        print(f"[Coupang] 카테고리 {category_id} 속성 조회 중...")
        try:
            data = self._get(
                f"/v2/providers/seller_api/apis/api/v1/marketplace/meta"
                f"/category-related-metas/display-category-codes/{category_id}",
            )
        except Exception as e:
            print(f"[Coupang] 속성 조회 실패: {e}")
            return []

        attrs: list[CategoryAttribute] = []
        # 응답: data.data.attributes 또는 data.data (list)
        payload = data.get("data") or {}
        if isinstance(payload, dict):
            items = (
                payload.get("attributes") or
                payload.get("items") or
                payload.get("requiredDocumentNames") or
                []
            )
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        # 디버그: 첫 번째 속성 항목의 키 목록 출력 (단위 관련 필드 파악용)
        if items:
            _dbg_keys = list(items[0].keys()) if isinstance(items[0], dict) else []
            print(f"[Coupang] 속성 API 필드: {_dbg_keys}")

        for item in items:
            if not isinstance(item, dict):
                continue
            # 실제 API 응답: attributeTypeName = 문자열, required = "MANDATORY"/"OPTIONAL"
            attr_name = item.get("attributeTypeName", "")
            if isinstance(attr_name, dict):
                attr_id   = attr_name.get("id", 0)
                attr_name = attr_name.get("name", "")
            else:
                attr_id = 0

            # required: "MANDATORY" = True, 나머지 = False
            req_val = item.get("required", "OPTIONAL")
            is_required = (req_val == "MANDATORY") if isinstance(req_val, str) else bool(req_val)

            # inputValues (SELECT 타입) 또는 attributeValueList
            raw_opts = item.get("inputValues") or item.get("attributeValueList") or []
            options = []
            for o in raw_opts:
                if isinstance(o, str):
                    options.append(AttributeOption(id=0, name=o))
                elif isinstance(o, dict):
                    options.append(AttributeOption(id=o.get("id", 0), name=o.get("name", "")))

            # inputType: "INPUT" = TEXT, "SELECT"/"COMBO" = SELECT
            raw_itype = item.get("inputType", "INPUT")
            input_type = "TEXT" if raw_itype == "INPUT" else "SELECT"

            # 단위 목록 파싱 (Coupang API 필드명이 버전마다 다름 — 여러 이름 시도)
            # usableUnits: list, basicUnit: 단일 문자열 (최신 API 응답)
            raw_units: list = (
                item.get("usableUnits") or    # 최신: list of str/dict
                item.get("unitTypes") or
                item.get("attributeUnit") or
                item.get("units") or
                item.get("unitTypeList") or
                []
            )
            # basicUnit: 단일 기본 단위 (없으면 None)
            basic_unit = item.get("basicUnit")
            if basic_unit and isinstance(basic_unit, str):
                str_units = [u for u in raw_units if isinstance(u, str)]
                if basic_unit not in str_units:
                    raw_units = list(raw_units) + [basic_unit]

            allowed_units: list[str] = []
            for u in raw_units:
                if isinstance(u, str) and u:
                    if u not in allowed_units:
                        allowed_units.append(u)
                elif isinstance(u, dict):
                    uname = u.get("name") or u.get("unit") or u.get("code") or ""
                    if uname and uname not in allowed_units:
                        allowed_units.append(str(uname))

            _basic_unit   = item.get("basicUnit") or ""
            _group_number = item.get("groupNumber") or 0

            if allowed_units:
                print(
                    f"[Coupang] 속성 '{attr_name}' "
                    f"basicUnit={_basic_unit!r}  groupNo={_group_number}  "
                    f"단위 목록: {allowed_units}"
                )
            else:
                print(
                    f"[Coupang] 속성 '{attr_name}' "
                    f"basicUnit={_basic_unit!r}  groupNo={_group_number}"
                )

            # ── 진단: inputType + 선택지(inputValues) 로그 ──────────
            _req_tag = "필수" if is_required else "선택"
            if options:
                print(
                    f"[Coupang]   └ [{_req_tag}] inputType={raw_itype}  "
                    f"선택지({len(options)}개): {[o.name for o in options[:15]]}"
                )
            else:
                print(f"[Coupang]   └ [{_req_tag}] inputType={raw_itype}  선택지=없음(자유입력)")

            attrs.append(CategoryAttribute(
                attribute_type_id=attr_id,
                attribute_type_name=attr_name,
                required=is_required,
                options=options,
                input_type=input_type,
                allowed_units=allowed_units,
                basic_unit=_basic_unit,
                group_number=_group_number,
            ))

        print(f"[Coupang] 속성 {len(attrs)}개 (필수 {sum(1 for a in attrs if a.required)}개)")
        return attrs

    # ────────────────────────────────────────────────────────────
    # 3. 이미지 업로드 (쿠팡 CDN)
    # ────────────────────────────────────────────────────────────

    def upload_image(self, image_path: str) -> Optional[str]:
        """
        로컬 이미지 업로드.
        1순위: 쿠팡 CDN (vendor-inventory/ 경로 반환 → cdnPath 사용)
        2순위: catbox.moe fallback (https URL 반환 → vendorPath 사용)
        실패 시 None.
        """
        path = Path(image_path)
        if not path.exists():
            print(f"[Coupang] 이미지 파일 없음: {image_path}")
            return None

        print(f"[Coupang] 이미지 업로드: {path.name}")

        # ── 1순위: 쿠팡 CDN ──────────────────────────────────────
        try:
            api_path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products/images/upload"
            headers = self._auth_header("POST", api_path)
            headers.pop("Content-Type", None)   # multipart 업로드 시 Content-Type 제거
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            with open(path, "rb") as f:
                files = {"file": (path.name, f, mime)}
                resp = requests.post(
                    BASE_URL + api_path,
                    headers=headers,
                    files=files,
                    proxies=self._proxy,
                    verify=False,
                    timeout=60,
                )
            resp.raise_for_status()
            res = resp.json()
            cdn_url = (
                res.get("data") or
                res.get("imageUrl") or
                res.get("url") or ""
            )
            if cdn_url:
                print(f"[Coupang] CDN 업로드 완료: {cdn_url[:60]}...")
                return cdn_url
        except Exception as e:
            print(f"[Coupang] CDN 업로드 실패: {e} → catbox.moe fallback 시도")

        # ── 2순위: catbox.moe (외부 공개 이미지 호스팅) ──────────
        # MIME을 다시 판단 (png/jpg)
        try:
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            with open(path, "rb") as f:
                cb_resp = requests.post(
                    "https://catbox.moe/user/api.php",
                    data={"reqtype": "fileupload"},
                    files={"fileToUpload": (path.name, f, mime)},
                    timeout=30,
                )
            if cb_resp.ok and "catbox.moe" in cb_resp.text:
                ext_url = cb_resp.text.strip()
                print(f"[Coupang] catbox.moe 업로드 완료: {ext_url}")
                return ext_url
            else:
                print(f"[Coupang] catbox.moe 실패: {cb_resp.status_code} {cb_resp.text[:80]}")
        except Exception as e:
            print(f"[Coupang] catbox.moe 오류: {e}")

        return None

    def upload_images(self, image_paths: list[str]) -> list[str]:
        """여러 이미지 순차 업로드. 실패한 것은 건너뜀."""
        urls = []
        for p in image_paths:
            url = self.upload_image(p)
            if url:
                urls.append(url)
        return urls

    # ────────────────────────────────────────────────────────────
    # 3.5  상품고시 (notices) 빌더
    # ────────────────────────────────────────────────────────────

    def build_notices(self, category_id: int, default_content: str = "상세페이지 참조") -> list[dict]:
        """
        카테고리에 맞는 상품고시 목록 생성.
        모든 항목의 content 를 default_content 로 채움.

        API: GET /v2/.../meta/category-related-metas/display-category-codes/{code}
        응답.data.noticeCategories 사용.
        """
        try:
            data = self._get(
                f"/v2/providers/seller_api/apis/api/v1/marketplace/meta"
                f"/category-related-metas/display-category-codes/{category_id}",
            )
        except Exception as e:
            print(f"[Coupang] notices 조회 실패: {e}")
            return []

        # notices는 oneOf 스키마 — 3개 중 정확히 1개 카테고리만 사용해야 함
        # 첫 번째(기본) 카테고리만 선택
        all_cats = (data.get("data") or {}).get("noticeCategories") or []

        if not all_cats:
            print(f"[Coupang] notices: noticeCategories 비어있음 (cat={category_id})")
            return []
        # 디버그: notice 카테고리 목록 한 줄 요약
        _nc_summary = ", ".join(
            f"[{i}]'{nc.get('noticeCategoryName','')[:15]}'"
            f"({len(nc.get('noticeCategoryDetailNames') or [])}항목)"
            for i, nc in enumerate(all_cats)
        )
        print(f"[Coupang] noticeCategories: {_nc_summary}")

        primary_cat = all_cats[0]
        cat_name = primary_cat.get("noticeCategoryName", "")
        notices = []
        for detail in primary_cat.get("noticeCategoryDetailNames") or []:
            detail_name = detail.get("noticeCategoryDetailName", "")
            if cat_name and detail_name:
                notices.append({
                    "noticeCategoryName":       cat_name,
                    "noticeCategoryDetailName": detail_name,
                    "content":                  default_content,
                })
        print(f"[Coupang] notices 생성 {len(notices)}개 (카테고리: {cat_name[:30]})")
        return notices

    # ────────────────────────────────────────────────────────────
    # 4. 상품 등록
    # ────────────────────────────────────────────────────────────

    def register_product(
        self,
        *,
        name:         str,
        category_id:  int,
        bundle_list:  list[dict],   # 묶음별 item 목록 — 아래 형식 참조
        delivery_fee: int  = 0,
        notices:      Optional[list[dict]] = None,   # None = 자동 조회
        brand:        str  = "해당없음",
    ) -> RegistrationResult:
        """
        쿠팡에 상품 1개(= 여러 묶음 옵션을 가진 단일 product) 등록.

        bundle_list 각 원소:
          {
            "qty":              int,        # 묶음 수량 (표시·속성 override용)
            "sale_price":       int,        # 판매가 (×1.27)
            "original_price":   int,        # 정상가 (×1.37)
            "image_urls":       list[str],  # HTTPS URL 또는 vendor-inventory/ 경로
            "attributes":       list[dict], # 수량 포함 속성 목록
            "detail_image_url": str,        # 상세페이지 이미지 (빈 문자열 가능)
            "item_name":        str,        # itemName (없으면 "{name} {qty}개" 자동)
          }

        각 bundle 은 body["items"] 배열의 원소 1개가 됩니다.
        수량(수량 속성)이 다른 item 이 여러 개 있어야 쿠팡이 옵션 묶음으로 처리합니다.
        """
        if not bundle_list:
            return RegistrationResult(success=False, message="bundle_list 가 비어있습니다.")

        # notices 자동 생성
        if notices is None:
            notices = self.build_notices(category_id)

        # ── 이미지 엔트리 빌더 ──────────────────────────────────────
        # cdnPath  = 쿠팡 CDN 내부 경로 (upload API 반환값)
        # vendorPath = 외부 URL (쿠팡이 fetch → 자체 CDN 저장)
        def _img_entry(i: int, url: str) -> dict:
            base = {
                "imageOrder": i,
                "imageType":  "REPRESENTATION" if i == 0 else "OPTIONAL",
            }
            if url.startswith("vendor-inventory/") or url.startswith("/vendor-inventory/"):
                base["cdnPath"] = url
            else:
                base["vendorPath"] = url
            return base

        # ── items 배열 빌드 ─────────────────────────────────────────
        items_list: list[dict] = []
        for bundle in bundle_list:
            qty               = bundle.get("qty", 1)
            sale_price        = bundle["sale_price"]
            original_price    = bundle["original_price"]
            image_urls        = bundle.get("image_urls", [])
            attributes        = bundle.get("attributes", [])
            detail_image_url  = bundle.get("detail_image_url", "")
            item_name         = bundle.get("item_name") or f"{name} {qty}개"

            if not image_urls:
                return RegistrationResult(
                    success=False,
                    message=f"{qty}개 묶음 이미지 URL 이 없습니다.",
                )

            images_arr  = [_img_entry(i, url) for i, url in enumerate(image_urls)]
            unit_count  = bundle.get("unit_count", 1)    # 개당 수량
            spec_text   = bundle.get("spec_text", "")    # 규격 텍스트 (용량·수량)

            print(
                f"[Coupang]  [{qty}개] 판매가={original_price:,}원  "
                f"시작가격={sale_price:,}원  최종가격={original_price:,}원  "
                f"속성={len(attributes)}개"
            )
            for _a in attributes:
                _unit = _a.get("attributeUnitValueName", "(단위없음)")
                print(
                    f"    - {_a['attributeTypeName']}: "
                    f"'{_a['attributeValueName']}'  단위={_unit}"
                )

            items_list.append({
                "itemName":                  item_name,
                "originalPrice":             original_price,   # ×1.37 정상가 (API 필수)
                "salePrice":                 original_price,   # ×1.37 판매가
                "minimumPrice":              sale_price,       # ×1.27 최저가 (자동가격조정용)
                "autoPricingInfoView": {
                    "active":       True,
                    "minimumPrice": sale_price,       # ×1.27 최저가
                    "wishPrice":    original_price,   # ×1.37 설정가격
                },
                "maximumBuyCount":           999,
                "maximumBuyForPerson":       0,
                "maximumBuyForPersonPeriod": 1,
                "unitCount":                 unit_count,
                "adultOnly":                 "EVERYONE",
                "taxType":                   "TAX",
                "skuCount":                  100,
                "offerCondition":            "NEW",
                "parallelImported":          "NOT_PARALLEL_IMPORTED",
                "overseasPurchased":         "NOT_OVERSEAS_PURCHASED",
                "pccNeeded":                 False,
                "emptyBarcode":              True,
                "emptyBarcodeReason":        "COUPANG",
                "outboundShippingTimeDay":   3,
                "images":                    images_arr,
                "attributes":                attributes,
                "notices":                   notices,
                "certifications": [
                    {
                        "certificationType":        "NOT_REQUIRED",
                        "certificationCode":        "",
                        "certificationAttachments": [],
                    }
                ],
                "contents": [
                    # ── 상세 이미지 (누끼) — URL 있을 때만 포함 ──────
                    *(
                        [
                            {
                                "contentsType":   "IMAGE",
                                "contentDetails": [
                                    {
                                        "content":    detail_image_url,
                                        "detailType": "IMAGE",
                                    }
                                ],
                            }
                        ]
                        if detail_image_url
                        else []
                    ),
                    # ── 상품명 텍스트 ────────────────────────────────
                    {
                        "contentsType":   "TEXT",
                        "contentDetails": [
                            {
                                "content":    item_name,
                                "detailType": "TEXT",
                            },
                        ],
                    },
                    # ── 제품 규격 (용량·수량) — 입력된 경우만 포함 ─────
                    *(
                        [
                            {
                                "contentsType":   "TEXT",
                                "contentDetails": [
                                    {
                                        "content":    spec_text,
                                        "detailType": "TEXT",
                                    }
                                ],
                            }
                        ]
                        if spec_text
                        else []
                    ),
                ],
            })

        body = {
            "displayCategoryCode":       category_id,
            "sellerProductName":         name,
            "vendorId":                  self.vendor_id,
            "saleStartedAt":             "2020-01-01T00:00:00",
            "saleEndedAt":               "2099-12-31T00:00:00",
            "displayProductName":        name,
            "brand":                     brand or "해당없음",
            "generalProductName":        name,
            "productGroup":              name[:50],
            "deliveryMethod":            "SEQUENCIAL",
            "deliveryCompanyCode":       self.delivery_company_code,
            "deliveryChargeType":        "FREE" if delivery_fee == 0 else "NOT_FREE",
            "deliveryCharge":            delivery_fee,
            "freeShipOverAmount":        0,
            "returnCenterCode":          self.return_center_code,
            "returnChargeName":          self.return_charge_name,
            "returnCharge":              self.return_charge,
            "returnZipCode":             self.return_zip_code,
            "returnAddress":             self.return_address,
            "returnAddressDetail":       self.return_address_detail,
            "companyContactNumber":      self.company_contact_number,
            "outboundShippingPlaceCode": self.outbound_shipping_place_code,
            "outboundShippingTimeDay":   3,
            "unionDeliveryType":         "NOT_UNION_DELIVERY",
            "deliveryChargeOnReturn":    self.return_charge,
            "deliverySurcharge":         0,
            "remoteAreaDeliverable":     "Y",
            "bundlePackingDelivery":     0,
            "exchangeType":              "AFTER",
            "afterServiceInformation":   "",
            "afterServiceContactNumber": "",
            "vendorUserId":              self.vendor_user_id,
            "items":                     items_list,
        }

        print(
            f"[Coupang] 상품 등록 요청: {name[:50]}"
            f"  |  브랜드='{brand}'  묶음 수={len(bundle_list)}개  notices={len(notices)}개"
        )

        # ── 진단: items 요약 ─────────────────────────────────────────
        import json as _json
        for _i, _item in enumerate(items_list):
            _diag = {
                "itemName":      _item.get("itemName", "")[:60],
                "originalPrice": _item.get("originalPrice"),
                "salePrice":     _item.get("salePrice"),
                "attributes":    _item.get("attributes", []),
                "notices_count": len(_item.get("notices", [])),
                "images":        _item.get("images", []),
            }
            print(
                f"[Coupang] items[{_i}] 진단:\n"
                f"{_json.dumps(_diag, ensure_ascii=False, indent=2)}"
            )

        try:
            api_path = "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products"
            res = self._post(api_path, body)
            # data 필드가 dict 또는 int(sellerProductId 직접) 양쪽 처리
            raw_data = res.get("data")
            if isinstance(raw_data, int):
                pid = raw_data
            elif isinstance(raw_data, dict):
                pid = raw_data.get("sellerProductId")
            else:
                pid = res.get("sellerProductId")
            if pid:
                print(f"[Coupang] 등록 성공! sellerProductId={pid}")
                return RegistrationResult(
                    success=True,
                    seller_product_id=pid,
                    message="등록 완료",
                    raw=res,
                )
            else:
                err_items = res.get("errorItems") or res.get("details") or []
                print(f"[Coupang] 등록 실패: {res.get('message', '')}")
                if err_items:
                    print(f"[Coupang] 오류 항목: {err_items}")
                else:
                    print(f"[Coupang] 전체 응답: {res}")
                return RegistrationResult(
                    success=False,
                    message=str(res.get("message") or res),
                    raw=res,
                )
        except requests.HTTPError as e:
            body_txt = e.response.text if e.response else ""
            print(f"[Coupang] HTTP 오류 {e.response.status_code}: {body_txt[:300]}")
            return RegistrationResult(
                success=False,
                message=f"HTTP {e.response.status_code}: {body_txt[:200]}",
            )
        except Exception as e:
            print(f"[Coupang] 등록 실패: {e}")
            return RegistrationResult(success=False, message=str(e))

    # ────────────────────────────────────────────────────────────
    # 5. 연결 테스트
    # ────────────────────────────────────────────────────────────

    # ────────────────────────────────────────────────────────────
    # 브랜드 검색 및 매칭
    # ────────────────────────────────────────────────────────────

    def search_brands(self, keyword: str) -> list[dict]:
        """
        쿠팡 브랜드 검색 API 호출.

        API: GET /v2/providers/seller_api/apis/api/v1/marketplace/meta/brands
        params: keyword={브랜드명}

        반환: [{"brandId": 12345, "brandName": "Nike"}, ...]
        """
        if not keyword or not keyword.strip():
            return []

        try:
            data = self._get(
                "/v2/providers/seller_api/apis/api/v1/marketplace/meta/brands",
                {"keyword": keyword.strip()},
            )
            raw_list = data.get("data") or []
            if isinstance(raw_list, dict):
                raw_list = raw_list.get("brands") or raw_list.get("items") or []

            results = []
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                bid   = item.get("brandId") or item.get("id")
                bname = item.get("brandName") or item.get("name") or ""
                if bid and bname:
                    results.append({"brandId": int(bid), "brandName": str(bname)})
            return results
        except Exception as e:
            print(f"[Coupang] 브랜드 검색 실패 (keyword={keyword}): {e}")
            return []

    def resolve_brand(
        self,
        naver_brand: str,
        gemini_api_key: str = "",
        gemini_model: str = "gemini-2.0-flash",
    ) -> str:
        """
        네이버 브랜드명 → 쿠팡 공식 브랜드명(매칭 성공 시) 또는 원본 브랜드명(폴백).

        파이프라인:
          1) 쿠팡 브랜드 검색 API로 후보 조회
          2) 후보 1개면 바로 확정
          3) 후보 여러 개면 Gemini로 최적 매칭
          4) 실패/확신 없음 → 원본 브랜드명 반환 (직접입력으로 처리)

        반환: 쿠팡에 등록할 브랜드명 문자열
        """
        if not naver_brand:
            return "해당없음"

        print(f"[Brand] 브랜드 매칭 시작: '{naver_brand}'")

        candidates = self.search_brands(naver_brand)
        if not candidates:
            print(f"[Brand] 쿠팡 브랜드 검색 결과 없음 → 직접입력: '{naver_brand}'")
            return naver_brand

        print(f"[Brand] 후보 {len(candidates)}개: {[c['brandName'] for c in candidates[:5]]}")

        # 후보 1개이고 이름이 완전히 같으면(대소문자 무시) 바로 확정
        if len(candidates) == 1:
            matched = candidates[0]
            print(f"[Brand] 단일 후보 확정: '{matched['brandName']}' (id={matched['brandId']})")
            return matched["brandName"]

        # 후보 여러 개 → Gemini 매칭
        if gemini_api_key:
            from modules.gemini_writer import match_brand_with_gemini
            matched = match_brand_with_gemini(
                naver_brand=naver_brand,
                coupang_candidates=candidates,
                api_key=gemini_api_key,
                model=gemini_model,
            )
            if matched:
                print(
                    f"[Brand] Gemini 매칭 성공: '{naver_brand}' → "
                    f"'{matched['brandName']}' (id={matched['brandId']})"
                )
                return matched["brandName"]
            print(f"[Brand] Gemini 매칭 실패(확신 없음) → 직접입력: '{naver_brand}'")
        else:
            print("[Brand] Gemini API 키 없음 → 직접입력 폴백")

        return naver_brand

    def diagnose_proxy(self) -> bool:
        """
        SOCKS5 프록시를 실제로 통과해 공개 IP를 확인.
        성공 시 True (터널 정상), 실패 시 False.
        """
        if not self._proxy:
            print("[Coupang] 프록시 설정 없음 (USE_SOCKS5=false) — 직접 연결 사용")
            return True

        print(f"[Coupang] 프록시 통과 테스트 ({self._proxy['https']})...")
        try:
            r = requests.get(
                "https://api.ipify.org?format=json",
                proxies=self._proxy,
                verify=False,
                timeout=15,
            )
            ip = r.json().get("ip", "")
            vps = getattr(self, "_vps_host", "")
            match = f" [VPS IP 일치]" if (ip == vps) else f" [주의: VPS IP({vps})와 다름]"
            print(f"[Coupang]  프록시 통과 IP = {ip}{match}")
            return True
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"[Coupang]  프록시 통과 실패: {err}")
            print(
                "[Coupang]  가능한 원인:\n"
                "    1) SSH 터널 프로세스가 VPS와 연결이 끊긴 상태 (포트는 열려 있지만 데드)\n"
                "    2) PySocks/requests-socks 패키지 미설치 (pip install requests[socks])\n"
                "    3) VPS 자체가 다운됨"
            )
            return False

    def test_connection(self) -> bool:
        """
        API 키·SOCKS5 연결 정상 여부 확인.
        실패 시 원인(터널/인증/네트워크)을 최대한 특정해 출력.
        """
        print("[Coupang] API 연결 테스트...")

        # 0) 프록시 실제 통과 테스트 (공개 IP 확인)
        proxy_ok = self.diagnose_proxy()
        if not proxy_ok:
            print("[Coupang] 프록시 문제로 API 연결 불가. 터널을 재시작하세요.")
            return False

        # 1) 카테고리 루트 조회 — _fetch_cat_children 우회해 직접 _get 호출
        print("[Coupang] [1/2] 카테고리 루트 조회...")
        try:
            api_path = (
                "/v2/providers/seller_api/apis/api/v1/marketplace/meta"
                "/display-categories/0"
            )
            data     = self._get(api_path)
            node     = data.get("data") or {}
            children = node.get("child") or [] if isinstance(node, dict) else []
            print(f"[Coupang]  카테고리 루트 OK — L1 {len(children)}개 확인")
        except Exception as e:
            print(f"[Coupang]  카테고리 루트 실패 [{type(e).__name__}]: {e}")
            if isinstance(e, requests.HTTPError) and e.response is not None:
                print(f"[Coupang]  응답: HTTP{e.response.status_code}: {e.response.text[:200]}")
            return False

        # 2) 상품 목록 조회 (인증 포함)
        print("[Coupang] [2/2] 상품 목록 조회 (인증 테스트)...")
        try:
            data = self._get(
                "/v2/providers/seller_api/apis/api/v1/marketplace/seller-products",
                {"vendorId": self.vendor_id, "status": "APPROVED", "limit": 1},
            )
            print(f"[Coupang]  인증 OK | 응답 코드: {data.get('code')}")
            return True
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else 0
            body   = e.response.text[:200] if e.response else ""
            print(f"[Coupang]  인증 실패 HTTP{status}: {body}")
            if status == 403:
                print("[Coupang]  -> IP 화이트리스트 미등록 또는 터널 IP 불일치")
            elif status == 401:
                print("[Coupang]  -> API 키(access-key / secret-key) 오류")
            return False
        except Exception as e:
            print(f"[Coupang]  연결 실패 [{type(e).__name__}]: {e}")
            return False
