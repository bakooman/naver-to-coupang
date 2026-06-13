"""
네이버 스마트스토어 가격 변동 모니터링

기능:
  - URL 목록 등록 / 삭제 / 조회
  - 현재 가격 / 품절 여부 경량 조회 (requests → __PRELOADED_STATE__ 파싱)
  - 가격 상승 / 품절 이벤트 감지 → alerts 목록 갱신
  - 데이터 영속화: data/price_watch.json

체크 주기:
  - 하루 2회 (00:00 / 12:00) asyncio 백그라운드 스케줄러

데이터 구조 (price_watch.json):
  {
    "watches": [
      {
        "uid": "...",
        "url": "https://smartstore.naver.com/...",
        "name": "상품명",
        "base_price": 12000,       ← 등록 당시 가격
        "current_price": 13000,    ← 최근 체크 가격
        "status": "risen",         ← "ok" / "risen" / "soldout"
        "change": 1000,            ← 변동액 (양수=상승, 음수=하락)
        "last_checked": "2025-06-01T00:00:00",
        "added": "2025-05-31T22:00:00",
        "history": [
          {"ts": "...", "price": 12000, "soldout": false}
        ]
      }
    ]
  }
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_DATA_FILE = Path(__file__).parent.parent / "data" / "price_watch.json"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://smartstore.naver.com/",
}

_BRIGHTDATA_API_URL  = "https://api.brightdata.com/request"
_BRIGHTDATA_ZONE     = "web_unlocker1"


def _get_brightdata_key() -> str:
    """settings에서 BrightData API 키 조회."""
    try:
        from config.settings import Settings
        return getattr(Settings, "BRIGHTDATA_API_KEY", "") or ""
    except Exception:
        return ""


def _brightdata_fetch(url: str, timeout: int = 90) -> Optional[str]:
    """
    BrightData Web Unlocker API를 통해 HTML 반환.
    API 키 없거나 실패 시 None 반환.
    """
    api_key = _get_brightdata_key()
    if not api_key:
        return None
    try:
        resp = requests.post(
            _BRIGHTDATA_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "zone":   _BRIGHTDATA_ZONE,
                "url":    url,
                "format": "raw",
            },
            timeout=timeout,
            verify=False,
        )
        if resp.status_code == 200:
            return resp.text
        print(f"[PriceChecker] BrightData 오류 {resp.status_code}: {resp.text[:200]}")
        return None
    except Exception as e:
        print(f"[PriceChecker] BrightData 요청 실패: {e}")
        return None

_SIGNALS = {"salePrice", "productName", "productNo", "name", "price"}


# ── 데이터 클래스 ─────────────────────────────────────────────────

@dataclass
class PriceSnapshot:
    ts:      str
    price:   int
    soldout: bool


@dataclass
class PriceWatch:
    uid:           str
    url:           str
    name:          str           = "조회 중..."
    base_price:    int           = 0    # 등록 당시 / 첫 체크 가격
    current_price: int           = 0    # 최근 체크 가격
    status:        str           = "ok" # "ok" / "risen" / "fallen" / "soldout"
    change:        int           = 0    # 변동액 (양수=상승)
    last_checked:  str           = ""
    added:         str           = field(default_factory=lambda: _now_str())
    history:       list[dict]    = field(default_factory=list)
    acked:         bool          = False # 확인완료 처리됨 — True면 재알림 억제
    store:         str           = "샵케이"  # "샵케이" / "제니스 트레이딩"


def _now_str() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ── 파일 입출력 ───────────────────────────────────────────────────

def _load() -> list[PriceWatch]:
    """JSON 파일 → PriceWatch 목록."""
    _DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not _DATA_FILE.exists():
        return []
    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8-sig"))
        watches = []
        for d in raw.get("watches", []):
            pw = PriceWatch(uid=d["uid"], url=d["url"])
            for k, v in d.items():
                if hasattr(pw, k):
                    setattr(pw, k, v)
            watches.append(pw)
        return watches
    except Exception as e:
        print(f"[PriceChecker] 로드 오류: {e}")
        return []


def _save(watches: list[PriceWatch], check_state: dict | None = None) -> None:
    """PriceWatch 목록 → JSON 파일."""
    try:
        data: dict = {"watches": [asdict(w) for w in watches]}
        if check_state is not None:
            data["check_state"] = check_state
        else:
            # 기존 check_state 유지
            try:
                existing = json.loads(_DATA_FILE.read_text(encoding="utf-8-sig"))
                if "check_state" in existing:
                    data["check_state"] = existing["check_state"]
            except Exception:
                pass
        _DATA_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[PriceChecker] 저장 오류: {e}")


def get_check_state() -> dict:
    """체크 진행 상태 반환. 앱 재시작 시 중단 여부 판별에 사용."""
    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8-sig"))
        return raw.get("check_state", {})
    except Exception:
        return {}


def clear_check_state() -> None:
    """체크 상태 초기화 (정상 완료 or 사용자가 재시작 후 확인)."""
    watches = _load()
    _save(watches, check_state={"in_progress": False, "done": 0, "total": 0, "started_at": ""})


# ── 경량 Naver 가격 조회 ──────────────────────────────────────────

def _fetch_product_info(url: str, timeout: int = 12) -> dict:
    """
    URL → {"name": str, "price": int, "soldout": bool, "error": str}

    1순위) BrightData Web Unlocker (봇 차단 우회)
    2순위) 직접 requests (BrightData 키 없을 때 fallback)
    __PRELOADED_STATE__ 우선 파싱 → meta tag fallback.
    """
    # ── 1순위: BrightData Web Unlocker ───────────────────────────
    html = _brightdata_fetch(url, timeout=90)

    # ── 2순위: 직접 요청 (BrightData 키 없거나 실패 시) ──────────
    if html is None:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=timeout, verify=False)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            return {"name": "", "price": 0, "soldout": False, "error": str(e)}

    # ── __PRELOADED_STATE__ 파싱 ─────────────────────────────────
    raw: Optional[dict] = None

    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{)", html)
    if m:
        try:
            dec = json.JSONDecoder()
            raw, _ = dec.raw_decode(
                re.sub(r"\bundefined\b", "null", html[m.start(1):])
            )
        except Exception:
            raw = None

    if raw is None:
        m2 = re.search(
            r"window\.__PRELOADED_STATE__\s*=\s*JSON\.parse\((['\"])(.+?)\1\)",
            html,
        )
        if m2:
            try:
                raw = json.loads(json.loads(f'"{m2.group(2)}"'))
            except Exception:
                raw = None

    if raw is None:
        import bs4
        soup = bs4.BeautifulSoup(html, "html.parser")
        tag  = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag:
            try:
                raw = json.loads(tag.string or "")
            except Exception:
                raw = None

    if raw:
        node    = _find_node(raw)
        name    = _get_name(node)
        price   = _get_price(node)
        soldout = _is_soldout(node, raw)
        # 즉시할인가 교정: node에서 salePrice(정상가)를 가져온 경우
        # raw 전체에서 discountedSalePrice 탐색 후 더 낮은 값으로 교정
        if price > 0:
            discounted = _find_discounted_price(raw, price)
            if discounted > 0:
                price = discounted
            return {"name": name, "price": price, "soldout": soldout, "error": ""}

    # ── meta tag fallback ─────────────────────────────────────────
    import bs4
    soup = bs4.BeautifulSoup(html, "html.parser")

    def _meta(prop: str) -> str:
        t = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return (t.get("content", "") if t else "").strip()

    name  = _meta("og:title") or _meta("product:title") or ""
    price_str = _meta("kakao:commerce:price") or _meta("product:price:amount") or ""
    soldout_str = _meta("product:availability") or ""
    price = 0
    try:
        price = int(re.sub(r"[^\d]", "", price_str))
    except Exception:
        pass
    soldout = "out" in soldout_str.lower()

    if price > 0:
        return {"name": name, "price": price, "soldout": soldout, "error": ""}

    return {"name": name or url, "price": 0, "soldout": False, "error": "가격 파싱 실패"}


def _find_node(raw: dict) -> dict:
    def has(d):
        for k in _SIGNALS:
            v = d.get(k)
            if v is not None and v != 0 and v != "":
                return True
        return False

    PRIORITY = (
        "simpleProductForDetailPage", "product", "ProductInfo",
        "currentProduct", "productDetail", "catalog",
        "item", "pageProps", "props", "data",
    )
    PSET = set(PRIORITY)

    def search(d, depth):
        if not isinstance(d, dict) or depth > 7:
            return None
        if has(d):
            return d
        for k in PRIORITY:
            r = search(d.get(k), depth + 1)
            if r:
                return r
        for k, v in d.items():
            if k not in PSET:
                r = search(v, depth + 1)
                if r:
                    return r
        return None

    return search(raw, 0) or raw


def _get_name(node: dict) -> str:
    for k in ("name", "productName", "catalogName", "itemName", "displayName"):
        if node.get(k):
            return str(node[k]).strip()
    return ""


def _get_price(node: dict) -> int:
    """
    즉시할인 적용 실판매가 반환 (크롤러와 동일 우선순위).
    discountedSalePrice > sellingPrice > price > salePrice
    salePrice는 취소선 정상가일 수 있으므로 최후순위.
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


def _find_discounted_price(raw: dict, sale_price: int, depth: int = 0) -> int:
    """
    raw 전체를 재귀 탐색해 즉시할인 실판매가를 찾는다.
    sale_price(정상가)보다 낮은 discountedSalePrice / sellingPrice 중 최댓값 반환.
    멤버십·쿠폰 할인가(customerPrice, benefitPrice)는 제외.
    """
    if depth > 7 or not isinstance(raw, dict):
        return 0
    DISCOUNT_KEYS = ("discountedSalePrice", "sellingPrice")
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
            sub = _find_discounted_price(v, sale_price, depth + 1)
            if sub:
                best = max(best, sub)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    sub = _find_discounted_price(item, sale_price, depth + 1)
                    if sub:
                        best = max(best, sub)
    return best


def _is_soldout(node: dict, raw: dict) -> bool:
    """품절 여부 판단 — 여러 키/패턴 종합."""
    # 직접 키 확인
    for d in (node, raw):
        if not isinstance(d, dict):
            continue
        for k in ("stockQuantity", "stock", "availableStock"):
            v = d.get(k)
            if v is not None:
                try:
                    if int(v) == 0:
                        return True
                except Exception:
                    pass
        for k in ("outOfStock", "isSoldOut", "soldOut", "unavailable"):
            if d.get(k):
                return True
        status = str(d.get("saleStatus", "") or d.get("status", "") or "").upper()
        if status in ("SOLD_OUT", "OUTOFSTOCK", "UNAVAILABLE", "DELETED"):
            return True

    # 재귀 탐색 (depth 제한)
    def _deep(obj, depth=0):
        if depth > 4 or not isinstance(obj, dict):
            return False
        for k, v in obj.items():
            kl = k.lower()
            if "soldout" in kl or "outofstock" in kl:
                if v is True or str(v).upper() in ("TRUE", "Y", "1"):
                    return True
            if isinstance(v, dict):
                if _deep(v, depth + 1):
                    return True
        return False

    return _deep(raw)


# ── 공개 API ──────────────────────────────────────────────────────

def add_watch(url: str, store: str = "샵케이") -> PriceWatch:
    """URL을 감시 목록에 추가. 이미 있으면 기존 반환."""
    watches = _load()
    url = url.strip()
    for w in watches:
        if w.url == url:
            return w
    pw = PriceWatch(uid=uuid.uuid4().hex[:8], url=url, store=store)
    watches.append(pw)
    _save(watches)
    return pw


def add_watch_with_info(
    url: str,
    name: str = "",
    base_price: int = 0,
    store: str = "샵케이",
) -> PriceWatch:
    """
    URL 감시 등록 + 상품명/기준가/스토어 즉시 반영.
    이미 등록된 URL이면 기존 항목 반환 (name/price 덮어쓰지 않음).
    """
    watches = _load()
    url = url.strip()
    for w in watches:
        if w.url == url:
            return w   # 기존 항목 그대로 유지
    pw = PriceWatch(uid=uuid.uuid4().hex[:8], url=url, store=store)
    if name:
        pw.name = name
    if base_price > 0:
        pw.base_price    = base_price
        pw.current_price = base_price
    watches.append(pw)
    _save(watches)
    return pw


def remove_watch(uid: str) -> None:
    watches = [w for w in _load() if w.uid != uid]
    _save(watches)


def all_watches() -> list[PriceWatch]:
    return _load()


def check_one(pw: PriceWatch) -> PriceWatch:
    """단일 상품 가격/품절 체크 → PriceWatch 갱신."""
    info = _fetch_product_info(pw.url)
    now  = _now_str()

    if info["error"] and not info["price"]:
        print(f"[PriceChecker] 조회 실패 ({pw.uid}): {info['error']}")
        pw.last_checked = now
        return pw

    new_price = info["price"]
    soldout   = info["soldout"]
    name      = info["name"] or pw.name

    _old_current = pw.current_price   # 갱신 전 current_price 보존

    pw.name          = name
    pw.last_checked  = now
    pw.current_price = new_price

    # 히스토리 추가 (최대 200건)
    # acked=True 상태에서 품절 억제 중이면 soldout=False 로 기록
    # → 이후 재입고 체크 시 _was_soldout=True 오탐 방지
    _record_soldout = soldout and not pw.acked
    pw.history.append({"ts": now, "price": new_price, "soldout": _record_soldout})
    if len(pw.history) > 200:
        pw.history = pw.history[-200:]

    # base_price 초기화:
    #   ① 처음 등록 후 아직 체크 안 된 항목 (base_price == 0)
    #   ② 이전에 current_price를 한 번도 가져오지 못한 항목 (_old_current == 0)
    #      → 과거 잘못된 base_price(쿠팡 판매가 등)가 저장됐을 수 있으므로 실제 네이버가로 교정
    if (pw.base_price == 0 or _old_current == 0) and new_price > 0:
        pw.base_price = new_price

    # 상태 판정
    if soldout:
        if pw.acked:
            # 확인완료 처리된 품절 → 재알림 억제
            pw.status = "ok"
            pw.change = 0
        else:
            pw.status = "soldout"
            pw.change = 0
    elif pw.acked:
        # acked=True 상태에서 → 실제로 품절 이력이 있을 때만 재입고 처리
        # 품절 아닌 알림(fallen/risen)을 확인완료 했을 때 오탐 방지
        _was_soldout = any(h.get("soldout") for h in pw.history[-10:])
        if _was_soldout:
            pw.status = "restocked"
            pw.acked  = False
            pw.base_price = new_price  # 재입고 시점 가격을 새 기준가로
            pw.change = 0
        else:
            # 품절 이력 없음 → acked 해제 후 일반 가격 비교
            pw.acked = False
            if pw.base_price > 0 and new_price > 0:
                pw.change = new_price - pw.base_price
                if pw.change > 0:
                    pw.status = "risen"
                elif pw.change < 0:
                    pw.status = "fallen"
                else:
                    pw.status = "ok"
            else:
                pw.status = "ok"
                pw.change = 0
    elif new_price > 0 and pw.base_price > 0:
        pw.change = new_price - pw.base_price
        if pw.change > 0:
            pw.status = "risen"
        elif pw.change < 0:
            pw.status = "fallen"
        else:
            pw.status = "ok"
    else:
        pw.status = "ok"
        pw.change = 0

    print(
        f"[PriceChecker] {name[:30]} | "
        f"{pw.base_price:,}→{new_price:,}원 | "
        f"{'품절' if soldout else f'변동 {pw.change:+,}원'} | {pw.status}"
    )
    return pw


def check_all(log_cb=None, max_workers: int = 5) -> dict:
    """
    전체 감시 목록 일괄 체크 (병렬 처리).

    - max_workers개 동시 요청으로 속도 향상
    - 항목마다 즉시 저장 → 중단돼도 진행분 보존
    - log_cb(msg, done, total) 형태로 진행 상황 전달

    Returns:
        {"risen": int, "fallen": int, "soldout": int, "ok": int, "errors": int,
         "done": int, "total": int}
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    watches = _load()
    total   = len(watches)
    if not watches:
        return {"risen": 0, "fallen": 0, "soldout": 0, "ok": 0, "errors": 0,
                "done": 0, "total": 0}

    # 체크 시작 상태 저장 (앱 종료 시 중단 감지용)
    _save(watches, check_state={
        "in_progress": True,
        "done": 0,
        "total": total,
        "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })

    result  = {"risen": 0, "fallen": 0, "soldout": 0, "ok": 0, "errors": 0,
               "done": 0, "total": total}
    uid_map = {pw.uid: i for i, pw in enumerate(watches)}
    lock    = threading.Lock()

    def _do_one(pw):
        try:
            updated = check_one(pw)
            return updated, None
        except Exception as e:
            return pw, e

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_do_one, pw): pw for pw in watches}
        for fut in as_completed(futures):
            updated_pw, err = fut.result()
            with lock:
                idx = uid_map[updated_pw.uid]
                # 레이스 컨디션 방지: 디스크의 acked 값을 우선 적용
                # (체크 도중 사용자가 확인완료 누르면 그 값을 보존)
                try:
                    current_on_disk = _load()
                    disk_map = {w.uid: w for w in current_on_disk}
                    if updated_pw.uid in disk_map:
                        disk_acked = disk_map[updated_pw.uid].acked
                        if disk_acked and not updated_pw.acked:
                            updated_pw.acked = True
                            if updated_pw.status in ("soldout", "risen", "fallen"):
                                updated_pw.status = "ok"
                                updated_pw.change = 0
                except Exception:
                    pass
                watches[idx] = updated_pw
                if err:
                    result["errors"] += 1
                    print(f"[PriceChecker] 오류 ({updated_pw.uid}): {err}")
                else:
                    result[updated_pw.status] = result.get(updated_pw.status, 0) + 1
                result["done"] += 1
                done = result["done"]
                _save(watches, check_state={
                    "in_progress": True,
                    "done": done,
                    "total": total,
                    "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                })

            name_short = (updated_pw.name or updated_pw.url)[:30]
            msg = (
                f"[가격체크 {done}/{total}] {name_short} "
                f"→ {updated_pw.status}"
                + (f" ({updated_pw.change:+,}원)" if updated_pw.change else "")
            )
            print(msg)
            if log_cb:
                try:
                    log_cb(msg, done, total)
                except TypeError:
                    log_cb(msg)   # 구형 호출부 호환

    # 체크 완료 상태 저장
    _save(watches, check_state={
        "in_progress": False,
        "done": total,
        "total": total,
        "started_at": "",
    })

    msg = (
        f"[가격체크] 완료 — 상승:{result['risen']} 하락:{result['fallen']} "
        f"품절:{result['soldout']} 정상:{result['ok']} 오류:{result['errors']}"
    )
    print(msg)
    if log_cb:
        try:
            log_cb(msg, total, total)
        except TypeError:
            log_cb(msg)
    return result


def reset_base_price(uid: str) -> None:
    """기준가를 현재가로 리셋 (사용자가 '확인 완료' 처리 시). 재알림 억제 플래그 설정."""
    watches = _load()
    for w in watches:
        if w.uid == uid:
            w.base_price = w.current_price
            w.status     = "ok"
            w.change     = 0
            w.acked      = True  # 다음 체크에서 같은 상태로 재알림 억제
            # 재입고 확인완료 시 → 히스토리 품절 기록 초기화
            # (남아있으면 다음 체크 때 acked=True + 품절이력 → 재입고 재알림 반복되는 버그 방지)
            if hasattr(w, "history") and isinstance(w.history, list):
                for h in w.history:
                    if isinstance(h, dict) and h.get("soldout"):
                        h["soldout"] = False
    _save(watches)


def reset_all_base_prices() -> int:
    """전체 base_price를 current_price로 리셋 (오탐 일괄 정리용).
    품절(soldout) 항목은 건드리지 않음.
    """
    watches = _load()
    count = 0
    for w in watches:
        if w.status == "soldout":
            continue
        if w.current_price > 0:
            w.base_price = w.current_price
            w.status     = "ok"
            w.change     = 0
            count += 1
    _save(watches)
    print(f"[PriceChecker] 전체 기준가 리셋 완료: {count}개 (품절 항목 제외)")
    return count


def has_alerts() -> bool:
    """알림 대상(상승/하락/품절) 항목 존재 여부."""
    return any(w.status in ("risen", "fallen", "soldout") for w in _load())


def alert_counts() -> dict:
    """{"risen": N, "fallen": N, "soldout": N, "restocked": N}"""
    counts = {"risen": 0, "fallen": 0, "soldout": 0, "restocked": 0}
    for w in _load():
        if w.status in counts:
            counts[w.status] += 1
    return counts


# ── 스케줄러 ─────────────────────────────────────────────────────

async def _seconds_until_next(hours: list[int]) -> float:
    """지정 시각(시) 중 가장 가까운 다음 시각까지 초 계산."""
    now   = datetime.datetime.now()
    times = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in hours]
    future = [t + datetime.timedelta(days=1) if t <= now else t for t in times]
    nxt   = min(future)
    delta = (nxt - now).total_seconds()
    return max(delta, 1)


def _send_price_alert(result: dict, watches: list) -> None:
    """
    가격 체크 후 이상 항목(상승/하락/품절)을 텔레그램으로 발송.
    정상 항목만 있으면 발송하지 않음.
    """
    try:
        from modules.notifier import send_notification
    except Exception:
        return

    risen   = [w for w in watches if w.status == "risen"]
    fallen  = [w for w in watches if w.status == "fallen"]
    soldout = [w for w in watches if w.status == "soldout"]

    if not risen and not fallen and not soldout:
        return  # 이상 없음 → 발송 안 함

    lines = ["📊 <b>[가격변동 알림]</b>"]

    if risen:
        lines.append(f"\n📈 <b>가격 상승 {len(risen)}건</b>")
        for w in risen[:5]:  # 최대 5개
            pct = ((w.current_price - w.base_price) / w.base_price * 100) if w.base_price else 0
            store_tag = f"[{w.store}] " if getattr(w, "store", "") else ""
            lines.append(
                f"  • {store_tag}{w.name[:25]}\n"
                f"    {w.base_price:,}원 → {w.current_price:,}원 (+{w.change:,}원, {pct:+.1f}%)"
            )
        if len(risen) > 5:
            lines.append(f"  ...외 {len(risen)-5}건")

    if fallen:
        lines.append(f"\n📉 <b>가격 하락 {len(fallen)}건</b>")
        for w in fallen[:5]:
            pct = ((w.current_price - w.base_price) / w.base_price * 100) if w.base_price else 0
            store_tag = f"[{w.store}] " if getattr(w, "store", "") else ""
            lines.append(
                f"  • {store_tag}{w.name[:25]}\n"
                f"    {w.base_price:,}원 → {w.current_price:,}원 ({w.change:,}원, {pct:+.1f}%)"
            )
        if len(fallen) > 5:
            lines.append(f"  ...외 {len(fallen)-5}건")

    if soldout:
        lines.append(f"\n🚫 <b>품절 {len(soldout)}건</b>")
        for w in soldout[:5]:
            store_tag = f"[{w.store}] " if getattr(w, "store", "") else ""
            lines.append(f"  • {store_tag}{w.name[:25]}")
        if len(soldout) > 5:
            lines.append(f"  ...외 {len(soldout)-5}건")

    lines.append(f"\n🕐 체크 시각: {_now_str()[:16]}")
    msg = "\n".join(lines)

    ok = send_notification(msg)
    if ok:
        print("[PriceChecker] 텔레그램 알림 발송 완료")
    else:
        print("[PriceChecker] 텔레그램 알림 발송 실패 (설정 확인)")


async def run_scheduler(log_cb=None) -> None:
    """
    00:00 / 12:00 에 check_all() 실행하는 무한 asyncio 태스크.
    앱 시작 시 asyncio.create_task(run_scheduler()) 로 시작.
    이상 항목(상승/하락/품절) 발생 시 텔레그램 알림 발송.
    """
    CHECK_HOURS = [0, 12]
    print("[PriceChecker] 스케줄러 시작 (00:00 / 12:00 체크)")
    while True:
        wait = await _seconds_until_next(CHECK_HOURS)
        h, m = divmod(int(wait), 3600)
        print(f"[PriceChecker] 다음 체크까지 {h}시간 {m//60}분 대기")
        await asyncio.sleep(wait)
        print(f"[PriceChecker] 정기 체크 시작: {_now_str()}")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: check_all(log_cb))
        # 체크 완료 후 이상 항목 텔레그램 발송
        watches = _load()
        await loop.run_in_executor(None, lambda: _send_price_alert({}, watches))
