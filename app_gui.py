"""
네이버 → 쿠팡 일괄등록 자동화 GUI  (v2 — 엑셀 일괄등록 방식)

흐름:
  1. 네이버 SmartStore URL 큐에 추가 (브랜드·수량·용량 지정)
  2. [전체 처리 시작] 클릭
       → 크롤링 → 이미지 누끼+합성 → Cloudflare R2 이미지 URL 획득 → 가격 산출
  3. [엑셀 생성 + 다운로드] → 쿠팡 Wing 일괄등록 페이지에 업로드
"""
from __future__ import annotations

import asyncio
import hashlib as _hashlib
import io
import json
import os
import re as _re
import socket
import subprocess
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

from nicegui import app as _app, ui

from config.settings import Settings
from modules.crawler import NaverStoreCrawler, ProductData
from modules.image_processor import ImageProcessor
from modules.price_calculator import PriceCalculator
from modules.image_uploader import upload_pil, upload_file, upload_url
from modules.excel_builder import ExcelBuilder, BulkItem, Bundle
from modules.category_detector import get_detector as _get_detector
from modules.gtin_lookup import lookup as _gtin_lookup, lookup_with_retry as _gtin_lookup_retry
from modules.wing_automator import run_bulk_publish_sync as _run_bulk_publish
from modules.gemini_writer import generate_detail_image_url as _gemini_detail
from modules.coupang_registrar import CoupangRegistrar
import modules.price_checker as _pc
from modules.notifier import (
    send_notification as _send_notification,
    send_notification_with_register_button as _send_notification_with_btn,
    poll_register_callback as _poll_register_callback,
)
from modules.product_history import (
    add_to_history as _add_to_history,
    run_duplicate_check as _run_duplicate_check,
    get_history_count as _get_history_count,
)

# ── URL 추가 성공 시 "띠링" 소리 (Web Audio API, 외부 파일 불필요) ──
_DING_JS = """
(function() {
    try {
        var ctx = new (window.AudioContext || window.webkitAudioContext)();
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(880, ctx.currentTime);
        gain.gain.setValueAtTime(0.45, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);
        osc.start(ctx.currentTime);
        osc.stop(ctx.currentTime + 0.45);
    } catch(e) {}
})();
"""

# ── 정적 파일 서빙 ─────────────────────────────────────────────────
_IMG_ROOT    = Path(__file__).parent / "data" / "images"
_OUTPUT_ROOT = Path(__file__).parent / "data" / "output"
_TMPL_ROOT   = Path(__file__).parent / "data" / "templates"
_BACKUP_ROOT = Path(__file__).parent / "data" / "backup"
for _d in (_IMG_ROOT, _OUTPUT_ROOT, _TMPL_ROOT, _BACKUP_ROOT):
    _d.mkdir(parents=True, exist_ok=True)
_app.add_static_files("/img",    str(_IMG_ROOT))
_app.add_static_files("/output", str(_OUTPUT_ROOT))

# ── 상세페이지 탭 — 클립보드 붙여넣기 큐 (단일 사용자 앱) ────────────
_DP_PASTE_QUEUE: list[str] = []   # FastAPI 엔드포인트 → UI 타이머로 전달

from fastapi import UploadFile, File as _FastAPIFile, Request as _FastAPIRequest

@_app.post("/api/dp/paste_upload")
async def _dp_paste_upload_endpoint(file: UploadFile = _FastAPIFile(...)):
    """클립보드 이미지 blob을 받아 로컬에 저장 후 경로를 큐에 넣음."""
    import tempfile as _tf
    suffix = ".png"
    tmp    = _tf.NamedTemporaryFile(
        delete=False, suffix=suffix, dir=str(_IMG_ROOT)
    )
    tmp.write(await file.read())
    tmp.close()
    _DP_PASTE_QUEUE.append(tmp.name)
    return {"ok": True, "path": tmp.name}


@_app.post("/api/dp/url_upload")
async def _dp_url_upload_endpoint(request: _FastAPIRequest):
    """URL 이미지를 서버가 직접 다운로드 → 큐에 넣음 (CORS 우회)."""
    import tempfile as _tf2
    import urllib.request as _urlreq2
    try:
        data = await request.json()
        url  = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return {"ok": False, "error": "invalid url"}
        ext = ".jpg"
        for _e in (".png", ".webp", ".gif"):
            if _e in url.lower():
                ext = _e; break
        tmp = _tf2.NamedTemporaryFile(delete=False, suffix=ext, dir=str(_IMG_ROOT))
        tmp.close()
        req = _urlreq2.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://smartstore.naver.com/"})
        with _urlreq2.urlopen(req, timeout=15) as resp:
            with open(tmp.name, "wb") as f:
                f.write(resp.read())
        _DP_PASTE_QUEUE.append(tmp.name)
        return {"ok": True, "path": tmp.name}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


@_app.post("/api/extra-img/upload")
async def _extra_img_upload_endpoint(file: UploadFile = _FastAPIFile(...)):
    """추가자료 다이얼로그 클립보드/파일 이미지 → R2 업로드 → URL 반환."""
    try:
        import uuid as _uuid2
        _data = await file.read()
        _ct   = file.content_type or "image/png"
        _ext  = "webp" if "webp" in _ct else "png" if "png" in _ct else "jpg"
        _fname = f"extra_{_uuid2.uuid4().hex[:8]}.{_ext}"
        from modules.image_uploader import _do_upload as _r2up2
        _url = _r2up2(_data, _fname, _ct)
        if _url:
            return {"ok": True, "url": _url}
        return {"ok": False, "error": "R2 업로드 실패"}
    except Exception as _e2:
        return {"ok": False, "error": str(_e2)}


@_app.post("/api/extra-img/url-upload")
async def _extra_img_url_upload_endpoint(request: _FastAPIRequest):
    """URL 이미지를 서버가 다운로드 → R2 업로드 → URL 반환 (CORS 우회)."""
    import tempfile as _tf3, urllib.request as _urlreq3, uuid as _uuid3
    try:
        data = await request.json()
        url  = (data.get("url") or "").strip()
        if not url.startswith("http"):
            return {"ok": False, "error": "invalid url"}
        req = _urlreq3.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://smartstore.naver.com/",
        })
        with _urlreq3.urlopen(req, timeout=15) as resp:
            _raw  = resp.read()
            _ct   = resp.headers.get("Content-Type", "image/jpeg")
        _ext  = "webp" if "webp" in _ct else "png" if "png" in _ct else "jpg"
        _fname = f"extra_{_uuid3.uuid4().hex[:8]}.{_ext}"
        from modules.image_uploader import _do_upload as _r2up3
        _r2url = _r2up3(_raw, _fname, _ct)
        if _r2url:
            return {"ok": True, "url": _r2url}
        return {"ok": False, "error": "R2 업로드 실패"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

# ── 로그인 인증 (미들웨어 방식 — NiceGUI 전역 UI와 충돌 없음) ─────────
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware
from starlette.requests import Request as _Request
from starlette.responses import RedirectResponse as _RedirectResponse, HTMLResponse as _HTMLResponse

_APP_USERNAME = os.getenv("APP_USERNAME", "admin")
_APP_PASSWORD = os.getenv("APP_PASSWORD", "")
_AUTH_COOKIE  = "nc_auth"

def _make_token(u: str, p: str) -> str:
    return _hashlib.sha256(f"{u}:{p}:naver-coupang".encode()).hexdigest()

_LOGIN_HTML = """<!DOCTYPE html><html lang="ko"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>로그인 — 네이버→쿠팡</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f172a;display:flex;align-items:center;
     justify-content:center;min-height:100vh}
.card{background:#1e293b;border-radius:16px;padding:40px;
      width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
h1{color:#f1f5f9;font-size:22px;font-weight:600;text-align:center;margin-bottom:6px}
.sub{color:#94a3b8;font-size:13px;text-align:center;margin-bottom:28px}
label{display:block;color:#94a3b8;font-size:13px;margin-bottom:6px;margin-top:16px}
input{width:100%;padding:12px 14px;background:#0f172a;border:1px solid #334155;
      border-radius:8px;color:#f1f5f9;font-size:15px;outline:none;transition:border .2s}
input:focus{border-color:#38bdf8}
.err{color:#f87171;font-size:13px;margin-top:14px;text-align:center}
button{width:100%;margin-top:24px;padding:13px;background:#0284c7;border:none;
       border-radius:8px;color:#fff;font-size:15px;font-weight:600;
       cursor:pointer;transition:background .2s}
button:hover{background:#0369a1}
</style></head><body>
<div class="card">
  <h1>&#x1F6D2; 네이버 → 쿠팡</h1>
  <p class="sub">자동 등록 파이프라인</p>
  <form method="POST" action="/login">
    <label>아이디</label>
    <input type="text" name="username" autocomplete="username" autofocus>
    <label>비밀번호</label>
    <input type="password" name="password" autocomplete="current-password">
    {error}
    <button type="submit">로그인</button>
  </form>
</div></body></html>"""

class _AuthMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request: _Request, call_next):
        path = request.url.path

        # 로그인 POST 처리
        if request.method == "POST" and path == "/login":
            form = await request.form()
            u = (form.get("username") or "").strip()
            p = (form.get("password") or "").strip()
            if u == _APP_USERNAME and p == _APP_PASSWORD:
                resp = _RedirectResponse("/", status_code=303)
                resp.set_cookie(_AUTH_COOKIE, _make_token(u, p),
                                httponly=True, max_age=86400 * 30, samesite="lax")
                return resp
            err = '<p class="err">아이디 또는 비밀번호가 틀렸습니다.</p>'
            return _HTMLResponse(_LOGIN_HTML.replace("{error}", err))

        # 로그인 GET 처리
        if path == "/login":
            return _HTMLResponse(_LOGIN_HTML.replace("{error}", ""))

        # 로그아웃 처리
        if path == "/logout":
            resp = _RedirectResponse("/login", status_code=303)
            resp.delete_cookie(_AUTH_COOKIE)
            return resp

        # NiceGUI 내부 경로, API 엔드포인트는 인증 면제
        if (path.startswith("/_nicegui") or path == "/favicon.ico"
                or path.startswith("/api/dp/")):
            return await call_next(request)

        # 인증 확인
        if _APP_PASSWORD:
            token = request.cookies.get(_AUTH_COOKIE, "")
            if token != _make_token(_APP_USERNAME, _APP_PASSWORD):
                return _RedirectResponse("/login")

        return await call_next(request)

_app.add_middleware(_AuthMiddleware)

_settings = Settings()

_registrar_instance: Optional[CoupangRegistrar] = None

def _get_registrar() -> CoupangRegistrar:
    global _registrar_instance
    if _registrar_instance is None:
        _registrar_instance = CoupangRegistrar(_settings)
    return _registrar_instance

# ── 쿠팡 카테고리별 유효 구매옵션 + gosisi_cat 데이터 ─────────────────
_CAT_OPTIONS_PATH = Path(__file__).parent / "config" / "category_options.json"
try:
    import json as _json
    with open(_CAT_OPTIONS_PATH, encoding="utf-8") as _f:
        _CAT_OPTIONS: dict[str, dict] = _json.load(_f)
except Exception:
    _CAT_OPTIONS = {}

def _valid_option_types(category_id: str) -> list[str]:
    """해당 카테고리에서 허용된 구매옵션 유형 목록 반환. 없으면 빈 리스트."""
    entry = _CAT_OPTIONS.get(category_id, {})
    return entry.get("valid_options", [])

def _guide_gosisi_cat(category_id: str) -> str:
    """가이드 파일 기준 gosisi_cat 반환."""
    return _CAT_OPTIONS.get(category_id, {}).get("gosisi_cat", "")

# ── SSH 터널 (선택 기능 — Coupang API 직접 호출 필요 시) ──────────
_SSH_PORT = getattr(_settings, "SOCKS5_PORT", 1081)
_ssh_proc: subprocess.Popen | None = None

def _is_tunnel_alive() -> bool:
    try:
        s = socket.create_connection(("127.0.0.1", _SSH_PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False

def _ensure_ssh_tunnel() -> bool:
    global _ssh_proc
    if _is_tunnel_alive():
        return True
    key_path = Path(__file__).parent / "ssh_keys" / "SSH_KeyPair-260527213658.pem"
    vps_host = getattr(_settings, "VPS_HOST", "")
    if not key_path.exists() or not vps_host:
        return False
    try:
        _ssh_proc = subprocess.Popen(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ServerAliveInterval=30",
             "-o", "ConnectTimeout=10", "-N", "-D", str(_SSH_PORT),
             "-i", str(key_path), f"ubuntu@{vps_host}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import time
        for _ in range(16):
            time.sleep(0.5)
            if _is_tunnel_alive():
                return True
    except Exception:
        pass
    return False


# ── UI 로그 스트림 ────────────────────────────────────────────────
class _UIStream(io.StringIO):
    def __init__(self, log: ui.log, buffer: list[str] | None = None) -> None:
        super().__init__()
        self._log    = log
        self._real   = sys.__stdout__
        self._buffer = buffer   # 외부 공유 버퍼 — 복사 버튼이 읽음
    def write(self, text: str) -> int:
        self._real.write(text)
        line = text.strip()
        if line:
            if self._buffer is not None:
                self._buffer.append(line)
            try:
                self._log.push(line)
            except RuntimeError:
                pass
        return len(text)
    def flush(self) -> None:
        self._real.flush()


# ── 큐 데이터 모델 ───────────────────────────────────────────────

@dataclass
class QueueEntry:
    uid:            str
    url:            str
    brand:          str            = ""
    qtys:           list[int]      = field(default_factory=lambda: [1])
    volume:         float          = 0.0
    volume_unit:    str            = "L"
    # origin 제거 — 엑셀 미사용 필드, BulkItem에 "수입산" 고정값 전달
    status:         str            = "pending"   # pending / processing / done / error
    product_name:   str            = ""
    error:          str            = ""
    result_item:    Optional[BulkItem] = None
    manual_options:    list[tuple[str, str]] = field(default_factory=list)
    gosisi_cat:        str = "기타 재화"  # 상품고시정보 카테고리 (Wing 드롭다운 값)
    detected_keyword:  str = ""           # 자동감지에 사용된 키워드 (표시용)
    category_id:       str = ""           # 쿠팡 카테고리 ID (자동감지 or 수동입력)
    category_is_manual: bool = False     # True = 사용자가 직접 입력/선택 → 재처리 시 덮어쓰기 금지
    qty_locked:        bool = False       # True = 사용자가 수동으로 수량 지정 → L 자동계산 스킵
    brand_locked:      bool = False       # True = 사용자가 직접 브랜드 입력 → 처리 중 덮어쓰기 금지
    min_qty:           int  = 1           # 최솟값 (기본 1개, 수정 가능)
    draft:             bool = False       # True = Wing 임시저장 (판매시작일 공란 → 상세페이지 직접 수정)
    use_nobg:          bool = False       # 항목별 누끼 설정 (파일 업로드 시점에 저장)
    gtin:              str  = ""          # 바코드(GTIN) — GS1 Korea 자동조회 or 수동입력
    naver_price:       int  = 0           # 네이버 크롤링 원가(1개 기준) — 가격감시 기준가용
    source_file:       str  = ""          # 출처 파일명 (.txt) or "" (단일 URL 직접 입력)
    lead_time:         int  = 2          # 출고리드타임 (국내 2일 / 해외 10일) — 처리 후 전환 가능
    lead_time_locked:  bool = False      # True = 뱃지 직접 토글 시 — 글로벌 패널값 무시하고 개별값 사용
    # ── 중복 감지 결과 ──────────────────────────────────────────────
    dup_status:        str  = ""         # "duplicate" | "variant" | "unknown" | "" (이상 없음)
    dup_reason:        str  = ""         # Gemini 판별 이유 (표시용)
    dup_matched_name:  str  = ""         # 매칭된 이력 상품명
    dup_matched_date:  str  = ""         # 매칭된 이력 상품 등록일
    # ── 단일 등록 전용 ────────────────────────────────────────────────
    single_mode:           bool       = False                       # True = 단일 등록 탭에서 추가된 항목
    single_selected_imgs:  list[str]  = field(default_factory=list) # 사용자가 선택한 네이버 상세 이미지 URL 목록
    margin_override:       float      = 0.0                         # >0 이면 글로벌 마진율 대신 이 값 사용
    watch_store:          str        = "샵케이"  # 가격감시 등록 스토어 구분 ("샵케이" / "제니스 트레이딩")
    price_extra:          int        = 0          # 관부가세 등 추가금액 (원) — 모든 옵션/수량 판매가에 합산
    extra_detail_images:  list[str]  = field(default_factory=list)  # 상세 이미지 하단 추가 이미지 URL 목록
    extra_detail_text:    str        = ""          # 상세 이미지 하단 추가 텍스트 (이미지로 렌더링)
    # 사용자가 직접 지정한 추가 옵션 (자동 추출 옵션 뒤에 병합됨)
    # 예: [("색상", "블랙"), ("사이즈", "M")]


# ── 브랜드 자동 추출 ─────────────────────────────────────────────
_KNOWN_BRANDS = [
    # 자동차/오일
    "모빌원", "Mobil 1", "Mobil1", "모빌", "Mobil",
    "쉘 히릭스", "Shell Helix", "쉘", "Shell",
    "카스트롤", "Castrol", "발보린", "Valvoline",
    "지크", "ZIC", "오일뱅크", "S-OIL",
    "토탈", "Total", "TotalEnergies",
    "리퀴몰리", "Liqui Moly", "BP", "Esso", "엑슨모빌",
    "현대오일뱅크", "GS칼텍스", "킥스", "Kixx",
    "ATE", "보쉬", "Bosch", "만필터", "Mann", "MANN",
    "말레", "MAHLE", "델박", "Delvac",
    # 식품/과자/음료
    "하리보", "Haribo",
    "롯데", "오리온", "해태", "크라운", "농심", "삼양",
    "메이지", "글리코", "부르봉", "노벨", "묘조",
    "아사히", "치보", "네스프레소", "네스카페",
    "이토엔", "산토리", "기린",
    # 뷰티/생활
    "존슨", "비오레", "판틴", "헤드앤숄더",
    "바디샵", "이솝", "크리니크",
    "배쓰앤바디웍스", "Bath & Body Works", "BBW",
    # 데오드란트/개인위생
    "파워스틱", "POWERSTICK", "Power Stick", "PowerStick",
    "도브", "Dove", "니베아", "Nivea", "올드스파이스", "Old Spice",
    "디그리", "Degree", "스피드스틱", "Speed Stick",
]

# ── 차량 suffix로 쓰이면 안 되는 연료·엔진·세대 타입 토큰 ────────────
# "올란도LPG" 오인식 방지: 공백 뒤 토큰이 이 목록에 있으면 suffix 무시
_FUEL_SUFFIX_TOKENS: frozenset[str] = frozenset({
    "LPG", "LPI", "LPE", "CNG", "GDI", "CVVT", "CDI", "HDI",
    "TDI", "TSI", "FSI", "PHEV", "HEV", "BEV", "EV", "CRD",
    "DTH", "VGT", "DOHC", "SOHC", "CRDI", "TGDI", "MPI",
})

# ── 차량 제조사 → 모델 키워드 매핑 ──────────────────────────────────
_CAR_MAKE_MODELS: dict[str, list[str]] = {
    "현대": [
        "아반떼", "소나타", "그랜저", "투싼", "싼타페", "팰리세이드", "코나",
        "벨로스터", "i30", "아이오닉", "넥쏘", "스타리아", "포터", "스타렉스",
        "제네시스", "갤로퍼", "테라칸", "베라크루즈",
    ],
    "기아": [
        "K3", "K5", "K7", "K8", "K9", "스포티지", "쏘렌토", "카니발",
        "셀토스", "니로", "EV6", "스팅거", "모하비", "봉고", "레이",
        "카렌스", "오피러스", "쎄라토", "모닝", "스토닉",
    ],
    "쉐보레": [
        "스파크", "크루즈", "말리부", "트레일블레이저", "트랙스",
        "이쿼녹스", "콜로라도", "카마로", "아베오", "라세티",
        "올란도", "캡티바", "임팔라", "볼트",
    ],
    "르노": [
        "QM3", "QM5", "QM6", "SM3", "SM5", "SM6", "SM7",
        "조에", "아르카나", "캡처",
    ],
    "쌍용": [
        "티볼리", "렉스턴", "코란도", "무쏘", "액티언", "카이런",
        "로디우스", "체어맨",
    ],
    "BMW": [
        "1시리즈", "2시리즈", "3시리즈", "4시리즈", "5시리즈",
        "6시리즈", "7시리즈", "8시리즈",
        "X1", "X2", "X3", "X4", "X5", "X6", "X7",
        "M3", "M5", "M8",
        "그란투리스모", "액티브투어러", "그란쿠페",
        "i3", "i4", "i5", "i7", "iX",
    ],
    "벤츠": [
        "A클래스", "B클래스", "C클래스", "E클래스", "S클래스",
        "CLA", "CLS", "GLA", "GLB", "GLC", "GLE", "GLS", "AMG",
    ],
    "아우디": [
        "A3", "A4", "A5", "A6", "A7", "A8",
        "Q3", "Q5", "Q7", "Q8", "TT", "R8", "e-tron",
    ],
    "폭스바겐": [
        "골프", "제타", "티구안", "파사트", "폴로", "아테온",
        "투아렉", "ID.4",
    ],
    "도요타": [
        "캠리", "RAV4", "프리우스", "코롤라", "하이랜더", "시에나", "탄드라",
    ],
    "렉서스": [
        "ES250", "ES300", "ES350",
        "IS250", "IS300", "IS350",
        "GS350", "GS450",
        "LS460", "LS500",
        "RX300", "RX350", "RX450",
        "NX200", "NX250", "NX350",
        "GX460", "GX550",
        "LX570", "LX600",
        "LC500",
        "UX200", "UX250",
        # 세대 표기 없는 단독 모델명 (반드시 긴 것 먼저 검색되도록 순서 유지)
        "ES", "IS", "GS", "LS", "RX", "NX", "LX", "LC", "UX",
    ],
    "혼다": [
        "어코드", "CR-V", "HR-V", "시빅", "파일럿", "오딧세이",
    ],
    "닛산": [
        "알티마", "맥시마", "무라노", "로그", "패스파인더", "아르마다",
    ],
    "볼보": [
        "XC40", "XC60", "XC90", "S60", "S90", "V60", "V90",
    ],
    "포드": [
        "익스플로러", "머스탱", "레인저", "F-150", "에지", "이스케이프",
    ],
    "지프": [
        "랭글러", "체로키", "그랜드체로키", "컴패스", "레니게이드",
    ],
    "포르쉐": [
        "카이엔", "마칸", "파나메라", "911", "박스터", "카이맨",
    ],
    "인피니티": [
        "Q50", "Q60", "Q70",
        "QX50", "QX55", "QX60", "QX70", "QX80",
        "G35", "G37",
        "M37", "M56",
        "FX35", "FX37", "FX50",
        "EX35", "EX37",
        "JX35",
    ],
}

# 역방향 색인: 모델 키워드(소문자) → 제조사
_MODEL_TO_MAKE: dict[str, str] = {}
for _make, _models in _CAR_MAKE_MODELS.items():
    for _m in _models:
        _MODEL_TO_MAKE[_m.lower().replace(" ", "")] = _make


def _extract_car_info(product_name: str) -> tuple[str, str]:
    """
    상품명에서 차종 + 제조사 추출.

    Returns:
        (car_model, car_make)  예: ("아반떼MD", "현대") / ("쏘렌토MQ4", "기아")
        미감지 시 ("", "")

    Suffix 추출 규칙:
      - 직접 붙은 경우(공백 없음): 영문만 허용. 한글은 절대 suffix로 취급 안 함.
        예) "아반떼MD가솔린" → "MD" / "그랜저IG 2.4" → "IG"
      - 공백으로 분리된 경우: 다음 토큰이 [A-Za-z][A-Za-z0-9]+ 이면 모델코드로 취급.
        예) "쏘렌토 MQ4 하이브리드" → "MQ4" / "카니발 KA4 디젤" → "KA4"
        단, "2.4 가솔린" 등 숫자 시작 or 한글 토큰은 모델코드로 보지 않음.
      - 영문·숫자 키워드(X7 등)는 앞에 영문/숫자가 붙으면 제외 (GX7→X7 오인식 방지)
    """
    best_model = ""
    best_make  = ""
    best_len   = 0
    name_lower = product_name.lower()

    for kw_norm, make in _MODEL_TO_MAKE.items():
        start = 0
        while True:
            pos = name_lower.find(kw_norm, start)
            if pos == -1:
                break

            # 영문/숫자로 시작하는 키워드: 앞 문자가 ASCII 영문/숫자면 단어 내부 → 제외
            # 예: "GX7"에서 "x7" 검색 시 pos=1, 앞 'g'(ASCII) → 건너뜀
            # 단, 앞이 한글(비ASCII)이면 새 단어 시작으로 허용 (예: "올뉴K7")
            if kw_norm and kw_norm[0].isascii() and kw_norm[0].isalnum():
                if pos > 0:
                    prev = name_lower[pos - 1]
                    if prev.isascii() and (prev.isalpha() or prev.isdigit()):
                        start = pos + 1
                        continue

            # 원본 대소문자 표기 복원
            kw_display = _CAR_MAKE_MODELS[make][
                [m.lower().replace(" ", "") for m in _CAR_MAKE_MODELS[make]].index(kw_norm)
            ]

            after = product_name[pos + len(kw_norm):]
            suffix = ""

            # ── 키워드 바로 뒤에 숫자가 붙으면 다른 코드의 일부 → 건너뜀 ──────
            # 예: "GX5"에서 "GX" 매칭 시 after[0]="5" → 렉서스GX 오인식 방지
            #     "Q50"에서 "Q5" 매칭 시 after[0]="0" → 아우디Q5 오인식 방지
            # (단, 한글 키워드는 after가 숫자여도 정상 매칭 허용 — "모닝1세대" 등)
            if kw_norm and kw_norm[-1].isascii() and after and after[0].isdigit():
                start = pos + 1
                continue

            if after and after[0] != " ":
                # ── 직접 붙은 suffix: 영문자로 시작하는 라틴 알파벳만 허용 ──
                # 한글(가-힣)로 시작하는 suffix는 절대 취하지 않음
                dm = _re.match(r'^([A-Za-z][A-Za-z0-9]*)', after)
                if dm:
                    raw = dm.group(1)
                    rem = after[len(raw):]
                    # 뒤에 소수점(배기량 2.4 등)이 오면 끝 숫자 제거
                    if rem.startswith(".") and raw[-1].isdigit():
                        raw = raw.rstrip("0123456789")
                    if raw:
                        suffix = raw.upper()

            elif after.startswith(" "):
                # ── 공백 뒤 모델코드 탐색 ─────────────────────────
                # [A-Za-z][A-Za-z0-9]{1,5} 형태의 토큰만 허용
                # (MQ4, KA4, YF, IG 등 — 한글·숫자시작·긴 영문단어 제외)
                sm = _re.match(
                    r"^ ([A-Za-z][A-Za-z0-9]{1,5})(?=\s|$|\[|\()",
                    after,
                )
                if sm:
                    candidate = sm.group(1)
                    # 순수 영문만이면서 일반 영단어(하이브리드 등)가 아닌 코드인지 확인
                    # → 길이 ≤ 5, 숫자 포함이거나 대문자 2개 이상이면 코드로 판단
                    is_code = (
                        any(c.isdigit() for c in candidate) or
                        sum(1 for c in candidate if c.isupper()) >= 2 or
                        len(candidate) <= 3
                    )
                    # ── 연료·엔진 타입 토큰은 suffix 제외 ────────────────────────
                    # 예: LPG·CNG·GDI → "올란도LPG" 오인식 방지
                    if candidate.upper() in _FUEL_SUFFIX_TOKENS:
                        is_code = False
                    # ── BMW·유럽형 세대 코드 (알파벳 1자+숫자 2자↑, 예: F45·G30·E90) ──
                    # 모델코드(MQ4·CN7)는 알파벳 2자+숫자 형태라 구별 가능
                    elif _re.fullmatch(r'[A-Z]\d{2,}', candidate.upper()):
                        is_code = False
                    if is_code:
                        suffix = candidate.upper()

            model_str = kw_display + suffix if suffix else kw_display

            if len(kw_norm) > best_len:
                best_model = model_str
                best_make  = make
                best_len   = len(kw_norm)
            break

    return best_model, best_make


def _auto_brand(name: str) -> str:
    import re
    nc = name.replace(" ", "").lower()
    for b in _KNOWN_BRANDS:
        if b.replace(" ", "").lower() in nc:
            return b
    # 첫 단어 추출 후 국가명/지역명이면 건너뜀
    _SKIP_WORDS = {
        # 국가/지역명
        "일본", "한국", "미국", "독일", "프랑스", "이탈리아", "영국", "중국",
        "호주", "캐나다", "스페인", "네덜란드", "스위스", "덴마크", "핀란드",
        "노르웨이", "스웨덴", "뉴질랜드", "태국", "베트남", "인도", "대만",
        # 상태/홍보
        "국내", "수입", "정품", "공식", "공식정품", "신제품", "최신",
        # 식품 일반명사
        "컵라면", "라면", "과자", "사탕", "초콜릿", "초코", "스낵", "캔디",
        "커피", "음료", "주스", "차", "물", "맥주", "와인", "소주",
        "과일", "채소", "육류", "생선", "해산물", "유제품", "빵", "케이크",
        "아이스크림", "젤리", "껌", "쿠키", "비스킷", "시리얼",
        # 뷰티/생활 일반명사
        "샴푸", "린스", "로션", "크림", "세럼", "에센스", "마스크", "팩",
        "치약", "칫솔", "비누", "세제", "섬유", "방향제",
        "데오드란트", "향수", "퍼퓸", "바디워시", "바디로션", "핸드크림", "풋크림",
        "헤어왁스", "헤어젤", "헤어스프레이", "헤어오일", "두피케어", "컨디셔너",
        "클렌저", "폼클렌징", "선크림", "선스틱", "선스프레이", "선블록",
        "아이크림", "립밤", "립글로스", "립스틱", "파운데이션", "쿠션",
        "기저귀", "물티슈", "생리대", "면도기", "면도크림", "면도젤",
        "토너", "미스트", "스프레이", "오일", "앰플", "젤", "왁스",
        "보충제", "영양제", "비타민", "유산균", "단백질", "프로틴",
        "세정제", "살균제", "소독제", "연고", "크림제", "패치",
        # 용도/대상 수식어 (브랜드 오인식 방지)
        "휴대용", "대용량", "소용량", "미니", "남성용", "여성용", "어린이용",
        "아기용", "유아용", "성인용", "노인용", "가정용", "업소용", "산업용",
        "전용", "공용", "다용도",
        # 기타
        "세트", "묶음", "패키지", "선물", "기프트",
    }
    tokens = re.split(r'[\s,，(（\[]', name)
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok in _SKIP_WORDS:
            continue
        # "하리보젤리" → "하리보": 토큰이 카테고리 접미사로 끝나면 접미사 제거
        for skip in sorted(_SKIP_WORDS, key=len, reverse=True):
            if len(tok) > len(skip) and tok.endswith(skip):
                tok = tok[: -len(skip)]
                break
        if tok:
            return tok
    return "해당없음"


# ── 브랜드 매핑 테이블 (누적 저장) ──────────────────────────────────
_BRAND_MAP_FILE = Path(__file__).parent / "data" / "brand_map.json"

_BRAND_MAP_INVALID_VALUES = {
    "unknown", "없음", "모름", "thought", "none", "null", "n/a", "error",
    "브랜드", "brand", "브랜드명", "제조사",
}

def _load_brand_map() -> dict:
    try:
        if _BRAND_MAP_FILE.exists():
            raw = json.loads(_BRAND_MAP_FILE.read_text(encoding="utf-8"))
            # Gemini 환각으로 오염된 값 자동 제거 (예: '맥심' → 'THOUGHT')
            cleaned = {
                k: v for k, v in raw.items()
                if v and v.strip().lower() not in _BRAND_MAP_INVALID_VALUES
            }
            if len(cleaned) != len(raw):
                _removed = {k: v for k, v in raw.items() if k not in cleaned}
                print(f"[BrandMap] 오염 항목 자동 제거: {_removed}")
                try:
                    _BRAND_MAP_FILE.write_text(
                        json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass
            return cleaned
    except Exception:
        pass
    return {}

def _save_brand_map(bmap: dict) -> None:
    try:
        _BRAND_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BRAND_MAP_FILE.write_text(
            json.dumps(bmap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"[BrandMap] 저장 오류: {e}")

def _is_english_brand(brand: str) -> bool:
    """브랜드명이 영문/숫자 위주인지 판별."""
    import re
    korean = len(re.findall(r'[가-힣]', brand))
    total  = len(brand.replace(" ", ""))
    return total > 0 and korean / total < 0.3   # 한글 비율 30% 미만 → 영문 브랜드

async def _resolve_brand_korean(brand: str, product_name: str = "") -> str:
    """
    브랜드를 쿠팡 등록명으로 변환.

    우선순위:
      1. 매핑 테이블 (brand_map.json) — 한국어 포함 모든 브랜드 즉시 반환
      2. 영문 브랜드이면 Gemini — 한국어 공식명 질의 후 테이블에 저장
      3. 원본 반환 (Gemini 실패 시)
    """
    if not brand:
        return brand

    bmap = _load_brand_map()

    # 1순위: 매핑 테이블 (대소문자 무시, 한국어 브랜드도 포함)
    key_lower = brand.strip().lower()
    for k, v in bmap.items():
        if k.strip().lower() == key_lower:
            print(f"[BrandMap] 테이블 히트: '{brand}' → '{v}'")
            return v

    # 영문 브랜드 → 2순위 Gemini 질의
    # 한국어 브랜드지만 짧은 경우(≤4자)도 Gemini로 공식명 재확인:
    #   ex) "너드"(잘못된 표기) → Gemini가 "너즈"로 정정
    # 단, 긴 한국어 브랜드(≥5자)나 product_name 없으면 Gemini 생략(오버헤드 방지)
    _is_english = _is_english_brand(brand)
    _is_short_korean = (not _is_english) and len(brand.replace(" ", "")) <= 4 and bool(product_name)
    if not _is_english and not _is_short_korean:
        return brand   # 긴 한국어 브랜드는 이미 올바른 이름으로 간주

    # 2순위: Gemini 질의
    _gk = getattr(_settings, "GEMINI_API_KEY", "")
    if _gk:
        try:
            import google.generativeai as genai
            genai.configure(api_key=_gk)
            _gm = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
            if _is_english:
                _prompt = (
                    f"브랜드명(영문): {brand}\n"
                    + (f"상품명 참고: {product_name[:60]}\n" if product_name else "")
                    + "\n"
                    "이 브랜드의 쿠팡(Coupang) Wing에 실제로 등록된 공식 브랜드명을 답하세요.\n"
                    "⚠️ 규칙:\n"
                    "1. 쿠팡에 한국어 공식 브랜드명이 존재하면 그 이름으로 답하세요.\n"
                    "   예) Nordisk → 노르디스크 / Neutrogena → 뉴트로지나 / Dove → 도브\n"
                    "   예) Nerds → 너즈 / Haribo → 하리보 / Kellogs → 켈로그\n"
                    "2. 쿠팡에 한국어 공식명이 없거나 영문 브랜드로만 등록된 경우 영문 원본 그대로 답하세요.\n"
                    "   예) Patagonia → Patagonia / Arc'teryx → Arc'teryx\n"
                    "3. 단순 영문→한글 음역 금지 (공식 등록명이 아닌 발음 표기 금지).\n"
                    "4. 확실하지 않으면 영문 원본을 답하세요.\n"
                    "브랜드명만 답하세요. 설명 금지."
                )
            else:
                # 짧은 한국어 브랜드 — 잘못된 표기 정정 질의
                _prompt = (
                    f"브랜드명: {brand}\n"
                    f"상품명: {product_name[:80]}\n\n"
                    "이 상품의 실제 제조사 공식 브랜드명을 쿠팡 기준으로 답하세요.\n"
                    "⚠️ 브랜드 표기가 틀렸을 수 있습니다. 상품 내용을 보고 올바른 공식 브랜드명을 확인하세요.\n"
                    "예) 브랜드='너드', 상품='레인보우 구미 클러스터' → 너즈  (Nerds 공식 한국어명)\n"
                    "예) 브랜드='도브', 상품='비타민C 비누' → 도브  (이미 올바름)\n"
                    "이미 올바른 브랜드명이면 그대로 답하세요. 브랜드명만 답하세요. 설명 금지."
                )
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, lambda: genai.GenerativeModel(_gm).generate_content(_prompt)
            )
            result = (resp.text or "").strip().split("\n")[0].strip()
            if result and len(result) <= 50 and result.lower() not in _BRAND_MAP_INVALID_VALUES:
                if result != brand:
                    # 변환 결과가 다르면 테이블에 저장 (다음부터 Gemini 호출 없이 즉시 사용)
                    bmap[brand] = result
                    _save_brand_map(bmap)
                    print(f"[BrandMap] Gemini 변환: '{brand}' → '{result}' (저장 완료)")
                return result
        except Exception as e:
            print(f"[BrandMap] Gemini 브랜드 변환 실패: {e}")

    return brand   # 3순위: 원본 반환


# ── 엑셀 등록 전 필수 항목 검증 ─────────────────────────────────────
def _excel_issues(entry: "QueueEntry") -> list[dict]:
    """
    완료(done) 상태 QueueEntry에서 엑셀 업로드 시 실패할 수 있는 항목 검사.

    반환: [{"field": str, "msg": str, "severity": "critical"|"warning", "fixable": bool}]
    severity=critical → 엑셀 다운로드 차단 (Wing 등록 불가)
    severity=warning  → 다운로드 허용하되 경고 표시 (등록 가능하나 품질 저하)
    fixable=True      → 카드에서 수기 수정 가능
    """
    issues = []
    item = entry.result_item

    # 1. 카테고리 ID
    if not entry.category_id:
        issues.append({
            "field": "category_id",
            "msg": "카테고리 ID 없음 — Wing 등록 불가",
            "severity": "critical",
            "fixable": True,
        })

    # 2. 브랜드
    brand = (item.brand if item else entry.brand) or ""
    if not brand.strip() or brand.strip() in ("해당없음", ""):
        issues.append({
            "field": "brand",
            "msg": "브랜드 없음 또는 미인식",
            "severity": "critical",
            "fixable": True,
        })

    # 3. 상품명
    pname = (item.product_name if item else entry.product_name) or ""
    if len(pname.strip()) < 5:
        issues.append({
            "field": "product_name",
            "msg": f"상품명 너무 짧음 ({len(pname.strip())}자) — 5자 이상 필요",
            "severity": "critical",
            "fixable": True,
        })

    # 4. 가격 0원
    if item and (not item.bundles or all(b.original_price == 0 for b in item.bundles)):
        issues.append({
            "field": "price",
            "msg": "판매가격 0원 — 네이버 가격 파싱 실패 (재수집 권장)",
            "severity": "critical",
            "fixable": False,
        })

    # 5. 이미지 없음
    if item:
        has_img = bool(item.main_image_url) or any(b.image_url for b in item.bundles)
        if not has_img:
            issues.append({
                "field": "image",
                "msg": "대표 이미지 없음 — Wing 등록 불가",
                "severity": "critical",
                "fixable": True,
            })

    # 6. 상세설명 없음 (Wing DF 컬럼 필수)
    if item and not (item.detail_description or "").strip():
        issues.append({
            "field": "detail_desc",
            "msg": "상세설명(DF) 없음 — Wing 등록 시 오류",
            "severity": "warning",
            "fixable": False,
        })

    # 7. 용량 없음 (엔진오일/부동액/브레이크오일만 — 오일필터·에어필터는 용량 개념 없음)
    _oil_cats = {"78889", "78903", "78894"}
    _is_set_product = (
        entry.category_id == "113070"
        or bool(_re.search(r'교환세트|오일세트|필터세트|점검세트', entry.product_name or ""))
    )
    if entry.category_id in _oil_cats and entry.volume == 0 and not _is_set_product:
        issues.append({
            "field": "volume",
            "msg": "엔진오일 용량 미입력 — 묶음 수량 옵션 누락",
            "severity": "warning",
            "fixable": True,
        })

    # 8. 구매옵션 유형 검증 (가이드 파일 기준)
    if item and entry.category_id:
        _valid_opts = _valid_option_types(entry.category_id)
        if _valid_opts:
            _invalid_opts = [
                t for t, _ in item.extra_options
                if t not in _valid_opts
            ]
            if _invalid_opts:
                issues.append({
                    "field": "extra_options",
                    "msg": f"카테고리 불허 구매옵션 유형 {_invalid_opts} — Wing 업로드 실패 원인",
                    "severity": "critical",
                    "fixable": False,
                })

    # 9. gosisi_cat 자동 보정 (가이드 파일 기준) — 오류 대신 조용히 덮어씀
    if item and entry.category_id:
        _guide_gcat = _guide_gosisi_cat(entry.category_id)
        if _guide_gcat:
            entry.gosisi_cat  = _guide_gcat
            item.gosisi_cat   = _guide_gcat

    return issues


# ── 용량(L) 기반 묶음 최대 수량 결정 ────────────────────────────────
def _volume_to_max_qty(volume_l: float) -> int:
    """
    용량(L) 절대값 → 묶음 최대 수량 자동 결정 (엔진오일 기준).

    사용자 지정 규칙:
      1L  → 최대 12개   (1L  × 12 = 12L)
      4L  → 최대  4개   (4L  ×  4 = 16L)
      6L  → 최대  3개   (6L  ×  3 = 18L)

    중간 용량 보간:
      ≤ 1L  → 12개
      ≤ 2L  →  6개
      ≤ 3L  →  4개
      ≤ 4L  →  4개
      ≤ 6L  →  3개
      ≤ 8L  →  2개
      > 8L  →  1개
    """
    if volume_l <= 1.0:  return 12
    if volume_l <= 2.0:  return 6
    if volume_l <= 4.0:  return 4
    if volume_l <= 6.0:  return 3
    if volume_l <= 8.0:  return 2
    return 1


# ── 상품명에서 용량 파싱 → L 환산 ──────────────────────────────────
_VOL_RE = _re.compile(
    r'(\d+(?:[.,]\d+)?)\s*'                          # 숫자 (소수 포함)
    r'(L|리터|ℓ|ml|mL|ML|밀리리터|cc|CC|㎖|㎗|qt|QT|gal|GAL|oz|OZ|fl\.?\s*oz)',
    _re.IGNORECASE,
)

# 단위 → L 환산 계수
_VOL_TO_L: dict[str, float] = {
    "l": 1.0, "리터": 1.0, "ℓ": 1.0,
    "ml": 0.001, "milliliter": 0.001, "밀리리터": 0.001, "㎖": 0.001,
    "cc": 0.001,
    "㎗": 0.1,
    "qt": 0.946,   # US quart (946ml)
    "gal": 3.785,  # US gallon
    "oz": 0.02957, # fl oz
    "fl oz": 0.02957,
    "fl.oz": 0.02957,
}

def _parse_volume(text: str) -> tuple[float, str, float]:
    """
    상품명에서 첫 번째 용량 표기 추출 → (원본값, 원본단위, L환산값).
    감지 실패 시 (0.0, "", 0.0) 반환.

    예시:
      "300ml"   → (300.0, "ml",  0.3)
      "946ml"   → (946.0, "ml",  0.946)
      "3.78L"   → (3.78,  "L",   3.78)
      "1 qt"    → (1.0,   "qt",  0.946)
      "1 gal"   → (1.0,   "gal", 3.785)
    """
    # 부정 lookahead: 바로 뒤가 한글 어미(당, 짜리 등)나 알파벳이면 건너뜀
    for m in _VOL_RE.finditer(text):
        end = m.end()
        if end < len(text):
            nc = text[end]
            if nc.isalpha() and nc.isascii():   # 뒤에 영문 이어지면 단어 내부
                continue
            if nc in ('당', '짜'):              # "L당", "L짜리" 제외
                continue
        raw_val  = float(m.group(1).replace(",", "."))
        raw_unit = m.group(2)
        unit_key = raw_unit.lower().replace(" ", "").rstrip(".")
        factor   = _VOL_TO_L.get(unit_key, _VOL_TO_L.get(unit_key[:2], 0.0))
        if factor == 0.0:
            continue
        vol_l = raw_val * factor
        return (raw_val, raw_unit, vol_l)
    return (0.0, "", 0.0)


# ── 상품명에서 중량 파싱 → g 환산 ───────────────────────────────────
_WT_RE = _re.compile(
    r'(\d+(?:[.,]\d+)?)\s*'
    r'(kg|KG|킬로그램|킬로|g(?!al)|G(?!AL)|그램|mg|MG|밀리그램'
    r'|lb|LB|파운드|oz(?!\s*fl)|OZ(?!\s*FL)|온스'
    r'|t(?:on)?|TON|톤)',
    _re.IGNORECASE,
)

_WT_TO_G: dict[str, float] = {
    "kg": 1000.0, "킬로그램": 1000.0, "킬로": 1000.0,
    "g":  1.0,    "그램": 1.0,
    "mg": 0.001,  "밀리그램": 0.001,
    "lb": 453.592, "파운드": 453.592,
    "oz": 28.3495, "온스": 28.3495,
    "t":  1_000_000.0, "ton": 1_000_000.0, "톤": 1_000_000.0,
}

def _parse_weight(text: str) -> tuple[float, str, float]:
    """
    상품명에서 첫 번째 중량 표기 추출 → (원본값, 원본단위, g환산값).
    감지 실패 시 (0.0, "", 0.0) 반환.

    예시:
      "500g"   → (500.0,  "g",  500.0)
      "1.5kg"  → (1.5,    "kg", 1500.0)
      "200mg"  → (200.0,  "mg", 0.2)
      "1lb"    → (1.0,    "lb", 453.6)
    """
    for m in _WT_RE.finditer(text):
        end = m.end()
        if end < len(text):
            nc = text[end]
            if nc.isalpha() and nc.isascii():
                continue
        raw_val  = float(m.group(1).replace(",", "."))
        raw_unit = m.group(2)
        unit_key = raw_unit.lower().rstrip(".")
        factor   = _WT_TO_G.get(unit_key, 0.0)
        if factor == 0.0:
            # 2글자 fallback (e.g. "KG" → "kg")
            factor = _WT_TO_G.get(unit_key[:2], 0.0)
        if factor == 0.0:
            continue
        return (raw_val, raw_unit, raw_val * factor)
    return (0.0, "", 0.0)


# ── raw_json 스펙에서 용량/중량 fallback 추출 ─────────────────────────
_WEIGHT_SPEC_KEYS = {
    "weight", "netweight", "grossweight", "productweight",
    "중량", "무게", "순중량", "개당중량", "개당 중량", "net weight",
}
_VOLUME_SPEC_KEYS = {
    "volume", "capacity", "fluidvolume", "liquidvolume",
    "용량", "내용량", "개당용량", "개당 용량", "net volume",
}


def _parse_weight_from_json(raw_json: dict) -> tuple[float, str, float]:
    """raw_json 스펙 속성에서 중량 표기 탐색 → _parse_weight() 위임."""

    # ── Naver 전용: simpleProductForDetailPage.A.unitCapacity ──────────
    # indicationUnit이 g/kg 계열이면 중량, ml/L이면 용량 → 중량만 여기서 처리
    try:
        _uc = (
            (raw_json.get("simpleProductForDetailPage") or {})
            .get("A", {})
            .get("unitCapacity") or {}
        )
        if isinstance(_uc, dict):
            _total = _uc.get("totalCapacityValue")
            _unit  = str(_uc.get("indicationUnit") or "").strip().lower()
            if _total is not None and _unit in ("g", "kg", "그램", "킬로그램"):
                rv, ru, rg = _parse_weight(f"{_total}{_unit}")
                if rg > 0:
                    return rv, ru, rg
    except Exception:
        pass

    def _search(d, depth=0) -> str:
        if depth > 7 or not isinstance(d, dict):
            return ""
        for k, v in d.items():
            kl = k.lower().replace(" ", "")
            if kl in _WEIGHT_SPEC_KEYS:
                if isinstance(v, (str, int, float)) and str(v).strip():
                    return str(v).strip()
            if isinstance(v, dict):
                r = _search(v, depth + 1)
                if r:
                    return r
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        # attributeName/attributeValue 패턴
                        an = (item.get("attributeName") or item.get("name") or "").lower().replace(" ", "")
                        av = item.get("attributeValue") or item.get("value") or ""
                        if an in _WEIGHT_SPEC_KEYS and av:
                            return str(av).strip()
                        r = _search(item, depth + 1)
                        if r:
                            return r
        return ""

    spec_str = _search(raw_json)
    if spec_str:
        rv, ru, rg = _parse_weight(spec_str)
        if rg > 0:
            return rv, ru, rg

    # ── fallback: raw_json 내 모든 문자열 값에서 "중량: 112g" 패턴 스캔 ──
    _WT_STR_RE = _re.compile(
        r'(?:중량|무게|net\s*weight|weight)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(g|kg|그램|킬로그램)',
        _re.I,
    )

    def _scan_str(d, depth=0) -> str:
        if depth > 10:
            return ""
        if isinstance(d, dict):
            for v in d.values():
                r = _scan_str(v, depth + 1)
                if r:
                    return r
        elif isinstance(d, list):
            for item in d:
                r = _scan_str(item, depth + 1)
                if r:
                    return r
        elif isinstance(d, str) and d:
            m = _WT_STR_RE.search(d)
            if m:
                return f"{m.group(1)}{m.group(2)}"
        return ""

    fallback_str = _scan_str(raw_json)
    if fallback_str:
        rv, ru, rg = _parse_weight(fallback_str)
        if rg > 0:
            return rv, ru, rg

    return 0.0, "", 0.0


def _parse_volume_from_json(raw_json: dict) -> tuple[float, str, float]:
    """raw_json 스펙 속성에서 용량 표기 탐색 → _parse_volume() 위임."""

    # ── Naver 전용: simpleProductForDetailPage.A.unitCapacity ──────────
    try:
        _uc = (
            (raw_json.get("simpleProductForDetailPage") or {})
            .get("A", {})
            .get("unitCapacity") or {}
        )
        if isinstance(_uc, dict):
            _total = _uc.get("totalCapacityValue")
            _unit  = str(_uc.get("indicationUnit") or "").strip().lower()
            if _total is not None and _unit in ("ml", "l", "cc", "리터"):
                rv, ru, rl = _parse_volume(f"{_total}{_unit}")
                if rl > 0:
                    return rv, ru, rl
    except Exception:
        pass

    def _search(d, depth=0) -> str:
        if depth > 7 or not isinstance(d, dict):
            return ""
        for k, v in d.items():
            kl = k.lower().replace(" ", "")
            if kl in _VOLUME_SPEC_KEYS:
                if isinstance(v, (str, int, float)) and str(v).strip():
                    return str(v).strip()
            if isinstance(v, dict):
                r = _search(v, depth + 1)
                if r:
                    return r
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        an = (item.get("attributeName") or item.get("name") or "").lower().replace(" ", "")
                        av = item.get("attributeValue") or item.get("value") or ""
                        if an in _VOLUME_SPEC_KEYS and av:
                            return str(av).strip()
                        r = _search(item, depth + 1)
                        if r:
                            return r
        return ""

    spec_str = _search(raw_json)
    if spec_str:
        rv, ru, rl = _parse_volume(spec_str)
        if rl > 0:
            return rv, ru, rl

    # ── fallback: raw_json 내 모든 문자열 값에서 "용량: 295ml" 패턴 스캔 ──
    _VL_STR_RE = _re.compile(
        r'(?:용량|내용량|capacity|volume)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(ml|mL|l|L|cc|리터)',
        _re.I,
    )

    def _scan_str(d, depth=0) -> str:
        if depth > 10:
            return ""
        if isinstance(d, dict):
            for v in d.values():
                r = _scan_str(v, depth + 1)
                if r:
                    return r
        elif isinstance(d, list):
            for item in d:
                r = _scan_str(item, depth + 1)
                if r:
                    return r
        elif isinstance(d, str) and d:
            m = _VL_STR_RE.search(d)
            if m:
                return f"{m.group(1)}{m.group(2)}"
        return ""

    fallback_str = _scan_str(raw_json)
    if fallback_str:
        rv, ru, rl = _parse_volume(fallback_str)
        if rl > 0:
            return rv, ru, rl

    return 0.0, "", 0.0


# ── 네이버 옵션 그룹 유형 감지 ────────────────────────────────────────
_SCENT_WORDS = {
    "피치", "라벤더", "로즈", "재스민", "코코넛", "민트", "바닐라", "시트러스",
    "머스크", "플로럴", "베르가못", "샌달우드", "체리", "망고", "파인애플",
    "허니", "그린티", "유칼립투스", "패션후르츠", "스트로베리", "블루베리",
    "레몬", "오렌지", "라임", "오키드", "튤립", "페오니", "라일락",
    "아쿠아", "향기", "향", "scent", "fragrance",
    "peach", "lavender", "rose", "jasmine", "coconut", "mint", "vanilla",
    "cherry", "mango", "lemon", "orange",
}
_COLOR_WORDS = {
    "레드", "블루", "그린", "옐로", "화이트", "블랙", "핑크", "퍼플",
    "베이지", "그레이", "네이비", "오렌지", "코랄", "민트", "골드", "실버",
    "red", "blue", "green", "yellow", "white", "black", "pink", "purple",
    "beige", "gray", "navy", "orange", "coral", "gold", "silver",
}
_SIZE_RE = _re.compile(r'\d+\s*(ml|mL|g|kg|oz|L|cm|mm|인치|inch)', _re.I)


def _detect_naver_option_group(opt_names: list[str], raw_json: dict) -> str:
    """
    네이버 옵션명 목록 + raw_json → 쿠팡 옵션 유형 이름 추론.
    1순위: raw_json에서 optionGroup 레이블 탐색
    2순위: 옵션명 키워드 휴리스틱
    반환: "향", "색상", "사이즈", "종류" 중 하나
    """
    # ── 1순위: raw_json optionGroup 레이블 탐색 ──────────────────────
    def _find_group_label(d, depth=0) -> str:
        if depth > 8 or not isinstance(d, dict):
            return ""
        for k, v in d.items():
            kl = k.lower()
            if kl in ("grouplabel", "optiongrouplabel", "label", "groupname",
                      "optiongroupname", "optiontype", "optionkind"):
                if isinstance(v, str) and v.strip():
                    return v.strip()
            if isinstance(v, dict):
                r = _find_group_label(v, depth + 1)
                if r:
                    return r
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        r = _find_group_label(item, depth + 1)
                        if r:
                            return r
        return ""

    raw_label = _find_group_label(raw_json)
    if raw_label:
        rl = raw_label.lower()
        if any(w in rl for w in ("향", "scent", "fragrance", "aroma")):
            return "향"
        if any(w in rl for w in ("색", "color", "colour")):
            return "색상"
        if any(w in rl for w in ("사이즈", "size", "용량", "volume", "중량", "weight")):
            return "사이즈"

    # ── 2순위: 옵션명 키워드 휴리스틱 ────────────────────────────────
    if not opt_names:
        return "종류"

    scent_hits = sum(
        1 for n in opt_names
        if any(w in n for w in _SCENT_WORDS)
    )
    color_hits = sum(
        1 for n in opt_names
        if any(w.lower() in n.lower() for w in _COLOR_WORDS)
    )
    size_hits = sum(
        1 for n in opt_names
        if _SIZE_RE.search(n)
    )

    total = len(opt_names)
    if scent_hits / total >= 0.5:
        return "향"
    if color_hits / total >= 0.5:
        return "색상"
    if size_hits / total >= 0.5:
        return "사이즈"
    return "종류"


# ── Gemini 필수 옵션 최종 보완 ──────────────────────────────────────
async def _gemini_fill_required_options(
    product_name: str,
    raw_json: dict,
    naver_category: str,
    category_id: str,
    category_label: str,
    required_options: list,
    valid_options: list,
    existing_options: list,
    gemini_api_key: str,
    model: str,
    loop,
    log_fn=None,
    uid: str = "",
) -> list:
    """
    Gemini를 이용해 쿠팡 카테고리의 필수 옵션을 최종 보완.

    흐름:
      1. 기존 파싱 결과(existing_options)에서 아직 채워지지 않은 required_options 파악
      2. Gemini에게 상품 정보 + 카테고리 필수옵션 전달 → JSON으로 값 반환 요청
      3. valid_options 기준 검증 후 기존 옵션에 덮어쓰기/추가
      4. Gemini 실패 시 → 기존 existing_options 그대로 반환 (안전 fallback)

    반환: 병합된 extra_options list[tuple[str, str]]
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not gemini_api_key:
        return existing_options

    # ── 필수 옵션 중 아직 미채워진 항목 파악 ──────────────────────────
    _existing_types = {t for t, _ in existing_options}
    # 수량은 별도 슬롯 처리 → 제외
    _qty_synonyms = {"수량", "총 수량", "총수량", "수량(개)", "개수"}
    _missing = [
        r for r in (required_options or [])
        if r not in _existing_types and r not in _qty_synonyms
    ]

    # 모두 채워져 있으면 Gemini 호출 불필요
    if not _missing:
        _log(f"[{uid[:6]}] ✅ 필수 옵션 완비 — Gemini 보완 스킵")
        return existing_options

    _log(f"[{uid[:6]}] 🤖 Gemini 필수 옵션 보완 시작 — 미입력: {_missing}")

    # ── 스펙 텍스트 추출 (raw_json → 참고 정보) ──────────────────────
    def _extract_flat_specs(d, depth=0, acc=None):
        if acc is None:
            acc = {}
        if depth > 5 or not isinstance(d, dict):
            return acc
        SPEC_KEYS = {
            "attributes", "attribute", "specs", "spec",
            "productAttributes", "detailAttributes",
            "ingredientInfo", "nutritionInfo", "componentInfo",
            "components", "volume", "capacity", "weight",
            "manufacturer", "origin", "brand", "contents",
        }
        for k, v in d.items():
            kl = k.lower()
            if any(sk in kl for sk in SPEC_KEYS):
                if isinstance(v, str) and v.strip():
                    acc[k] = v.strip()[:200]
                elif isinstance(v, list):
                    for item in v[:15]:
                        if isinstance(item, dict):
                            name = (item.get("attributeName") or item.get("name") or "")
                            val  = (item.get("attributeValue") or item.get("value") or "")
                            if name and val:
                                acc[name] = str(val)[:100]
            if isinstance(v, dict):
                _extract_flat_specs(v, depth + 1, acc)
        return acc

    specs = _extract_flat_specs(raw_json)

    # Naver unitCapacity 추가 (중량/용량 힌트)
    try:
        _uc = (
            (raw_json.get("simpleProductForDetailPage") or {})
            .get("A", {})
            .get("unitCapacity") or {}
        )
        if isinstance(_uc, dict) and _uc.get("totalCapacityValue"):
            specs["[Naver단위용량]"] = f"{_uc['totalCapacityValue']}{_uc.get('indicationUnit','')}"
    except Exception:
        pass

    spec_lines = "\n".join(f"- {k}: {v}" for k, v in list(specs.items())[:25])

    # 기존 채워진 옵션 표시 (Gemini 참고용)
    existing_str = "\n".join(f"- {t}: {v}" for t, v in existing_options if t not in _qty_synonyms)

    # ── 옵션별 단위 가이드 ───────────────────────────────────────────
    _UNIT_GUIDE = {
        "개당 중량":  "숫자+단위. 단위는 g 또는 kg만 허용. 예: 112g, 500g, 1kg, 1.5kg",
        "개당 용량":  (
            "숫자+단위. 단위는 ml 또는 L만 허용. 예: 295ml, 500ml, 1L, 1.5L\n"
            "    ⚠ 고체 제품(과자·사탕·젤리·건어물·정제·분말·스틱형데오드란트·고체비누 등)은 개당 용량 항목을 JSON에서 제외하세요.\n"
            "    ⚠ 액상·겔·크림·세럼·샴푸·음료 등 실제 부피가 있는 제품에만 기입하세요.\n"
            "    ⚠ 정확한 용량을 알 수 없을 때 '1ml', '2ml' 같은 임의 소용량을 절대 기입하지 마세요. 모르면 제외."
        ),
        "수량":       "숫자+단위. 예: 30개, 36개입, 2세트",
        "총 수량":    "숫자+단위. 예: 30개, 36개입",
        "색상":       "색상명. 예: 블랙, 화이트, 레드, 핑크",
        "향":         "향 이름. 예: 라벤더, 복숭아, 민트, 오리지널",
        "사이즈":     "크기 표기. 예: S, M, L, XL 또는 100ml, 200ml",
        "종류":       "종류/타입명. 예: 오리지널, 스탠다드, 프리미엄",
    }
    unit_guide_lines = []
    for opt in _missing:
        if opt in _UNIT_GUIDE:
            unit_guide_lines.append(f"  - {opt}: {_UNIT_GUIDE[opt]}")

    # ── Gemini 프롬프트 ────────────────────────────────────────────
    prompt = f"""당신은 쿠팡 상품 등록 전문가입니다. 아래 상품의 필수 옵션값을 JSON으로 반환하세요.

[상품 정보]
상품명: {product_name}
네이버 카테고리: {naver_category or "없음"}
{"스펙/단위 속성:" + chr(10) + spec_lines if spec_lines else ""}

[쿠팡 카테고리]
카테고리명: {category_label or "알 수 없음"} (ID: {category_id})

[이미 입력된 옵션]
{existing_str or "없음"}

[반드시 채워야 할 필수 옵션 — 모두 입력 필수]
{chr(10).join(f"- {r}" for r in _missing)}

[허용된 옵션 유형 전체]
{", ".join(valid_options or [])}

[단위 가이드]
{chr(10).join(unit_guide_lines) if unit_guide_lines else "  (없음)"}

[규칙]
1. [반드시 채워야 할 필수 옵션]을 모두 채우세요. 단, 아래 규칙 5에 해당하면 제외 허용.
2. 정확한 값을 모를 경우에도 상품명·카테고리·일반 상품 지식으로 합리적으로 추론하여 입력하세요.
   공란(미입력)은 쿠팡 등록 실패를 유발하므로 합리적 추측이 공란보다 낫습니다.
3. 값 형식은 단위 가이드를 반드시 따르세요.
   예) "36개들이" → "36개입", "500 그램" → "500g", "236mL" → "236ml"
4. 반드시 JSON 형식으로만 답하세요. 설명 금지.
5. '개당 용량'은 액상·겔·크림·세럼·샴푸·음료 등 실제 부피가 있는 액체 제품에만 기입하세요.
   고체 식품(과자·사탕·젤리·건어물·정제·분말 등)이나 고체 상품(스틱형 데오드란트·고체 비누·왁스·립밤 등)은 '개당 용량'을 JSON에서 완전히 제외하세요.
   실제 용량 수치를 확인할 수 없을 때 '1ml', '2ml' 같은 임의 소용량을 절대 기입하지 마세요.

[출력 예시]
{{"개당 중량": "112g", "개당 용량": "295ml"}}"""

    try:
        from google import genai as _genai_lib
        _gclient = _genai_lib.Client(api_key=gemini_api_key)

        resp = await loop.run_in_executor(
            None,
            lambda: _gclient.models.generate_content(
                model=model,
                contents=prompt,
            )
        )
        raw_text = (resp.text or "").strip()
        _log(f"[{uid[:6]}] 🤖 Gemini 필수옵션 원본응답: {raw_text[:200]}")

        # JSON 파싱
        import json as _json_mod
        _json_str = raw_text
        # 코드블록 제거
        if "```" in _json_str:
            _m = _re.search(r'```(?:json)?\s*(\{.+?\})\s*```', _json_str, _re.S)
            if _m:
                _json_str = _m.group(1)
        # 인라인 JSON 추출
        if not _json_str.startswith("{"):
            _m2 = _re.search(r'\{.+\}', _json_str, _re.S)
            if _m2:
                _json_str = _m2.group(0)

        gemini_opts = _json_mod.loads(_json_str)

        if not isinstance(gemini_opts, dict):
            raise ValueError("JSON 응답이 dict가 아님")

        # ── 검증 + 병합 ────────────────────────────────────────────
        merged = list(existing_options)
        _existing_set = {t for t, _ in merged}
        _added = []

        for opt_type, opt_val in gemini_opts.items():
            opt_type = opt_type.strip()
            opt_val  = str(opt_val).strip()

            # 허용 옵션 목록 검증 (필수 목록에 있는 것은 통과 허용)
            if valid_options and opt_type not in valid_options:
                if required_options and opt_type not in required_options:
                    _log(f"[{uid[:6]}] ⚠ Gemini 옵션 무시 (카테고리 불허): {opt_type}={opt_val}")
                    continue

            # 필수 목록에 없는 옵션 무시 (할루시네이션 방지)
            if required_options and opt_type not in required_options:
                _log(f"[{uid[:6]}] ⚠ Gemini 옵션 무시 (필수 외 추가): {opt_type}={opt_val}")
                continue

            # 빈값 / 명시적 "모름" 응답 무시
            if not opt_val or opt_val.lower() in (
                "없음", "null", "none", "unknown", "알수없음", "모름",
                "알 수 없음", "불명", "미상", "n/a", "-", "해당없음",
            ):
                _log(f"[{uid[:6]}] ⚠ Gemini 옵션 무시 (빈값/모름): {opt_type}={opt_val}")
                continue

            # ── 단위 정합성 검증: 중량/용량 옵션에 혼합 단위 값 방지 ──────
            # 예) "개당 중량"에 "20g 20ml" → "20g"만 추출, 나머지 버림
            _WT_CLEAN_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(g|kg)', _re.I)
            _VL_CLEAN_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(ml|mL|l|L|cc)', _re.I)
            _wt_keywords = {"개당 중량", "최소 중량", "중량", "무게", "순중량"}
            _vl_keywords = {"개당 용량", "최소 용량", "용량", "내용량"}

            if opt_type in _wt_keywords:
                _m_wt = _WT_CLEAN_RE.search(opt_val)
                if _m_wt:
                    _clean = f"{_m_wt.group(1)}{_m_wt.group(2).lower()}"
                    if _clean != opt_val:
                        _log(f"[{uid[:6]}] 🔧 Gemini 중량값 정제: '{opt_val}' → '{_clean}'")
                    opt_val = _clean
                else:
                    _log(f"[{uid[:6]}] ⚠ Gemini 옵션 무시 (중량 단위 없음): {opt_type}={opt_val}")
                    continue
            elif opt_type in _vl_keywords:
                _m_vl = _VL_CLEAN_RE.search(opt_val)
                if _m_vl:
                    _clean = f"{_m_vl.group(1)}{_m_vl.group(2).lower()}"
                    if _clean != opt_val:
                        _log(f"[{uid[:6]}] 🔧 Gemini 용량값 정제: '{opt_val}' → '{_clean}'")
                    opt_val = _clean
                    # ── 고체 제품 할루시네이션 방지 ──────────────────────────
                    # 중량 ≥ 5g 인데 용량 ≤ 2ml이면 물리적으로 불가능 → 거부
                    # (고체 10g 구미에 1ml 용량은 Gemini 할루시네이션)
                    try:
                        _vl_num = float(_m_vl.group(1))
                        _vl_unit = _m_vl.group(2).lower()
                        _vl_ml = _vl_num * 1000.0 if _vl_unit == 'l' else _vl_num
                        if _vl_ml <= 2.0:
                            # ≤2ml는 중량과 무관하게 무조건 할루시네이션으로 거부
                            _log(
                                f"[{uid[:6]}] ⚠ Gemini 개당 용량 의심값 거부 "
                                f"({_vl_ml}ml — 임의 소용량 할루시네이션 가능성): "
                                f"{opt_type}={opt_val}"
                            )
                            continue
                    except Exception:
                        pass
                else:
                    _log(f"[{uid[:6]}] ⚠ Gemini 옵션 무시 (용량 단위 없음): {opt_type}={opt_val}")
                    continue

            # 이미 있으면 덮어쓰기 (기존값보다 Gemini가 포맷을 더 정확히 맞춤)
            if opt_type in _existing_set:
                merged = [(t, v) if t != opt_type else (t, opt_val) for t, v in merged]
                _log(f"[{uid[:6]}] 🤖 Gemini 옵션 교정: {opt_type} → '{opt_val}'")
            else:
                merged.append((opt_type, opt_val))
                _existing_set.add(opt_type)
                _log(f"[{uid[:6]}] 🤖 Gemini 옵션 추가: {opt_type}='{opt_val}'")
            _added.append(opt_type)

        if _added:
            _log(f"[{uid[:6]}] ✅ Gemini 필수 옵션 보완 완료: {_added}")
        else:
            _log(f"[{uid[:6]}] ℹ Gemini 응답했으나 추가된 옵션 없음")

        return merged

    except Exception as _ge:
        _log(f"[{uid[:6]}] ⚠ Gemini 필수 옵션 보완 실패 → 기존 옵션 유지: {_ge}")
        return existing_options  # 안전 fallback


# ── 바코드 EAN 체크섬 검증 ──────────────────────────────────────────
def _validate_barcode(code: str) -> bool:
    """
    EAN-8 / EAN-13 / UPC-A(12) 체크섬 검증.

    쿠팡은 체크섬이 틀린 바코드를 "유효한 바코드가 아닙니다"로 거부하므로
    Gemini / UPC DB 결과에 반드시 적용한다.

    알고리즘:
      EAN-13: 홀수 위치(1,3,5...) ×1 + 짝수 위치(2,4,6...) ×3
              합계 mod 10 == 0 이면 유효
      EAN-8 : 홀수 위치 ×3 + 짝수 위치 ×1  (EAN-13과 가중치 반대)
      UPC-A (12자리): EAN-13 규칙을 앞에 0을 붙여 13자리로 변환 후 적용
    """
    code = code.strip().replace("-", "").replace(" ", "")
    if not code.isdigit():
        return False

    # 12자리 UPC-A → 13자리로 변환 후 EAN-13 규칙 적용
    if len(code) == 12:
        code = "0" + code

    if len(code) == 13:
        total = sum(
            int(d) * (1 if i % 2 == 0 else 3)
            for i, d in enumerate(code)
        )
        return total % 10 == 0

    if len(code) == 8:
        total = sum(
            int(d) * (3 if i % 2 == 0 else 1)
            for i, d in enumerate(code)
        )
        return total % 10 == 0

    return False  # 지원하지 않는 자릿수


# ── 네이버 URL 정규화 + 상품 ID 추출 ─────────────────────────────
def _naver_product_id(url: str) -> str:
    """
    URL에서 상품 ID만 추출 → 중복 판정 기준.
    https://smartstore.naver.com/store/products/12345678?NaPm=... → "12345678"
    추출 실패 시 URL 자체를 쿼리 파라미터 제거 후 반환.
    """
    m = _re.search(r'/products/(\d+)', url)
    if m:
        return m.group(1)
    return url.split("?")[0].rstrip("/")

def _is_duplicate_url(url: str, queue: list) -> bool:
    """
    큐에 동일 상품 ID의 항목이 있으면 True.
    - 쿼리 파라미터 차이 무시 (NaPm, tracking 등)
    - 스토어명 차이 무시 (같은 상품 ID면 동일 상품)
    - 동일 파일 내 중복도 완벽 차단
    """
    pid = _naver_product_id(url)
    return any(_naver_product_id(e.url) == pid for e in queue)


# ── 텍스트 파일에서 URL 파싱 ──────────────────────────────────────
def _parse_urls_from_text(text: str) -> list[str]:
    """
    메모장(.txt) 내용에서 URL 목록 추출.

    규칙:
      - 'https://' 또는 'http://'로 시작하는 토큰 = 새 URL 시작
      - 이전 줄이 URL이었고 현재 줄이 https://로 시작하지 않으면
        이전 URL에 이어붙임 (줄바꿈으로 잘린 긴 URL 처리)
      - 빈 줄은 구분자 역할
    """
    lines = text.strip().splitlines()
    urls: list[str] = []
    current = ""
    for line in lines:
        s = line.strip()
        if not s:
            if current:
                urls.append(current)
                current = ""
            continue
        if s.startswith("https://") or s.startswith("http://"):
            if current:
                urls.append(current)
            current = s
        else:
            current += s   # 잘린 URL 이어붙이기
    if current:
        urls.append(current)
    # 끝 구두점 제거 후 반환
    return [u.rstrip(".,;)\"'") for u in urls if u.startswith("http")]


def _clean_product_name(name: str) -> str:
    """
    상품명에서 불필요한 표기 제거.

    처리 순서:
      ① 대괄호 홍보/배송 문구  : [당일출고] [공식] [정품] [한정판] 등
      ② 원산지·수입국 괄호     : [원산지:일본] [수입산] 등 후미 속성 괄호
      ③ 앞부분 배송 문구       : "당일출고 상품명" 형태
      ④ 쉼표 뒤 중량·수량 제거  : ", 38g" / ", 1L" / ", 1개" 등
                                  네이버 상품명 "제품명, 중량, 수량" 패턴 대응
      ⑤ 비쉼표 중량·수량 제거  : "캐스트롤 0W20 1L" 등 쉼표 없이 단독 표기된 경우

    예시:
      "[한정판] 모리나가 베이크 치즈, 38g, 1개 [원산지:일본]"
        → "모리나가 베이크 치즈"
      "[당일출고] 로드스킨 rhode 펩타이드 립 틴트"
        → "로드스킨 rhode 펩타이드 립 틴트"
      "당일발송 캐스트롤 0W20 1L"
        → "캐스트롤 0W20"
    """
    import re, unicodedata

    # ① 대괄호형 홍보·배송·판매조건 문구 제거 ────────────────────────
    #   [한정판], [한정품], [시즌한정], [공식], [정품] 등 포함
    _BRACKET = re.compile(
        r'[\[\(【〔]'
        r'(?:당일\s*(?:출고|발송|배송)|오늘\s*(?:출고|발송|배송)'
        r'|빠른\s*배송|로켓\s*배송|무료\s*배송|특급\s*배송|익일\s*배송'
        r'|공식(?:판매)?|정품|한국\s*정품|국내\s*정품|직수입|직영'
        r'|해외직구|해외배송|직배송|국내배송'
        r'|BEST|NEW|HOT|SALE|특가|이벤트'
        r'|한정(?:판|품|수량)?|시즌\s*한정'   # [한정판] [한정품] [시즌한정] 추가
        r')'
        r'[\]\)】〕]'
        r'[\s,\-_]*',
        re.IGNORECASE,
    )

    # ② 원산지·수입국 괄호 (어느 위치든 제거) ────────────────────────
    #   [원산지:일본] [원산국:미국] [수입산] [국내산] [제조국:중국] 등
    _ORIGIN = re.compile(
        r'\s*[\[\(]'
        r'(?:원산지|원산국|제조국|수입산|국내산|수입원|수입)[^\]\)]*'
        r'[\]\)]',
        re.IGNORECASE,
    )

    # ③ 앞부분 배송 문구 제거 ─────────────────────────────────────────
    _DELIVERY_PREFIX = re.compile(
        r'^(?:'
        r'당일\s*(?:출고|발송|배송)'
        r'|오늘\s*(?:출고|발송|배송)'
        r'|빠른\s*배송|익일\s*배송|특급\s*배송'
        r'|무료\s*배송|로켓\s*배송|직배송'
        r')\s*[\s,\-_]*',
        re.IGNORECASE,
    )

    # ④-a 쉼표 뒤 중량·용량 세그먼트 제거 ───────────────────────────
    #   네이버 상품명 패턴: "제품명, 38g, 1개" — 콤마가 명시적으로 있는 경우
    #   우선 처리하여 의도한 속성 구분자를 정밀하게 제거
    _COMMA_WGT = re.compile(
        r',\s*\d+(?:[.,]\d+)?\s*'
        r'(?:L|ML|mL|CC|KG|G(?!AL)|MG|mg'
        r'|리터|밀리리터|그램|킬로그램|밀리그램'
        r'|oz|OZ|qt|QT|gal|GAL|lb|LB|파운드|온스)'
        r'(?![A-Za-z가-힣])',
        re.IGNORECASE,
    )
    # ④-b 쉼표 뒤 수량 세그먼트 제거
    _COMMA_QTY = re.compile(
        r',\s*\d+\s*개(?:입|짜리|묶음|봉)?(?![가-힣A-Za-z])',
        re.IGNORECASE,
    )

    # ⑤-a 비쉼표 중량·용량 (공백·×·/ 뒤 단독 표기)
    _VOL = re.compile(
        r'[\s×·/*]*[\(\[\{]?'
        r'\d+(?:[.,]\d+)?'
        r'\s*(?:L|ML|mL|CC|KG|G(?!AL)|MG|mg'
        r'|리터|밀리리터|그램|킬로그램|밀리그램'
        r'|oz|OZ|qt|QT|gal|GAL|lb|LB|파운드|온스)'
        r'(?![A-Za-z])'     # 뒤에 영문자 → 매칭 취소 (LPG→L+PG 오인식 방지)
        r'(?:짜리)?[\)\]\}]?',
        re.IGNORECASE,
    )
    # ⑤-b 비쉼표 수량
    _QTY = re.compile(r'[\s×·/*]*\d+\s*개(?:입|짜리|묶음|봉)?(?![가-힣A-Za-z])')

    # 보이지 않는 유니코드 제어/형식 문자 제거 (BOM ﻿, zero-width space 등)
    result = ''.join(c for c in name if unicodedata.category(c) != 'Cf')

    # ① 대괄호 홍보/배송 문구
    result = _BRACKET.sub('', result)
    # ② 원산지 괄호
    result = _ORIGIN.sub('', result)
    # ③ 앞부분 배송 문구
    result = _DELIVERY_PREFIX.sub('', result)

    # ④ 쉼표 기반 중량·수량 제거 (반복: "A, 38g, 1개" 구조 대응)
    for _ in range(4):
        prev = result
        result = _COMMA_WGT.sub('', result)
        result = _COMMA_QTY.sub('', result)
        if result == prev:
            break

    # ⑤ 비쉼표 중량·수량 제거 (캐스트롤 0W20 1L 등)
    for _ in range(4):
        prev = result
        result = _VOL.sub('', result)
        result = _QTY.sub('', result)
        if result == prev:
            break

    # 앞뒤 구분자(쉼표·공백·슬래시·×··) 정리 + 중복 공백 제거
    result = re.sub(r'^[\s,/\-_×·*]+|[\s,/\-_×·*]+$', '', result)
    result = re.sub(r'\s{2,}', ' ', result)
    return result.strip()


# ── 상품명 금지 키워드 감지 ───────────────────────────────────────
import re as _re_name

_BANNED_NAME_PATTERN = _re_name.compile(
    r'(?<![a-zA-Z0-9])'          # 영숫자 복합어 내부 오탐 방지
    r'('
    # ── 기존: 순위·최상급·과장 ──────────────────────────────────────
    r'\d+위'                                          # 순위: 1위, 2위
    r'|(?:국내|세계|전국|판매|인기|아시아)?\s*(?:최고|최초|최대|최강|최상|최저|최소)(?!급|한|대한|소한|단|소)'
    r'|특효(?:약|과)?'
    r'|부작용\s*(?:없|zero|제로)'
    r'|(?:전문가|의사|약사|피부과)\s*추천'
    r'|완치|치료(?:제|약|효과)?'
    r'|기적|혁신적|압도적'
    r'|무조건\s*(?:효과|추천|보장)'
    r'|효과\s*보장|100%\s*효과'
    # ── 식품표시광고법: 질병 예방·치료 오인 ─────────────────────────
    r'|당뇨(?:병|예방|개선|완화)?'
    r'|고혈압(?:예방|개선|완화)?'
    r'|혈당\s*(?:강하|안정|조절|개선)'
    r'|혈압\s*(?:강하|낮추|조절|개선)'
    r'|항암(?:효과|작용)?'
    r'|항염(?:효과|작용)?'
    r'|암\s*(?:예방|억제|치료|발생방지)'
    r'|아토피\s*(?:개선|완화|치료)?'
    r'|생리통\s*(?:완화|개선)?|생리불순'
    r'|탈모\s*(?:방지|예방|개선|치료)'
    r'|골다공증|고지혈증|치매\s*예방'
    r'|변비\s*(?:개선|해소|완화)|쾌변'
    r'|소화\s*(?:불량|개선)|소화성\s*궤양'
    r'|수족냉증\s*완화|갱년기\s*(?:개선|완화)'
    # ── 식품표시광고법: 건강기능식품 오인 ───────────────────────────
    r'|다이어트\s*(?:커피|음료|식품|보조|효과)?(?=\s)'
                                                      # "다이어트 용품·가방" 등 일반 명사 앞에선 허용
    r'|체중\s*감량|지방\s*분해|식욕\s*억제'
    r'|면역력\s*(?:증진|향상|강화|개선)'
    r'|혈액순환\s*(?:개선|촉진)'
    r'|간\s*기능\s*(?:개선|개선)'
    r'|항산화\s*(?:효과|작용)?'
    r'|키\s*(?:성장|크는|키우는)'
    # ── 식품표시광고법: 한약 처방명 ─────────────────────────────────
    r'|공진단|경옥고|쌍화탕|십전대보탕|사군자탕|사물탕'
    r'|녹용대보탕|총명탕|귀비탕|육미지황탕|우황청심원'
    r'|익수영진고|오자연종환'
    # ── 표시광고법: 공산품 의료기기 오인 ───────────────────────────
    r'|족저근막염|목디스크|거북목|일자목'
    r'|발가락\s*교정(?:기)?|척추\s*교정'
    r'|코골이\s*(?:방지|개선|치료)|수면무호흡'
    r'|주름\s*(?:개선|치료|완화)'
    r'|피부질환\s*(?:치료|완화|개선)'
    r')'
    r'(?:의|이|가|은|는|을|를|로|으로|적|인)?',       # 조사·접미어 포함 처리
    _re_name.IGNORECASE,
)

def _has_banned_keyword(name: str) -> list[str]:
    """상품명에서 금지 키워드 목록 반환. 없으면 빈 리스트."""
    return [m.group(1) for m in _BANNED_NAME_PATTERN.finditer(name)]


# ── 단일 URL 파이프라인 ───────────────────────────────────────────

async def _process_entry(
    entry: QueueEntry,
    log: "ui.log | None",
    margin_rate: float,
    lead_time: int = 2,
    use_nobg: bool = True,
) -> None:
    """
    1개 URL 에 대해 크롤 → 이미지 → catbox 업로드 → 가격 산출 → BulkItem 생성.
    entry.result_item 에 결과 저장. 실패 시 entry.error 에 메시지.
    """
    loop = asyncio.get_running_loop()

    # 처리 시작 시 lead_time 확정
    # (lead_time_locked=True인 경우 이미 _run_global_processing에서 개별값으로 전달됨)
    entry.lead_time = lead_time

    def log_(msg: str):
        print(msg)

    try:
        # ── 1. 크롤링 ─────────────────────────────────────────────
        log_(f"[{entry.uid[:6]}] 크롤링: {entry.url}")
        product: ProductData = await NaverStoreCrawler(_settings).crawl(entry.url)
        # 크롤 실패 감지 — 이름이 "Unknown" 이면 파싱 실패로 간주
        if not product.name or product.name.strip().lower() in ("unknown", ""):
            raise ValueError(
                f"상품명 추출 실패 (Unknown) — 네이버 페이지 구조 변경 or 봇 차단 의심. "
                f"잠시 후 🔄 재수집 버튼으로 다시 시도하세요."
            )
        entry.product_name = product.name
        entry.naver_price  = product.price   # 네이버 원가(1개) — 가격감시 기준가용
        # ── 브랜드 1순위: Gemini 직접 추출 ────────────────────────────
        # 규칙 기반보다 Gemini가 문맥을 이해하므로 먼저 시도
        if not entry.brand:
            _gk_b = getattr(_settings, "GEMINI_API_KEY", "")
            if _gk_b:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_b)
                    _gm_b = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
                    _brand_prompt = (
                        f"상품명: {product.name}\n"
                        f"네이버 카테고리: {product.naver_category or '없음'}\n\n"
                        "위 상품의 공식 제조사/브랜드명을 답하세요.\n"
                        "⚠️ 상품명에 적힌 브랜드 표기가 틀렸을 수 있습니다. "
                        "상품 내용을 종합해서 실제 제조사의 공식 브랜드명을 확인해 주세요.\n"
                        "규칙:\n"
                        "- 브랜드명만 답하세요 (최대 30자, 한국어 또는 영문)\n"
                        "- 제품 종류(샴푸/데오드란트/크림/오일/비타민 등)는 브랜드가 아닙니다\n"
                        "- 수식어(휴대용/여성용/대용량/미국 등)는 브랜드가 아닙니다\n"
                        "- 브랜드를 알 수 없으면 '해당없음'\n"
                        "예시: '미국 휴대용 데오드란트 여성용 수아브 트로피컬 시트러스' → 수아브\n"
                        "예시: '고바야시제약 브레스케어 민트향 50정' → 고바야시제약\n"
                        "예시: 'Dove 비타민C 비누 3개입' → Dove\n"
                        "예시: '파워스틱 남성용 데오드란트 스틱' → POWERSTICK\n"
                        "예시: '너드 말랑말랑 과일향 젤리 레인보우 구미 클러스터' → 너즈  (Nerds 공식 한국어명 '너즈', '너드' 아님)\n"
                        "예시: '하리보 골드베렌 젤리 200g' → 하리보"
                    )
                    _gresp_b = await loop.run_in_executor(
                        None, lambda: genai.GenerativeModel(_gm_b).generate_content(_brand_prompt)
                    )
                    _brand_txt = (_gresp_b.text or "").strip().split("\n")[0].strip()
                    if _brand_txt and _brand_txt.lower() not in ("unknown", "해당없음", "없음", ""):
                        log_(f"[{entry.uid[:6]}] ✅ 브랜드 Gemini(1순위) 추출: '{_brand_txt}'")
                        entry.brand = _brand_txt
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ 브랜드 Gemini 미인식 — 규칙 기반 폴백")
                except Exception as _be:
                    log_(f"[{entry.uid[:6]}] ⚠ 브랜드 Gemini 실패: {_be} — 규칙 기반 폴백")

        # ── 브랜드 2순위: 규칙 기반 폴백 (Gemini 실패/미인식 시) ────────
        if not entry.brand or entry.brand.strip() in ("해당없음", ""):
            entry.brand = _auto_brand(product.name)
            if entry.brand and entry.brand not in ("해당없음", ""):
                log_(f"[{entry.uid[:6]}] 브랜드 규칙 기반(2순위) 추출: '{entry.brand}'")

        log_(f"[{entry.uid[:6]}] 크롤 완료: {product.name} (브랜드: {entry.brand})")

        # ── 브랜드 매핑 변환 (영문→한국어, 한국어→공식명 포함) ──────────
        # brand_map.json에 한국어 브랜드도 등록 가능 (예: 파워스틱→POWERSTICK)
        # brand_locked=True(사용자 직접 입력)이면 변환 생략 — 사용자 의도 보존
        if entry.brand and not entry.brand_locked:
            _brand_before = entry.brand
            entry.brand = await _resolve_brand_korean(entry.brand, product.name)
            if entry.brand != _brand_before:
                log_(f"[{entry.uid[:6]}] 브랜드 변환: '{_brand_before}' → '{entry.brand}'")
        elif entry.brand and entry.brand_locked:
            log_(f"[{entry.uid[:6]}] 브랜드 사용자 지정(🔒): '{entry.brand}' — 변환 스킵")

        # ── 브랜드 쿠팡 공식 DB 매칭 (직접입력 → 공식 브랜드 ID 매칭) ──────
        # resolve_brand: 쿠팡 브랜드 검색 API → Gemini 최적 매칭 → 상위노출 개선
        # brand_locked=True이면 DB 매칭도 스킵 (사용자가 지정한 브랜드 우선)
        if entry.brand and entry.brand not in ("해당없음", "") and not entry.brand_locked:
            _gemini_key = getattr(_settings, "GEMINI_API_KEY", "")
            _gemini_model = getattr(_settings, "GEMINI_MODEL", "gemini-2.0-flash")
            try:
                _registrar = _get_registrar()
                _brand_matched = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _registrar.resolve_brand(
                        entry.brand,
                        gemini_api_key=_gemini_key,
                        gemini_model=_gemini_model,
                    ),
                )
                if _brand_matched and _brand_matched != entry.brand:
                    log_(f"[{entry.uid[:6]}] 브랜드 쿠팡DB 매칭: '{entry.brand}' → '{_brand_matched}'")
                    entry.brand = _brand_matched
            except Exception as _be:
                log_(f"[{entry.uid[:6]}] 브랜드 쿠팡DB 매칭 실패(무시): {_be}")

        # ── 1-a. 카테고리 감지 (3단계) + Gemini 무조건 검증 ─────────
        #
        # 감지 단계 (수동 설정이 아닌 경우):
        #   1순위: 상품명 키워드 매핑 (category_map.json)
        #   2순위: 네이버 카테고리 경로 (wholeCategoryName 크롤링 추출)
        #
        # Gemini 검증 (수동 설정 아닌 경우 항상 실행):
        #   - 카테고리 감지 성공 → "이 카테고리가 맞는가?" 검증 → 틀리면 교정
        #   - 카테고리 미감지 → 직접 카테고리 키워드 제안 → wing_categories 검색
        detector = _get_detector()

        if product.naver_category:
            log_(f"[{entry.uid[:6]}] 네이버 카테고리: {product.naver_category}")

        # ── 1순위: category_map.json 키워드 사전 매핑 ────────────────
        detected_cat_id, detected_gosisi, detected_kw = detector.detect(
            product.name, naver_category=product.naver_category
        )

        if detected_kw:
            if not entry.category_is_manual:
                entry.category_id = detected_cat_id
            entry.gosisi_cat       = detected_gosisi
            entry.detected_keyword = detected_kw
            log_(f"[{entry.uid[:6]}] ✅ [1순위] 키워드 사전 감지: '{detected_kw}' → "
                 f"ID={entry.category_id or '(미설정)'}")

        elif not entry.category_is_manual and not entry.category_id:
            # ── 2순위: 네이버 카테고리 경로 → wing_categories 이름 검색 ──
            if product.naver_category:
                _nc_parts = [p.strip() for p in _re.split(r'[>/]', product.naver_category) if p.strip()]
                for _nc_kw in reversed(_nc_parts):
                    if len(_nc_kw) <= 1:
                        continue
                    _nc_res = detector._fallback_wing_search(_nc_kw)
                    if _nc_res:
                        _nc_id, _nc_gcat, _nc_matched = _nc_res
                        entry.category_id      = _nc_id
                        entry.gosisi_cat       = _nc_gcat
                        entry.detected_keyword = f"[네이버카테고리]{_nc_matched}"
                        log_(f"[{entry.uid[:6]}] ✅ [2순위] 네이버 카테고리 매핑: '{_nc_kw}' → ID={_nc_id}")
                        break

            if not entry.category_id:
                log_(f"[{entry.uid[:6]}] ⚠ [1~2순위] 모두 실패 → Gemini 직접 매핑 시도")
        else:
            if entry.category_id:
                log_(f"[{entry.uid[:6]}] ⚠ 키워드 미감지 — 수동입력 ID 사용: {entry.category_id}")
            else:
                log_(f"[{entry.uid[:6]}] ❌ 카테고리 미감지 & 수동입력 없음")

        # ── [Gemini 검수] 1~2순위 자동 매핑 결과를 Gemini로 검증 ────────
        # 잘못된 카테고리가 그대로 등록되는 오매핑 방지
        # 예) "후추 분쇄기" → 분쇄기(가전) 오매핑 / "커피 그라인더" → 커피 오매핑
        # ※ [자동] 접두사 없는 키워드 = category_map.json 수동 관리 항목 → 검수 불필요
        _is_manual_kw = detected_kw and not detected_kw.startswith("[자동]") and not detected_kw.startswith("[네이버카테고리]")
        _gk_verify = getattr(_settings, "GEMINI_API_KEY", "")
        if (
            entry.category_id
            and not entry.category_is_manual
            and detected_kw
            and _gk_verify
            and not _is_manual_kw
        ):
            try:
                _cat_name_for_verify = detector.get_name_by_id(entry.category_id)
                if _cat_name_for_verify:
                    import google.generativeai as _gv_genai
                    _gv_genai.configure(api_key=_gk_verify)
                    _gv_model_name = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
                    _gv_model = _gv_genai.GenerativeModel(_gv_model_name)
                    _verify_prompt = (
                        f"상품명(한국어): {product.name}\n"
                        f"네이버 카테고리: {product.naver_category or '없음'}\n"
                        f"자동 감지된 쿠팡 카테고리: {_cat_name_for_verify} (ID: {entry.category_id})\n"
                        f"감지 키워드: {detected_kw}\n\n"
                        "위 카테고리 매핑이 정확한지 판단하세요.\n"
                        "정확하면 → 정확\n"
                        "부정확하면 → 부정확,올바른카테고리명(예: 향신료그라인더,후추밀,커피그라인더)\n"
                        "예시:\n"
                        "  - '후추 분쇄기 그라인더' + 분쇄기(가전) → 부정확,후추그라인더\n"
                        "  - '커피 그라인더' + 커피(식품) → 부정확,커피그라인더\n"
                        "  - '캐스트롤 5W30 엔진오일' + 엔진오일 → 정확\n"
                        "답변 형식만 준수하세요. 설명 금지."
                    )
                    log_(f"[{entry.uid[:6]}] 🔍 Gemini 카테고리 검수 중: {_cat_name_for_verify}...")
                    _verify_resp = await loop.run_in_executor(
                        None, lambda: _gv_model.generate_content(_verify_prompt)
                    )
                    _verify_txt = (_verify_resp.text or "").strip().lower()
                    if _verify_txt.startswith("부정확"):
                        # 틀린 카테고리 → 검수 실패, Gemini 재매핑으로 넘김
                        _wrong_name = entry.category_id  # 로그용
                        entry.category_id = ""
                        entry.gosisi_cat  = "기타 재화"
                        # 부정확 응답에서 올바른 카테고리 후보 추출
                        _parts = (_verify_resp.text or "").strip().split(",", 1)
                        _suggest = _parts[1].strip() if len(_parts) > 1 else ""
                        log_(f"[{entry.uid[:6]}] ⚠ Gemini 검수 실패: {_cat_name_for_verify} → "
                             f"'{_suggest or '재매핑'}' 으로 재시도")
                        # 제안 카테고리로 즉시 재검색
                        if _suggest:
                            _corr = detector.search_by_keyword(_suggest)
                            if not _corr:
                                _corr = detector._fallback_wing_search(_suggest)
                            if _corr:
                                _corr_id, _corr_gc, _corr_nm = _corr
                                entry.category_id      = _corr_id
                                entry.gosisi_cat       = _corr_gc
                                entry.detected_keyword = f"[Gemini검수]{_corr_nm}"
                                log_(f"[{entry.uid[:6]}] ✅ Gemini 검수 보정: '{_cat_name_for_verify}' → "
                                     f"'{_corr_nm}' (ID={_corr_id})")
                    else:
                        log_(f"[{entry.uid[:6]}] ✅ Gemini 검수 통과: {_cat_name_for_verify}")
            except Exception as _gve:
                log_(f"[{entry.uid[:6]}] ⚠ Gemini 카테고리 검수 오류 (무시): {_gve}")

        # ── Gemini 폴백: 1~2순위 + 검수 후에도 category_id 없을 때 ──
        if not entry.category_is_manual and not entry.category_id:
            _gk = getattr(_settings, "GEMINI_API_KEY", "")
            _gm = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
            if _gk:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk)
                    _gclient = genai.GenerativeModel(_gm)

                    # ── Wing 후보 목록 생성: 상품 도메인에 맞는 세분류 이름 추출 ──
                    # 네이버 카테고리 + 상품명 키워드로 Wing DB에서 연관 경로 필터링
                    def _get_wing_candidates(pname: str, naver_cat: str) -> str:
                        _combined = f"{pname} {naver_cat}".lower()
                        # 경로 키워드 → Wing DB path 필터
                        _path_hints = []
                        if any(w in _combined for w in ["커피", "coffee"]):
                            _path_hints.append("커피")
                        if any(w in _combined for w in ["녹차", "홍차", "보리차", "율무", "둥글레", "차"]):
                            _path_hints.append("티/전통차")
                        if any(w in _combined for w in ["음료", "주스", "드링크"]):
                            _path_hints.append("음료")
                        if any(w in _combined for w in ["과자", "스낵", "비스킷", "쿠키"]):
                            _path_hints.append("과자")
                        if any(w in _combined for w in ["라면", "국수", "파스타"]):
                            _path_hints.append("면류")
                        if any(w in _combined for w in ["세제", "샴푸", "비누", "치약"]):
                            _path_hints.append("생활용품")
                        if not _path_hints:
                            return ""
                        try:
                            _flat = _load_wing_flat() if hasattr(detector, "_fallback_wing_search") else []
                            from modules.category_detector import _load_wing_flat as _lwf
                            _flat = _lwf()
                            _candidates = []
                            for _e in _flat:
                                _epath = _e.get("path", "")
                                _ename = _e.get("name", "")
                                if not _ename or len(_ename) < 2:
                                    continue
                                if any(h in _epath for h in _path_hints):
                                    _candidates.append(_ename)
                            if _candidates:
                                return "아래는 실제 쿠팡 Wing 카테고리 세분류 목록입니다. 이 중에서 선택하세요:\n" + ", ".join(_candidates[:60])
                        except Exception:
                            pass
                        return ""

                    _wing_hint = _get_wing_candidates(product.name, product.naver_category or "")

                    # ── 프롬프트: Wing 후보 목록 포함 ──
                    _cat_prompt = (
                        f"상품명(한국어): {product.name}\n"
                        f"네이버 카테고리 경로: {product.naver_category or '없음'}\n\n"
                        + (_wing_hint + "\n\n" if _wing_hint else "")
                        + "위 목록에서 이 상품에 가장 적합한 카테고리 명칭 후보를 3개 답하세요.\n"
                        "반드시 위 목록에 있는 정확한 명칭 그대로 사용하세요 (변형 금지).\n"
                        "목록이 없으면 쇼핑몰에서 쓰이는 표현으로 작성하세요.\n"
                        "형식: 명칭1,명칭2,명칭3\n"
                        "명칭만 답하세요. 설명·문장 금지."
                    )
                    log_(f"[{entry.uid[:6]}] [3순위] Gemini 카테고리 매핑 중...")

                    _gresp = await loop.run_in_executor(
                        None, lambda: _gclient.generate_content(_cat_prompt)
                    )
                    _gtxt = (_gresp.text or "").strip()

                    if not _gtxt:
                        log_(f"[{entry.uid[:6]}] ⚠ Gemini 응답 없음")
                    else:
                        # ── [수정] 파싱: 쉼표 분리 후 strip만 (split()[0] 제거) ──
                        # 띄어쓰기 포함 명칭("오일 필터")이 잘리지 않도록 함
                        _candidates = [c.strip() for c in _gtxt.split(",") if c.strip()]

                        _gf_res = None
                        _matched_kw = ""

                        # Try 1: 각 후보를 유사도 기반 전용 검색으로 시도
                        for _cat_kw in _candidates:
                            _gf_res = detector.search_by_keyword(_cat_kw)
                            if _gf_res:
                                _matched_kw = _cat_kw
                                break

                        # Try 2: 유사도 검색 실패 시 기존 상품명 포함 방식도 병행
                        if not _gf_res:
                            log_(f"[{entry.uid[:6]}] ⚠ Gemini 유사도 검색 실패 → 상품명 포함 방식 재시도")
                            for _cat_kw in _candidates:
                                _gf_res = detector._fallback_wing_search(_cat_kw)
                                if _gf_res:
                                    _matched_kw = _cat_kw
                                    break

                        # Try 3: 모두 실패 시 Gemini에게 더 짧은 단어 재요청
                        if not _gf_res:
                            log_(f"[{entry.uid[:6]}] ⚠ Gemini 후보 {_candidates} 모두 wing 매핑 실패, 재요청...")
                            try:
                                _retry_prompt = (
                                    f"상품명(한국어): {product.name}\n"
                                    f"네이버 카테고리: {product.naver_category or '없음'}\n\n"
                                    "이 상품의 쇼핑몰 카테고리 명칭 후보를 5개 답하세요. "
                                    "이번엔 더 짧고 일반적인 명칭 위주로 작성하세요.\n"
                                    "형식: 명칭1,명칭2,명칭3,명칭4,명칭5\n"
                                    "명칭만, 설명 금지."
                                )
                                _retry_resp = await loop.run_in_executor(
                                    None, lambda: _gclient.generate_content(_retry_prompt)
                                )
                                # ── [수정] 쉼표 분리 + strip, split()[0] 제거 ──
                                _retry_candidates = [
                                    c.strip()
                                    for c in (_retry_resp.text or "").strip().split(",")
                                    if c.strip() and len(c.strip()) >= 2
                                ]
                                for _rw in _retry_candidates:
                                    _rf_res = detector.search_by_keyword(_rw)
                                    if not _rf_res:
                                        _rf_res = detector._fallback_wing_search(_rw)
                                    if _rf_res:
                                        _rf_id, _rf_gcat, _rf_matched = _rf_res
                                        entry.category_id      = _rf_id
                                        entry.gosisi_cat       = _rf_gcat
                                        entry.detected_keyword = f"[Gemini재시도]{_rf_matched}"
                                        log_(f"[{entry.uid[:6]}] ✅ Gemini 재시도 매핑: '{_rw}' → ID={_rf_id}")
                                        _gf_res = _rf_res
                                        break
                                if not entry.category_id:
                                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 재시도도 wing 매핑 실패")
                            except Exception as _re2:
                                log_(f"[{entry.uid[:6]}] ⚠ Gemini 재시도 오류: {_re2}")

                        if _gf_res and not entry.category_id:
                            _gf_id, _gf_gcat, _gf_matched = _gf_res
                            entry.category_id      = _gf_id
                            entry.gosisi_cat       = _gf_gcat
                            entry.detected_keyword = f"[Gemini]{_gf_matched}"
                            log_(f"[{entry.uid[:6]}] ✅ [3순위] Gemini 매핑: '{_matched_kw}' → "
                                 f"ID={_gf_id} ({_gf_matched})")

                        # ── [3순위] Gemini가 카테고리를 찾았으면 동일한 검수 로직 적용 ──
                        if entry.category_id:
                            try:
                                _cv_name = detector.get_name_by_id(entry.category_id)
                                if _cv_name:
                                    _cv_prompt = (
                                        f"상품명(한국어): {product.name}\n"
                                        f"네이버 카테고리: {product.naver_category or '없음'}\n"
                                        f"자동 감지된 쿠팡 카테고리: {_cv_name} (ID: {entry.category_id})\n\n"
                                        "위 카테고리 매핑이 정확한지 판단하세요.\n"
                                        "정확하면 → 정확\n"
                                        "부정확하면 → 부정확,올바른카테고리명(예: 텐트,돔텐트,알파인텐트)\n"
                                        "답변 형식만 준수하세요. 설명 금지."
                                    )
                                    log_(f"[{entry.uid[:6]}] 🔍 [3순위] Gemini 검수: {_cv_name}...")
                                    _cv_resp = await loop.run_in_executor(
                                        None, lambda: _gclient.generate_content(_cv_prompt)
                                    )
                                    _cv_txt = (_cv_resp.text or "").strip().lower()
                                    if _cv_txt.startswith("부정확"):
                                        _cv_parts = (_cv_resp.text or "").strip().split(",", 1)
                                        _cv_suggest = _cv_parts[1].strip() if len(_cv_parts) > 1 else ""
                                        log_(f"[{entry.uid[:6]}] ⚠ [3순위] 검수 실패: '{_cv_name}' → "
                                             f"'{_cv_suggest or '재매핑'}' 재시도")
                                        entry.category_id = ""
                                        entry.gosisi_cat  = "기타 재화"
                                        if _cv_suggest:
                                            _cv_corr = detector.search_by_keyword(_cv_suggest)
                                            if not _cv_corr:
                                                _cv_corr = detector._fallback_wing_search(_cv_suggest)
                                            if _cv_corr:
                                                entry.category_id      = _cv_corr[0]
                                                entry.gosisi_cat       = _cv_corr[1]
                                                entry.detected_keyword = f"[Gemini검수]{_cv_corr[2]}"
                                                log_(f"[{entry.uid[:6]}] ✅ [3순위] 검수 보정: "
                                                     f"'{_cv_name}' → '{_cv_corr[2]}' (ID={_cv_corr[0]})")
                                    else:
                                        log_(f"[{entry.uid[:6]}] ✅ [3순위] 검수 통과: {_cv_name}")
                            except Exception as _cve:
                                log_(f"[{entry.uid[:6]}] ⚠ [3순위] 검수 오류 (무시): {_cve}")

                except Exception as _gce:
                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 카테고리 매핑 실패: {_gce}")

        if not entry.category_id and not entry.category_is_manual:
            log_(f"[{entry.uid[:6]}] ❌ 카테고리 최종 미결정 — 카드에서 수동 입력하세요")

        # ── 1-a2. 가이드 기준 gosisi_cat 보정 (항상 덮어씀 — 구형 긴 문자열 자동 수정) ──
        if entry.category_id:
            _g_gcat = _guide_gosisi_cat(entry.category_id)
            if _g_gcat:
                if entry.gosisi_cat != _g_gcat:
                    log_(f"[{entry.uid[:6]}] gosisi_cat 보정: '{entry.gosisi_cat}' → '{_g_gcat}'")
                entry.gosisi_cat = _g_gcat

        # ── 1-b. 묶음 수량 결정 ──────────────────────────────────
        #
        # 규칙 (우선순위 순):
        #   qty_locked  → 사용자 수동 지정값 유지 (재수집 시)
        #   IS_SET      → 1세트 고정 (교환세트류)
        #   엔진오일(78889) → 볼륨 기반 자동 계산 (1L→12개, 4L→4개, 6L→3개)
        #   가전/가구 gosisi_cat → 단품 1개 강제
        #   그 외 → 좌측 패널 입력값(min~max) 그대로 사용
        #
        _IS_SET = (
            entry.category_id == "113070"
            or bool(_re.search(r'교환세트|오일세트|필터세트|점검세트', product.name))
        )
        _VOL_QTY_CAT = "78889"   # 엔진오일만 볼륨 기반 수량 자동계산
        # 가전/가구 등 단품 강제 gosisi_cat 키워드
        _FORCE_SINGLE_GOSISI_KW = ("가정용 전기제품", "가구", "산업용품")

        if entry.qty_locked:
            log_(f"[{entry.uid[:6]}] 수량 잠금 유지: {entry.qtys}")
            # 수량은 잠금이지만 용량은 감지 — 옵션·경고 판정에 필요
            if entry.volume == 0:
                _rv, _ru, _rl = _parse_volume(product.name)
                if _rl > 0:
                    entry.volume      = _rv
                    entry.volume_unit = _ru
                    log_(f"[{entry.uid[:6]}] 수량 잠금 — 용량 감지: {_rv}{_ru} ({_rl:.3f}L)")

        elif _IS_SET:
            entry.qtys = [1]
            log_(f"[{entry.uid[:6]}] 세트 상품 감지 → 단품(1개) 고정 (중복구매 불필요)")

        elif entry.category_id == _VOL_QTY_CAT:
            # ── 엔진오일: 볼륨 기반 자동 수량 ─────────────────────
            _rv, _ru, _rl = _parse_volume(product.name)
            if _rl > 0:
                _auto_max = _volume_to_max_qty(_rl)
                _mn = max(1, entry.min_qty)
                entry.qtys        = list(range(_mn, _auto_max + 1)) or [_mn]
                entry.volume      = _rv
                entry.volume_unit = _ru
                log_(f"[{entry.uid[:6]}] [엔진오일] 용량 {_rv}{_ru}({_rl:.3f}L) → 수량 {_mn}~{_auto_max}개 자동 설정")
            elif entry.volume > 0:
                _vol_l = entry.volume if entry.volume_unit == "L" else (
                    entry.volume / 1000 if entry.volume_unit in ("ml", "cc") else entry.volume
                )
                _auto_max = _volume_to_max_qty(_vol_l)
                _mn = max(1, entry.min_qty)
                entry.qtys = list(range(_mn, _auto_max + 1)) or [_mn]
                log_(f"[{entry.uid[:6]}] [엔진오일] UI 용량 {_vol_l}L → 수량 {_mn}~{_auto_max}개 자동 설정")
            else:
                # 엔진오일인데 볼륨 미감지 → 패널 입력 사용
                _mn = max(1, entry.min_qty)
                _mx = max(entry.qtys) if entry.qtys else _mn
                _mx = max(_mn, _mx)
                entry.qtys = list(range(_mn, _mx + 1)) or [_mn]
                log_(f"[{entry.uid[:6]}] [엔진오일] 볼륨 미감지 → 패널 설정 수량 {_mn}~{_mx}개")

        else:
            # ── 일반 상품: 볼륨 저장 후 패널 입력 수량 사용 ──────────
            _rv, _ru, _rl = _parse_volume(product.name)
            if _rl > 0:
                entry.volume      = _rv
                entry.volume_unit = _ru
                log_(f"[{entry.uid[:6]}] 용량 {_rv}{_ru} 감지 (수량은 패널 설정 사용)")

            # 가전/가구 gosisi_cat → 단품 강제
            _force_single = any(kw in (entry.gosisi_cat or "") for kw in _FORCE_SINGLE_GOSISI_KW)
            if _force_single:
                _mn = max(1, entry.min_qty)
                entry.qtys = [_mn]
                log_(f"[{entry.uid[:6]}] 가전/가구 카테고리 → 단품 {_mn}개 강제")
            else:
                # 패널 입력값 사용 — 개별 선택 모드면 qtys 그대로, range 모드면 범위 생성
                _mn = max(1, entry.min_qty)
                _qtys_s = sorted(entry.qtys) if entry.qtys else [_mn]
                _is_pick = _qtys_s != list(range(_qtys_s[0], _qtys_s[-1] + 1))
                if _is_pick:
                    # 개별 선택: 선택한 수량 그대로 유지
                    entry.qtys = _qtys_s
                    log_(f"[{entry.uid[:6]}] 개별 선택 수량: {entry.qtys}")
                else:
                    # range 모드: min~max 전체 생성
                    _mx = max(_qtys_s)
                    _mx = max(_mn, _mx)
                    entry.qtys = list(range(_mn, _mx + 1)) or [_mn]
                    log_(f"[{entry.uid[:6]}] 패널 설정 수량: {_mn}~{_mx}개 ({len(entry.qtys)}종)")

        # ── 1-c. GTIN(바코드) 확보 ──────────────────────────────────
        # 우선순위: ① 수동입력 → ② 네이버 크롤링 → ③ raw_json 패턴 추출
        #          → ④ 외부 DB 다중 쿼리(재시도) → ⑤ Gemini 폴백(스펙 포함)
        if entry.gtin:
            if _validate_barcode(entry.gtin):
                log_(f"[{entry.uid[:6]}] ✅ GTIN 수동입력 유효: {entry.gtin}")
            else:
                log_(f"[{entry.uid[:6]}] ⚠ GTIN 수동입력 체크섬 오류 → 재조회: {entry.gtin}")
                entry.gtin = ""   # 오류값 버리고 아래 자동조회 진행

        if not entry.gtin and product.barcode:
            if _validate_barcode(product.barcode):
                entry.gtin = product.barcode
                log_(f"[{entry.uid[:6]}] ✅ GTIN 네이버 직접 추출: {entry.gtin}")
            else:
                log_(f"[{entry.uid[:6]}] ⚠ GTIN 네이버 추출 체크섬 오류 (무시)")

        # ③ raw_json 전체 텍스트에서 GTIN 패턴 직접 추출
        #    크롤러의 키 기반 탐색과 달리, JSON 값 내 자유 텍스트에 숨은 바코드 탐색
        if not entry.gtin:
            try:
                _raw_text = json.dumps(product.raw_json)
                # EAN-13(13자리) / UPC-A(12자리) / EAN-8(8자리) 후보 추출
                # 가격·전화번호 오탐 방지: 8자리 미만 및 15자리 이상 제외
                for _bc_len, _bc_min in [(13, 13), (12, 12), (8, 8)]:
                    _bc_pat = re.findall(rf'\b(\d{{{_bc_len}}})\b', _raw_text)
                    for _bc in _bc_pat:
                        if _validate_barcode(_bc):
                            entry.gtin = _bc
                            log_(f"[{entry.uid[:6]}] ✅ GTIN raw_json 패턴 추출 ({_bc_len}자리): {entry.gtin}")
                            break
                    if entry.gtin:
                        break
            except Exception:
                pass

        if not entry.gtin:
            # ④ 외부 DB 조회 — 쿼리 변형 다중 시도 + 재시도 포함
            log_(f"[{entry.uid[:6]}] GTIN 외부 DB 조회 중 (다중 쿼리)...")
            _looked = await loop.run_in_executor(
                None, lambda: _gtin_lookup_retry(product.name, entry.brand or "", max_retries=2)
            )
            if _looked:
                entry.gtin = _looked
                log_(f"[{entry.uid[:6]}] ✅ GTIN 외부 DB 조회 성공: {entry.gtin}")
            else:
                # ⑤ Gemini 폴백 — 스펙/속성 정보 포함해 정확도 향상
                log_(f"[{entry.uid[:6]}] GTIN 외부 DB 전부 실패 — Gemini 폴백 시도...")
                _gk_gtin = getattr(_settings, "GEMINI_API_KEY", "")
                if _gk_gtin:
                    try:
                        import google.genai as _genai_gtin
                        import google.genai.types as _gtypes_gtin
                        _gcli_gtin = _genai_gtin.Client(api_key=_gk_gtin)

                        # raw_json에서 유용한 스펙 추출 (모델번호, 규격 등)
                        _spec_lines: list[str] = []
                        _SPEC_HINT_KEYS = {
                            "modelNo", "modelNumber", "model", "itemNo",
                            "manufacturer", "brand", "sku", "asin",
                            "attributes", "specs", "weight", "volume",
                        }
                        def _extract_spec_hints(d: dict, depth: int = 0) -> None:
                            if depth > 4 or not isinstance(d, dict):
                                return
                            for k, v in d.items():
                                if any(hk.lower() in k.lower() for hk in _SPEC_HINT_KEYS):
                                    if isinstance(v, (str, int, float)) and str(v).strip():
                                        _spec_lines.append(f"{k}: {str(v)[:80]}")
                                elif isinstance(v, dict):
                                    _extract_spec_hints(v, depth + 1)
                                elif isinstance(v, list):
                                    for item in v[:5]:
                                        if isinstance(item, dict):
                                            _extract_spec_hints(item, depth + 1)
                        _extract_spec_hints(product.raw_json)
                        _spec_str = "\n".join(_spec_lines[:10]) if _spec_lines else ""

                        _gtin_prompt = (
                            f"상품명: {product.name}\n"
                            f"브랜드: {entry.brand or ''}\n"
                            + (f"스펙 정보:\n{_spec_str}\n" if _spec_str else "")
                            + "위 제품의 정확한 UPC 또는 EAN 바코드 번호(GTIN)를 "
                            "인터넷에서 검색해 찾아주세요.\n"
                            "찾은 바코드 번호를 숫자만 답하세요 "
                            "(공백·하이픈 없이, 8~14자리).\n"
                            "검색해도 확실한 바코드를 찾을 수 없으면 '없음'이라고만 답하세요."
                        )
                        _gr_gtin = _gcli_gtin.models.generate_content(
                            model=getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash"),
                            contents=[_gtypes_gtin.Part.from_text(text=_gtin_prompt)],
                            config=_gtypes_gtin.GenerateContentConfig(
                                tools=[_gtypes_gtin.Tool(
                                    google_search=_gtypes_gtin.GoogleSearch()
                                )]
                            ),
                        )
                        _gtin_raw = (_gr_gtin.text or "").strip()
                        _gtin_digits = _re.sub(r"\D", "", _gtin_raw)
                        if 8 <= len(_gtin_digits) <= 14 and _validate_barcode(_gtin_digits):
                            entry.gtin = _gtin_digits
                            log_(f"[{entry.uid[:6]}] ✅ GTIN Gemini 폴백 성공: {entry.gtin}")
                        else:
                            log_(f"[{entry.uid[:6]}] GTIN Gemini 폴백 실패 (응답: {_gtin_raw[:40]!r}) — 공란 등록")
                    except Exception as _ge_gtin:
                        log_(f"[{entry.uid[:6]}] GTIN Gemini 폴백 오류: {_ge_gtin} — 공란 등록")
                else:
                    log_(f"[{entry.uid[:6]}] GTIN 최종 미확보 — 공란 등록")

        # ── 2. 이미지 가공 (누끼 + 수량별 합성) ──────────────────
        # 단일 등록 모드: 누끼·배지 없이 원본 이미지 직접 업로드
        if entry.single_mode:
            log_(f"[{entry.uid[:6]}] [단일등록] 이미지 합성 스킵 — 원본 이미지 직접 사용")
        if not use_nobg and not entry.single_mode:
            log_(f"[{entry.uid[:6]}] 누끼 OFF — 원본 이미지 그대로 사용")
        composed: dict[int, str] = {}
        if product.local_image_path and not entry.single_mode:
            try:
                proc = ImageProcessor(_settings, store=getattr(entry, "watch_store", "샵케이"))
                composed = await loop.run_in_executor(
                    None,
                    partial(proc.process, product.local_image_path,
                            product.product_id, entry.qtys,
                            **({"skip_nobg": True} if not use_nobg else {})),
                )
                log_(f"[{entry.uid[:6]}] 이미지 합성: {len(composed)}개")
            except TypeError:
                # skip_nobg 파라미터 미지원 시 fallback
                composed = await loop.run_in_executor(
                    None,
                    partial(proc.process, product.local_image_path,
                            product.product_id, entry.qtys),
                )
                log_(f"[{entry.uid[:6]}] 이미지 합성(fallback): {len(composed)}개")
            except Exception as ie:
                log_(f"[{entry.uid[:6]}] 이미지 가공 실패 (원본 URL 사용): {ie}")

        # ── 3. R2 이미지 업로드 ───────────────────────────────────
        log_(f"[{entry.uid[:6]}] R2 이미지 업로드 시작...")
        bundle_image_urls: dict[int, str] = {}

        if entry.single_mode:
            # 단일 등록: 합성 없이 원본 이미지 직접 업로드
            _single_main = ""
            if product.local_image_path and Path(product.local_image_path).exists():
                _single_main = await loop.run_in_executor(
                    None, lambda p=product.local_image_path: upload_file(p)
                )
                if _single_main:
                    log_(f"[{entry.uid[:6]}] [단일등록] 원본 이미지 업로드 완료")
            if not _single_main and product.image_url:
                _single_main = await loop.run_in_executor(
                    None, lambda: upload_url(product.image_url, f"{product.product_id}_main.jpg")
                ) or product.image_url
            bundle_image_urls[1] = _single_main
        else:
            for qty in entry.qtys:
                img_path = composed.get(qty)
                if img_path and Path(img_path).exists():
                    url_result = await loop.run_in_executor(
                        None, lambda p=img_path: upload_file(p)
                    )
                    if url_result:
                        bundle_image_urls[qty] = url_result
                        log_(f"[{entry.uid[:6]}] {qty}개 이미지 → {url_result[:60]}...")
                    else:
                        log_(f"[{entry.uid[:6]}] {qty}개 이미지 업로드 실패 → 원본 URL 사용")

        # 대표이미지 (1개 번들 이미지 or 네이버 원본)
        main_img = bundle_image_urls.get(1) or ""
        if not main_img and product.image_url:
            log_(f"[{entry.uid[:6]}] 원본 이미지 직접 업로드...")
            main_img = await loop.run_in_executor(
                None, lambda: upload_url(product.image_url, f"{product.product_id}_main.jpg")
            ) or product.image_url

        # ── 3-b. 상세페이지 전용 클린 이미지 생성 및 R2 업로드 ────────
        # 배지(수량 스탬프) 절대 포함 금지 — process_detail() 은 _stamp_label() 미호출
        # 캐시된 _nobg.png 파일은 사용하지 않음 (오염된 캐시 방지)
        # 단일 등록 모드: 사용자가 선택한 네이버 상세 이미지를 해외배송.png 와 합성
        detail_img_url = ""
        if entry.single_mode:
            # 단일 모드: 사용자 선택 이미지 → 업로드 → URL 목록으로 활용
            # detail_img_url 은 빈 값으로 두고 아래 합성 단계에서 single_selected_imgs 사용
            log_(f"[{entry.uid[:6]}] [단일등록] 상세이미지: 사용자 선택 이미지 {len(entry.single_selected_imgs)}장 사용")
        elif product.local_image_path:
            try:
                _proc_det = ImageProcessor(_settings)
                _det_path = await loop.run_in_executor(
                    None,
                    lambda: _proc_det.process_detail(
                        product.local_image_path,
                        product.product_id,
                        skip_nobg=not use_nobg,
                    ),
                )
                if _det_path and Path(_det_path).exists():
                    _det_up = await loop.run_in_executor(
                        None, lambda p=_det_path: upload_file(p)
                    )
                    if _det_up:
                        detail_img_url = _det_up
                        log_(f"[{entry.uid[:6]}] 상세이미지(배지없음) 업로드 완료")
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ 상세이미지 업로드 실패 — 원본 URL 사용")
                else:
                    log_(f"[{entry.uid[:6]}] ⚠ 상세이미지 생성 실패 — 원본 URL 사용")
            except Exception as _det_e:
                log_(f"[{entry.uid[:6]}] ⚠ 상세이미지 처리 오류 ({_det_e}) — 원본 URL 사용")
        if not detail_img_url and not entry.single_mode:
            detail_img_url = product.image_url or ""
            if detail_img_url:
                log_(f"[{entry.uid[:6]}] 상세이미지 fallback → 원본 네이버 URL 사용")

        # 해외배송(✈️) 선택 시 우마이마켓 안내 이미지를 상세페이지 최상단에 삽입
        _UMAI_NOTICE_IMG = (
            "https://pub-52f3ccc0b1874a4dbca6ac2b8b860d49.r2.dev/"
            "%ED%95%B4%EC%99%B8%EB%B0%B0%EC%86%A1.png"
        )
        overseas_prefix = (
            f"<img src='{_UMAI_NOTICE_IMG}'>"
            if lead_time == 10 else ""
        )

        # ── 4. 가격 산출 (단일 마진율) ──────────────────────────────
        bp_list = PriceCalculator(_settings).calculate(
            product, quantities=entry.qtys,
            sale_rate=margin_rate, original_rate=margin_rate,
        )
        log_(f"[{entry.uid[:6]}] 가격 산출 (×{margin_rate}): {[(b.qty, b.sale_price) for b in bp_list]}")

        # ── 5. 네이버 옵션 처리 ───────────────────────────────────
        # 네이버 옵션이 있으면 → 옵션별로 가격 개별 산출 후 Bundle 생성
        # 네이버 옵션이 없으면 → 기존 수량 묶음 방식 유지
        naver_opts = product.naver_options
        naver_option_rows: list[tuple[str, str]] = []   # ("종류", "옵션명") 목록

        if naver_opts:
            log_(f"[{entry.uid[:6]}] 네이버 옵션 {len(naver_opts)}개 감지 → 옵션별 가격 산출")
            base_bp = bp_list[0]   # qty=1 기준 가격
            unit_sale = base_bp.sale_price
            unit_orig = base_bp.original_price

            # 옵션별 Bundle: qty=1, 가격에 추가금액×마진 반영
            _extra = getattr(entry, "price_extra", 0) or 0
            bundles = []
            for opt in naver_opts:
                add_margin = round(opt.add_price * margin_rate / 10) * 10  # 10원 단위 반올림
                opt_sale   = unit_sale + add_margin + _extra
                opt_orig   = unit_orig + add_margin + _extra
                bundles.append(Bundle(
                    qty=1,
                    sale_price=opt_sale,
                    original_price=opt_orig,
                    image_url=main_img,
                    option_label=opt.name,   # 옵션명을 Bundle에 태깅
                ))
                price_note = f" (+{add_margin:,}원 추가)" if opt.add_price else ""
                extra_note = f" [관부가세+{_extra:,}원]" if _extra else ""
                log_(f"[{entry.uid[:6]}]   옵션: {opt.name} → {opt_sale:,}원{price_note}{extra_note}")
            # 옵션 그룹 유형 자동 감지 (향/색상/사이즈/종류)
            _opt_type = _detect_naver_option_group(
                [o.name for o in naver_opts], product.raw_json
            )
            naver_option_rows = [(_opt_type, opt.name) for opt in naver_opts]
            log_(f"[{entry.uid[:6]}] 네이버 옵션 유형 감지: '{_opt_type}'")
        else:
            # 기존 수량 묶음 방식
            _extra = getattr(entry, "price_extra", 0) or 0
            bundles = [
                Bundle(
                    qty=bp.qty,
                    sale_price=bp.sale_price + _extra,
                    original_price=bp.original_price + _extra,
                    image_url=bundle_image_urls.get(bp.qty, main_img),
                )
                for bp in bp_list
            ]

        # 상품명: 용량·수량 제거 후 50자 이내 (쿠팡 옵션에서 중복 노출되므로)
        _extra_suffix = " 관부가세 포함" if (getattr(entry, "price_extra", 0) or 0) > 0 else ""
        _max_name_len = 50 - len(_extra_suffix)
        product_name_50 = _clean_product_name(product.name)[:_max_name_len] + _extra_suffix
        log_(f"[{entry.uid[:6]}] 상품명 정제: {product.name[:40]}... → {product_name_50}")

        # ── 상품명 금지 키워드 감지 → Gemini 재작성 ──────────────────
        _banned = _has_banned_keyword(product_name_50)
        if not _banned:
            _banned = _has_banned_keyword(product.name)   # 원본도 체크
        if _banned:
            log_(f"[{entry.uid[:6]}] ⚠ 상품명 금지 키워드 감지: {_banned} → Gemini 재작성 중...")
            _gk_bn = getattr(_settings, "GEMINI_API_KEY", "")
            if _gk_bn:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_bn)
                    _gm_bn = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
                    _bn_prompt = (
                        f"원본 상품명(한국어): {product.name}\n"
                        f"브랜드: {entry.brand or '없음'}\n"
                        f"네이버 카테고리: {product.naver_category or '없음'}\n"
                        f"감지된 금지 키워드: {', '.join(_banned)}\n\n"
                        "위 상품명에서 아래 금지 표현을 제거하고 쿠팡 등록용 상품명으로 다시 작성하세요.\n\n"
                        "【금지 표현 목록 — 식품표시광고법·표시광고법 위반】\n"
                        "· 순위·우위: 1위, 최고, 최초, 최대, 최강, 유일한, 독보적\n"
                        "· 과장·허위: 특효, 기적, 혁신적, 압도적, 무조건 효과, 효과 보장\n"
                        "· 의학적 오인: 전문가·의사·약사·피부과 추천, 완치, 치료, 부작용 없음\n"
                        "· 질병명(식품표시광고법): 당뇨, 고혈압, 혈당강하, 혈압강하, 항암,\n"
                        "  아토피, 생리통완화, 탈모방지, 골다공증, 고지혈증, 치매예방,\n"
                        "  변비개선, 쾌변, 소화성궤양, 수족냉증, 갱년기 등 모든 질병·증상명\n"
                        "· 건강기능 표현: 다이어트(식품 카테고리에서), 체중감량, 지방분해,\n"
                        "  식욕억제, 면역력증진, 혈액순환개선, 항산화, 간기능개선, 키성장\n"
                        "· 한약 처방명: 공진단, 경옥고, 쌍화탕, 십전대보탕 등\n"
                        "· 의료기기 오인(공산품): 족저근막염, 목디스크, 거북목, 일자목,\n"
                        "  발가락교정, 코골이개선, 수면무호흡, 주름개선, 피부질환치료 등\n\n"
                        "반드시 지켜야 할 규칙:\n"
                        "1. 브랜드명, 원산지, 등급, 향/맛, 핵심 특징은 절대 제거하지 마세요.\n"
                        "   예) '로렌조 이탈리아산 엑스트라버진 올리브오일' → 그대로 유지\n"
                        "2. 금지 표현 부분만 제거하거나 중립 표현으로 대체하세요.\n"
                        "   예) '당뇨에 좋은 여주즙' → '여주즙'\n"
                        "   예) '족저근막염 깔창' → '아치 지지 깔창'\n"
                        "   예) '탈모방지 샴푸' → '두피 케어 샴푸'\n"
                        "3. 제거 후 문장이 어색하면 자연스럽게 다듬되, 없는 내용 추가 금지.\n"
                        "4. 중량(380g, 1kg 등)·용량(500ml, 1L 등)·수량(1개, 2개입 등) 표기는\n"
                        "   쿠팡 옵션 항목에서 따로 노출되므로 상품명에 포함하지 마세요.\n"
                        "5. 10~50자 이내로 작성. 상품명만 답하세요. 설명·문장 금지."
                    )
                    _gresp_bn = await loop.run_in_executor(
                        None, lambda: genai.GenerativeModel(_gm_bn).generate_content(_bn_prompt)
                    )
                    _bn_txt = (_gresp_bn.text or "").strip().split("\n")[0].strip()[:50]
                    if len(_bn_txt.strip()) >= 5:
                        log_(f"[{entry.uid[:6]}] ✅ 상품명 금지키워드 제거: '{product_name_50}' → '{_bn_txt}'")
                        product_name_50 = _bn_txt
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ 상품명 금지키워드 Gemini 재작성 실패 — 원본 유지")
                except Exception as _bne:
                    log_(f"[{entry.uid[:6]}] ⚠ 상품명 금지키워드 처리 오류: {_bne}")
            else:
                log_(f"[{entry.uid[:6]}] ⚠ GEMINI_API_KEY 없음 — 금지키워드 포함 상태로 등록 주의")

        # 상품명 5자 미만 → Gemini 보완
        if len(product_name_50.strip()) < 5:
            _gk_n = getattr(_settings, "GEMINI_API_KEY", "")
            if _gk_n:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_n)
                    _gm_n = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
                    _name_prompt = (
                        f"원본 상품명(한국어): {product.name}\n"
                        f"브랜드: {entry.brand or '없음'}\n"
                        f"네이버 카테고리: {product.naver_category or '없음'}\n\n"
                        "쿠팡 상품 등록용 한국어 상품명을 10~40자 이내로 생성하세요.\n"
                        "아래 금지 표현은 절대 포함하지 마세요 (위반 시 계정 정지):\n"
                        "· 질병명: 당뇨, 고혈압, 항암, 아토피, 탈모방지, 족저근막염, 목디스크 등\n"
                        "· 건강기능: 다이어트(식품), 면역력, 혈액순환개선, 항산화, 지방분해 등\n"
                        "· 최상급·과장: 1위, 최고, 최초, 유일한, 특효, 기적, 혁신적 등\n"
                        "· 한약 처방명: 공진단, 경옥고, 쌍화탕 등\n"
                        "· 의료기기 오인(공산품): 거북목, 일자목, 코골이개선, 주름개선 등\n"
                        "중량(g/kg)·용량(ml/L)·수량(N개/N개입) 표기는 옵션에서 따로 노출되므로 상품명에 포함하지 마세요.\n"
                        "상품명만 답하세요. 문장이나 설명 금지."
                    )
                    _gresp_n = await loop.run_in_executor(
                        None, lambda: genai.GenerativeModel(_gm_n).generate_content(_name_prompt)
                    )
                    _name_txt = (_gresp_n.text or "").strip().split("\n")[0].strip()[:50]
                    if len(_name_txt.strip()) >= 5:
                        log_(f"[{entry.uid[:6]}] ✅ 상품명 Gemini 보완: '{product_name_50}' → '{_name_txt}'")
                        product_name_50 = _name_txt
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ 상품명 Gemini 보완 실패 — 원본 사용")
                except Exception as _ne:
                    log_(f"[{entry.uid[:6]}] ⚠ 상품명 Gemini 실패: {_ne}")

        # ── 정제된 상품명을 entry에 동기화 ────────────────────────────
        # entry.product_name 은 지금까지 raw Naver 명칭(380g·1개 포함)을 담고 있었음.
        # product_name_50 이 모든 처리(정제+Gemini 재작성)를 마쳤으므로 이제 덮어쓴다.
        # → UI 카드 헤더·수동수정 패널·가격감시 이름 모두 정제된 이름으로 표시됨.
        entry.product_name = product_name_50

        # ── Gemini 상세 설명 생성 → 이미지 렌더링 → R2 업로드 ────────
        # product_name_50 확정 후에 호출해야 판매 멘트가 정확한 상품명으로 생성됨
        # 단일 등록 모드: Gemini 판매멘트 불필요 → 스킵
        _gemini_key   = getattr(_settings, "GEMINI_API_KEY", "")
        _gemini_model = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
        gemini_img_url = ""
        if entry.single_mode:
            log_(f"[{entry.uid[:6]}] [단일등록] Gemini 판매멘트 스킵")
        elif _gemini_key:
            log_(f"[{entry.uid[:6]}] Gemini 상세페이지 생성 + 이미지 렌더링 중...")

            def _upload_bytes(data: bytes, fname: str) -> str:
                """bytes → R2 업로드 → URL 반환 (gemini_writer 에서 호출)."""
                import os
                tmp_path = Path(tempfile.gettempdir()) / fname
                try:
                    tmp_path.write_bytes(data)
                    return upload_file(tmp_path) or ""
                finally:
                    try: os.unlink(tmp_path)
                    except Exception: pass

            try:
                _pn50_snap = product_name_50   # lambda 클로저 캡처용 로컬 변수
                gemini_img_url = await loop.run_in_executor(
                    None,
                    lambda: _gemini_detail(
                        product_name=_pn50_snap,
                        image_url=detail_img_url or product.image_url,
                        raw_json=product.raw_json,
                        api_key=_gemini_key,
                        model=_gemini_model,
                        upload_fn=_upload_bytes,
                        product_id=product.product_id,
                    ),
                )
                if gemini_img_url:
                    log_(f"[{entry.uid[:6]}] ✅ Gemini 설명 이미지 완료: {gemini_img_url[:60]}...")
                else:
                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 설명 이미지 생성 실패 — 기본 HTML 사용")
            except Exception as _ge:
                log_(f"[{entry.uid[:6]}] ⚠ Gemini 오류: {_ge} — 기본 HTML 사용")
        elif not entry.single_mode:
            log_(f"[{entry.uid[:6]}] GEMINI_API_KEY 미설정 — 기본 HTML 사용")

        # ── 상세 설명 이미지 합성 ───────────────────────────────────────
        # 우마이마켓 공지 + Gemini 판매멘트 + 누끼 이미지를 세로로 합쳐 단일 PNG로 업로드.
        # → Wing 상세설명 컬럼에 단일 URL만 기입 (HTML <img> 태그는 일부 카테고리에서
        #   Wing 서버가 JSON 파싱 오류를 냄) → 합성 이미지 하나로 모든 컨텐츠 표현.
        # 단일 등록 모드: 사용자가 선택한 네이버 상세 이미지 URL 목록을 대신 사용
        _detail_urls_to_merge: list[str] = []
        if entry.single_mode:
            _detail_urls_to_merge.extend(entry.single_selected_imgs)
        else:
            if gemini_img_url:
                _detail_urls_to_merge.append(gemini_img_url)
            if detail_img_url:
                _detail_urls_to_merge.append(detail_img_url)

        # 사용자 추가 이미지 (기존 합성 이미지 하단에 추가)
        for _eu in (getattr(entry, "extra_detail_images", None) or []):
            if _eu.strip():
                _detail_urls_to_merge.append(_eu.strip())

        # 사용자 추가 텍스트 → PIL 이미지로 렌더링 후 합성
        _extra_text = (getattr(entry, "extra_detail_text", None) or "").strip()
        if _extra_text:
            try:
                from PIL import Image as _PILImage2, ImageDraw as _ImageDraw2, ImageFont as _ImageFont2
                import io as _io2
                _txt_w, _txt_line_h, _txt_pad = 780, 28, 30
                _lines2 = []
                for _raw_line in _extra_text.splitlines():
                    # 한 줄 너무 길면 70자 단위로 강제 개행
                    while len(_raw_line) > 70:
                        _lines2.append(_raw_line[:70])
                        _raw_line = _raw_line[70:]
                    _lines2.append(_raw_line)
                _txt_h = _txt_pad * 2 + len(_lines2) * _txt_line_h
                _txt_img = _PILImage2.new("RGB", (_txt_w, _txt_h), (255, 255, 255))
                _drw = _ImageDraw2.Draw(_txt_img)
                # 한글 지원 폰트 시도 (없으면 기본)
                _font = None
                for _fp in [
                    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
                    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
                    "/usr/share/fonts/truetype/noto/NotoSansCJKkr-Regular.otf",
                    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                    "/usr/share/fonts/nanum/NanumGothic.ttf",
                    # Windows 경로
                    "C:/Windows/Fonts/malgun.ttf",
                    "C:/Windows/Fonts/gulim.ttc",
                    # 프로젝트 내 폰트 (있을 경우)
                    str(Path(__file__).parent / "assets" / "NanumGothic.ttf"),
                    str(Path(__file__).parent / "fonts" / "NanumGothic.ttf"),
                ]:
                    try:
                        _font = _ImageFont2.truetype(_fp, 18)
                        break
                    except Exception:
                        pass
                _y2 = _txt_pad
                for _tl in _lines2:
                    _drw.text((30, _y2), _tl, fill=(30, 30, 30), font=_font)
                    _y2 += _txt_line_h
                _buf2 = _io2.BytesIO()
                _txt_img.save(_buf2, format="PNG")
                _txt_bytes = _buf2.getvalue()
                _txt_fname = f"{product.product_id}_detail_text.png"
                _txt_url = await loop.run_in_executor(
                    None, lambda: _upload_bytes(_txt_bytes, _txt_fname)
                )
                if _txt_url:
                    _detail_urls_to_merge.append(_txt_url)
                    log_(f"[{entry.uid[:6]}] 텍스트 이미지 추가 완료")
            except Exception as _te:
                log_(f"[{entry.uid[:6]}] ⚠ 텍스트 이미지 렌더 실패 (스킵): {_te}")

        detail_html = ""
        if lead_time == 10 or _detail_urls_to_merge:
            try:
                from PIL import Image as _PILImage
                import urllib.request as _urllib_req
                import io as _io

                def _dl_img(url: str):
                    req = _urllib_req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    with _urllib_req.urlopen(req, timeout=10) as r:
                        return _PILImage.open(_io.BytesIO(r.read())).convert("RGB")

                _target_w = 780
                _pil_imgs = []

                # 해외배송 안내 이미지: 로컬 파일 직접 로드 (CDN 캐시 우회)
                if lead_time == 10:
                    _overseas_img_path = Path(__file__).parent / "data" / "해외배송.png"
                    try:
                        _ov_img = _PILImage.open(_overseas_img_path).convert("RGB")
                        _ratio = _target_w / _ov_img.width
                        _pil_imgs.append(_ov_img.resize((_target_w, int(_ov_img.height * _ratio)), _PILImage.LANCZOS))
                        log_(f"[{entry.uid[:6]}] 해외배송 안내 이미지 로컬 로드 완료")
                    except Exception as _oe:
                        log_(f"[{entry.uid[:6]}] ⚠ 해외배송 안내 이미지 로드 실패: {_oe}")

                for _u in _detail_urls_to_merge:
                    try:
                        _img = await loop.run_in_executor(None, lambda u=_u: _dl_img(u))
                        # 너비를 780px로 맞추고 비율 유지하여 리사이즈
                        _ratio = _target_w / _img.width
                        _new_h  = int(_img.height * _ratio)
                        _pil_imgs.append(_img.resize((_target_w, _new_h), _PILImage.LANCZOS))
                    except Exception as _ie:
                        log_(f"[{entry.uid[:6]}] ⚠ 상세이미지 다운로드 실패 (스킵): {_ie}")

                if _pil_imgs:
                    _total_h = sum(i.height for i in _pil_imgs)
                    _combined = _PILImage.new("RGB", (_target_w, _total_h), (255, 255, 255))
                    _y = 0
                    for _pi in _pil_imgs:
                        _combined.paste(_pi, (0, _y))
                        _y += _pi.height

                    _buf = _io.BytesIO()
                    _combined.save(_buf, format="PNG", optimize=True)
                    _combined_bytes = _buf.getvalue()

                    _combined_fname = f"{product.product_id}_detail_combined.png"
                    _combined_url = await loop.run_in_executor(
                        None,
                        lambda: _upload_bytes(_combined_bytes, _combined_fname),
                    )
                    if _combined_url:
                        detail_html = _combined_url
                        log_(f"[{entry.uid[:6]}] ✅ 상세이미지 합성 완료 ({len(_pil_imgs)}장 → 1장): {_combined_url}")
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ 합성이미지 업로드 실패 — Gemini 단독 사용")
                        detail_html = gemini_img_url or detail_img_url or ""
            except Exception as _me:
                log_(f"[{entry.uid[:6]}] ⚠ 이미지 합성 실패 ({_me}) — Gemini URL 사용")
                detail_html = gemini_img_url or detail_img_url or ""
        else:
            detail_html = ""

        log_(f"[{entry.uid[:6]}] 상세 설명 완료 (이미지={'있음' if detail_html else '없음'}"
             f"{', 합성' if '_combined' in detail_html else ''}"
             f"{', 해외배송 포함' if lead_time == 10 else ''})")

        # ── 옵션 자동 추출 (세트 상품 vs 일반 상품 분기) ──────────────
        extra_options: list[tuple[str, str]] = []
        _qty_unit = "개"   # 기본 수량 단위

        if _IS_SET:
            # ── 세트 상품: 차종 + 제조사 옵션 ──────────────────────
            _qty_unit = "세트"
            car_model, car_make = _extract_car_info(product.name)
            if car_model:
                extra_options.append(("차종", car_model))
                log_(f"[{entry.uid[:6]}] 세트 옵션 — 차종: {car_model}")
            else:
                log_(f"[{entry.uid[:6]}] 세트 옵션 — 차종 미감지 (상품명에서 추출 실패)")
            if car_make:
                extra_options.append(("자동차제조사", car_make))
                log_(f"[{entry.uid[:6]}] 세트 옵션 — 제조사: {car_make}")
            # SAE 점도도 있으면 추가 (세트에도 표기된 경우)
            sae = _re.search(r'(\d+)\s*[Ww]-?\s*(\d+)', product.name)
            if sae:
                grade = f"{sae.group(1)}w{sae.group(2)}"
                extra_options.append(("엔진오일 SAE점도", grade))
                log_(f"[{entry.uid[:6]}] 세트 옵션 — SAE점도: {grade}")

        else:
            # ── 일반 상품: 기존 로직 ────────────────────────────────

            # [1] SAE 점도 (엔진오일: "5w30", "0w20" 형식)
            sae = _re.search(r'(\d+)\s*[Ww]-?\s*(\d+)', product.name)
            if sae:
                grade = f"{sae.group(1)}w{sae.group(2)}"
                extra_options.append(("엔진오일 SAE점도", grade))
                log_(f"[{entry.uid[:6]}] 옵션 자동추출 — SAE점도: {grade}")

            # [2] 용량/중량 옵션 — 엔진오일 카테고리에서만 추가
            # (쿠팡 카테고리마다 허용 옵션유형이 다름 — 비오일 카테고리에서 추가하면 등록 실패)
            _OIL_CATS = {"78889", "78897", "78893", "78903", "78894"}
            if entry.category_id in _OIL_CATS:
                if entry.volume:
                    vol_str = f"{entry.volume:g}{entry.volume_unit}"
                    extra_options.append(("개당 용량", vol_str))
                    log_(f"[{entry.uid[:6]}] 옵션 직접입력 — 용량: {vol_str}")
                else:
                    _vo_rv, _vo_ru, _vo_rl = _parse_volume(product.name)
                    if _vo_rl > 0:
                        vol_str = f"{_vo_rv:g}{_vo_ru}"
                        extra_options.append(("개당 용량", vol_str))
                        log_(f"[{entry.uid[:6]}] 옵션 자동추출 — 용량: {vol_str}")
                    else:
                        # 중량 추출 (용량 미감지 시)
                        _wt_rv, _wt_ru, _wt_rg = _parse_weight(product.name)
                        if _wt_rg > 0:
                            wt_str = f"{_wt_rv:g}{_wt_ru}"
                            extra_options.append(("중량", wt_str))
                            log_(f"[{entry.uid[:6]}] 옵션 자동추출 — 중량: {wt_str}")

            # [2-B] 뷰티/개인위생 카테고리 — 상품명에서 중량 직접 파싱 후 사전 확정
            # "개당 중량"이 허용 옵션인 카테고리에서 Gemini 보다 먼저 값을 꽂아두어
            # Gemini 할루시네이션("개당 용량" 임의 채움 등) 방지
            # 엔진오일(_OIL_CATS) 은 위 [2]에서 이미 처리 → 중복 방지 조건 포함
            _wt_cat_valid = _valid_option_types(entry.category_id)
            _wt_already   = any(t in {"개당 중량", "중량", "무게"} for t, _ in extra_options)
            if (
                "개당 중량" in _wt_cat_valid
                and not _wt_already
                and entry.category_id not in _OIL_CATS
            ):
                # 1순위: Naver JSON 스펙에서 추출
                _wt_rv, _wt_ru, _wt_rg = _parse_weight_from_json(product.raw_json or {})
                # 2순위: 상품명 raw 텍스트에서 추출
                if _wt_rg <= 0:
                    _wt_rv, _wt_ru, _wt_rg = _parse_weight(product.name)
                if _wt_rg > 0:
                    _wt_str = f"{_wt_rv:g}{_wt_ru}"
                    extra_options.append(("개당 중량", _wt_str))
                    log_(f"[{entry.uid[:6]}] 옵션 사전확정 — 개당 중량: {_wt_str} (Gemini 전 직접파싱)")

            # [3] 사이즈 추출 (인치, cm — TV·가구·의류 등)
            size_m = _re.search(
                r'(\d+(?:\.\d+)?)\s*(인치|inch|"|cm|CM)(?!\w)',
                product.name, _re.I,
            )
            if size_m:
                sz_str = f"{size_m.group(1)}{size_m.group(2)}"
                extra_options.append(("사이즈", sz_str))
                log_(f"[{entry.uid[:6]}] 옵션 자동추출 — 사이즈: {sz_str}")

            # [4] 연료첨가제 카테고리 — 대상 연료 자동 감지
            # Wing 드롭다운 값: 디젤 엔진 / 디젤 전용 / 디젤/경유 / 모든 연료 유형 /
            #                   무연 가솔린 / 연료 특정 아님 / 플렉스 연료
            _FUEL_CATS = {"78899", "78900", "78901", "78902"}
            if entry.category_id in _FUEL_CATS:
                _pn_lower = product.name.lower()
                if "디젤" in _pn_lower or "경유" in _pn_lower:
                    _fuel = "디젤 엔진"
                elif "가솔린" in _pn_lower or "휘발유" in _pn_lower or "gasoline" in _pn_lower:
                    _fuel = "무연 가솔린"
                elif "lpg" in _pn_lower or "가스" in _pn_lower:
                    _fuel = "플렉스 연료"
                else:
                    _fuel = "모든 연료 유형"
                extra_options.append(("대상 연료", _fuel))
                log_(f"[{entry.uid[:6]}] 옵션 자동추출 — 대상연료: {_fuel}")

        # [공통] 네이버 옵션 병합 (자동추출보다 우선)
        if naver_option_rows and not _IS_SET:
            extra_options = naver_option_rows + extra_options
            log_(f"[{entry.uid[:6]}] 네이버 옵션 {len(naver_option_rows)}개 추가옵션에 반영")

        # [공통] 수동 옵션 병합 (사용자가 UI에서 직접 입력한 옵션)
        for opt_type, opt_val in entry.manual_options:
            extra_options.append((opt_type, opt_val))
            log_(f"[{entry.uid[:6]}] 옵션 수동입력 — {opt_type}: {opt_val}")

        # ── 카테고리 가이드 기준 유효 옵션 필터링 ──────────────────────────
        _guide_valid = _valid_option_types(entry.category_id) if entry.category_id else []

        # category_options.json에 없는 카테고리 → Gemini로 valid_options 조회
        if entry.category_id and not _guide_valid:
            _gk_opt = getattr(_settings, "GEMINI_API_KEY", "")
            _gm_opt = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
            if _gk_opt:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_opt)
                    _cur_cat_label_opt = ""
                    for _wc in _get_wing_cat_flat():
                        if str(_wc.get("code", "")) == entry.category_id:
                            _cur_cat_label_opt = _wc.get("label") or _wc.get("name", "")
                            break
                    _opt_prompt = (
                        f"상품명(한국어): {product.name}\n"
                        f"쿠팡 카테고리: {_cur_cat_label_opt} (ID: {entry.category_id})\n\n"
                        "이 쿠팡 카테고리에서 구매옵션으로 허용되는 옵션 유형을 아는 것만 답하세요.\n"
                        "형식: 옵션유형1,옵션유형2 (쉼표 구분, 최대 3개)\n"
                        "예시 가능한 옵션유형: 수량,향/맛,용량,색상,사이즈,개당 용량,개당 중량,종류\n"
                        "반드시 '수량'은 포함하세요. 확실하지 않으면 '수량'만 답하세요.\n"
                        "옵션유형 목록만 답하세요. 설명 금지."
                    )
                    _gresp_opt = await loop.run_in_executor(
                        None, lambda: genai.GenerativeModel(_gm_opt).generate_content(_opt_prompt)
                    )
                    _gtxt_opt = (_gresp_opt.text or "").strip().split("\n")[0]
                    # "총 수량", "총수량" 등 수량 동의어 → "수량"으로 정규화
                    _QTY_SYNONYMS = {"총 수량", "총수량", "수량(개)", "개수"}
                    _gemini_valid = [
                        "수량" if o.strip() in _QTY_SYNONYMS else o.strip()
                        for o in _gtxt_opt.split(",") if o.strip()
                    ]
                    if _gemini_valid:
                        _guide_valid = _gemini_valid
                        log_(f"[{entry.uid[:6]}] ✅ Gemini 유효옵션 조회: {_guide_valid}")
                    else:
                        log_(f"[{entry.uid[:6]}] ⚠ Gemini 유효옵션 응답 없음 — 필터링 생략")
                except Exception as _oe:
                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 유효옵션 조회 실패: {_oe}")

        if _guide_valid:
            _before = len(extra_options)
            extra_options = [(t, v) for t, v in extra_options if t in _guide_valid]
            _removed = _before - len(extra_options)
            if _removed:
                log_(f"[{entry.uid[:6]}] ⚠ 카테고리 불허 옵션 {_removed}개 제거 "
                     f"(허용: {_guide_valid})")

        # ── 가이드 필수 옵션 자동 보완 (미감지 시 상품명 재추출 or 기본값) ────
        # 수량 제외 — 수량은 항상 슬롯0으로 별도 처리
        _existing_types = {t for t, _ in extra_options}
        _pname = product.name

        # 개당 용량 (올리브오일, 식초 등 액체 식품 카테고리 필수)
        if '개당 용량' in (_guide_valid or []) and '개당 용량' not in _existing_types:
            _vp_rv, _vp_ru, _vp_rl = _parse_volume(_pname)
            if _vp_rl == 0:   # 상품명 실패 → raw_json fallback
                _vp_rv, _vp_ru, _vp_rl = _parse_volume_from_json(product.raw_json)
                if _vp_rl > 0:
                    log_(f"[{entry.uid[:6]}] 개당 용량 raw_json fallback 성공")
            if _vp_rl > 0:
                _vstr = f"{_vp_rv:g}{_vp_ru}"
                # ≤2ml는 고체 식품 할루시네이션 가능성 — 스킵
                if _vp_ru == "ml" and _vp_rv <= 2:
                    log_(f"[{entry.uid[:6]}] 개당 용량 보완 스킵 (≤2ml 의심값): {_vstr}")
                else:
                    extra_options.append(("개당 용량", _vstr))
                    log_(f"[{entry.uid[:6]}] 개당 용량 보완: {_vstr}")

        # 개당 중량 (식품 등) — required_options에 있을 때만 자동 보완
        # valid_options에만 있고 required가 아닌 경우(캡슐세제 등) 중량을 옵션에 넣으면 불필요한 중복 옵션 발생
        _guide_req_wt = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", []) if entry.category_id else []
        if '개당 중량' in (_guide_valid or []) and '개당 중량' in _guide_req_wt and '개당 중량' not in _existing_types:
            _wt2_rv, _wt2_ru, _wt2_rg = _parse_weight(_pname)
            if _wt2_rg == 0:  # 상품명 실패 → raw_json fallback
                _wt2_rv, _wt2_ru, _wt2_rg = _parse_weight_from_json(product.raw_json)
                if _wt2_rg > 0:
                    log_(f"[{entry.uid[:6]}] 개당 중량 raw_json fallback 성공")
            if _wt2_rg > 0:
                _wstr = f"{_wt2_rv:g}{_wt2_ru}"
                extra_options.append(("개당 중량", _wstr))
                log_(f"[{entry.uid[:6]}] 개당 중량 보완: {_wstr}")

        # ── g + ml 배타 처리 ──────────────────────────────────────────
        # 개당 용량(ml)과 개당 중량(g)은 쿠팡에서 배타적 옵션 — 수치 무관하게 하나만 유지
        # 카테고리 required_options 기준으로 유효한 단위를 선택
        _vol_val = next((v for t, v in extra_options if t == "개당 용량"), None)
        _wt_val  = next((v for t, v in extra_options if t == "개당 중량"), None)
        if _vol_val and _wt_val:
            _req_opts = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", [])
            _need_vol = "개당 용량" in _req_opts
            _need_wt  = "개당 중량" in _req_opts
            # 액체 판별: 용량 수치 파싱 (≥ 50ml이면 액체 식품으로 간주)
            try:
                _vol_num_str = _re.sub(r'[^\d.]', '', _vol_val)
                _vol_unit_str = _re.sub(r'[\d. ]', '', _vol_val).lower()
                _vol_ml_val = float(_vol_num_str) * (1000 if _vol_unit_str in ('l',) else 1)
                _is_liquid = _vol_ml_val >= 50
            except Exception:
                _is_liquid = False
            if _need_vol and not _need_wt:
                # 액체류 카테고리 — 용량 유지, 중량 제거
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 개당 중량 배타 제거 (카테고리 필수: 용량 {_vol_val} 유지)")
            elif _need_wt and not _need_vol:
                # 고체류 카테고리 — 중량 유지, 용량 제거
                extra_options = [(t, v) for t, v in extra_options if t != "개당 용량"]
                log_(f"[{entry.uid[:6]}] 개당 용량 배타 제거 (카테고리 필수: 중량 {_wt_val} 유지)")
            elif _need_vol and _need_wt and _is_liquid:
                # 둘 다 required지만 액체 식품(≥50ml) — 용량 우선, 중량 제거
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 개당 중량 배타 제거 (액체 식품 {_vol_val} ≥ 50ml, 용량 우선)")
            elif not _need_vol and not _need_wt:
                # required 미지정 — 용량 우선 유지 (기본값)
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 개당 중량 배타 제거 (required 미지정, 용량 {_vol_val} 우선)")

        # 개당 중량 추가 안전망: 카테고리 required_options에 없으면 제거
        # Gemini가 앞단에서 이미 추가했더라도 required가 아닌 중량 옵션은 Wing 상위노출에 불리
        _req_opts_wt_chk = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", []) if entry.category_id else []
        if '개당 중량' not in _req_opts_wt_chk:
            _wt_extra = [v for t, v in extra_options if t == '개당 중량']
            if _wt_extra:
                extra_options = [(t, v) for t, v in extra_options if t != '개당 중량']
                log_(f"[{entry.uid[:6]}] 개당 중량 제거 (카테고리 required 아님: {_wt_extra[0]})")

        # 색상 (Wing 카테고리 필수이나 실제 색상 없는 상품 — 기본값 삽입)
        # ⚠️ 단, 번들이 여러 개인데 상품명에서 실제 색상 단어를 찾지 못한 경우:
        #    모든 번들에 동일한 색상값("기본"/브랜드명 등)이 들어가면
        #    Wing이 "중복된 옵션값" 오류를 냄 → 색상 옵션을 아예 삽입하지 않음.
        if '색상' in (_guide_valid or []) and '색상' not in _existing_types:
            _color_kw = _re.search(
                r'(화이트|블랙|블루|레드|그린|옐로|실버|골드|베이지|그레이|투명|클리어'
                r'|White|Black|Blue|Red|Green|Yellow|Silver|Gold|Grey|Gray|Clear)',
                _pname, _re.I
            )
            if _color_kw:
                # 실제 색상 단어 감지 → 모든 번들에 동일값이지만 진짜 색상이므로 삽입
                _color_val = _color_kw.group(1).capitalize()
                extra_options.append(("색상", _color_val))
                log_(f"[{entry.uid[:6]}] 색상 보완: {_color_val}")
            elif len(entry.qtys) <= 1:
                # 단품(번들 1개)이면 중복 문제 없으므로 "단일색상" 삽입
                extra_options.append(("색상", "단일색상"))
                log_(f"[{entry.uid[:6]}] 색상 보완(단품): 단일색상")
            else:
                # 번들 여러 개 + 실제 색상 없음 → 삽입 시 전 행이 동일값 → 중복 오류
                # 색상 옵션 자체를 생략 (Wing 필수 요구에도 불구하고 더 나은 선택)
                log_(f"[{entry.uid[:6]}] ⚠ 색상 보완 생략 — 번들 {len(entry.qtys)}개이고 "
                     f"실제 색상 미감지 → 중복 옵션값 오류 방지")

        # ── Gemini 필수 옵션 최종 보완 ────────────────────────────────────
        # 기존 파싱으로 채워지지 않은 required_options를 Gemini가 최종 채움.
        # 세트 상품 / 네이버 옵션(향·색상) 상품은 스킵 — 이미 올바른 구조로 채워짐.
        # Gemini 실패 시 → 기존 extra_options 그대로 유지 (안전 fallback 내장).
        if (
            not _IS_SET
            and not naver_option_rows   # 네이버 옵션(향/색상) 이미 있으면 스킵
            and entry.category_id
            and _guide_valid
        ):
            _req_opts_for_gemini = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", [])
            _gemini_key_opt  = getattr(_settings, "GEMINI_API_KEY", "")
            _gemini_model_opt = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")

            # 카테고리 레이블 조회
            _cat_label_g = ""
            for _wc in _get_wing_cat_flat():
                if str(_wc.get("code", "")) == entry.category_id:
                    _cat_label_g = _wc.get("label") or _wc.get("name", "")
                    break

            extra_options = await _gemini_fill_required_options(
                product_name     = product.name,
                raw_json         = product.raw_json,
                naver_category   = product.naver_category,
                category_id      = entry.category_id,
                category_label   = _cat_label_g,
                required_options = _req_opts_for_gemini,
                valid_options    = _guide_valid,
                existing_options = extra_options,
                gemini_api_key   = _gemini_key_opt,
                model            = _gemini_model_opt,
                loop             = loop,
                log_fn           = log_,
                uid              = entry.uid,
            )

        # ── Gemini 후 중량/용량 배타 최종 정리 ────────────────────────────
        # Gemini가 기존 dedup 이후 중량·용량을 재추가하는 경우를 잡기 위한 2차 필터
        _WT_TYPES = {"개당 중량", "중량", "무게", "순중량", "최소 중량"}
        _VL_TYPES = {"개당 용량", "용량", "내용량", "최소 용량"}
        _final_vol = next((v for t, v in extra_options if t in _VL_TYPES), None)
        _final_wt  = next((v for t, v in extra_options if t in _WT_TYPES), None)
        if _final_vol and _final_wt:
            _req_f = set(_CAT_OPTIONS.get(entry.category_id, {}).get("required_options", [])) if entry.category_id else set()
            _need_vol_f = bool(_req_f & _VL_TYPES)
            _need_wt_f  = bool(_req_f & _WT_TYPES)
            # 용량 수치 파싱 — 50ml 이상이면 액체로 간주
            try:
                _vn = float(_re.sub(r'[^\d.]', '', _final_vol) or "0")
                _vu = _re.sub(r'[\d. ]', '', _final_vol).lower()
                _vml_f = _vn * 1000.0 if _vu in ('l',) else _vn
                _is_liq_f = _vml_f >= 50
            except Exception:
                _is_liq_f = False
            if _need_vol_f and not _need_wt_f:
                extra_options = [(t, v) for t, v in extra_options if t not in _WT_TYPES]
                log_(f"[{entry.uid[:6]}] [후처리] 중량 제거 (카테고리 용량 우선: {_final_vol})")
            elif _need_wt_f and not _need_vol_f:
                extra_options = [(t, v) for t, v in extra_options if t not in _VL_TYPES]
                log_(f"[{entry.uid[:6]}] [후처리] 용량 제거 (카테고리 중량 우선: {_final_wt})")
            elif _is_liq_f:
                # 명확한 액체(≥50ml) → 용량 유지, 중량 제거
                extra_options = [(t, v) for t, v in extra_options if t not in _WT_TYPES]
                log_(f"[{entry.uid[:6]}] [후처리] 중량 제거 (액체 ≥50ml — 용량 {_final_vol} 우선)")
            else:
                # 소용량(≤49ml) 또는 g 단위 → 고체 추정 → 중량 유지, 용량 제거
                extra_options = [(t, v) for t, v in extra_options if t not in _VL_TYPES]
                log_(f"[{entry.uid[:6]}] [후처리] 용량 제거 (고체 추정 — 중량 {_final_wt} 우선)")

        # ── 수량 옵션 허용 여부 확인 ──────────────────────────────────────
        # 카테고리 가이드에 '수량'이 없으면 수량 슬롯 생략 → extra_options[0]부터 슬롯0 사용
        # 예: 63726 커피그라인더 → 모델명/품번만 허용
        # "총 수량" 등 동의어도 수량 허용으로 간주
        _QTY_SYNONYMS_CHECK = {"수량", "총 수량", "총수량", "수량(개)", "개수"}
        _qty_as_option = any(o in _QTY_SYNONYMS_CHECK for o in _guide_valid) if _guide_valid else True

        # 실제 옵션 유형 이름 결정 (카테고리가 "총 수량" 요구 시 그 이름 사용)
        # 예: 컵라면 → "총 수량", 일반 → "수량"
        _qty_option_type = "수량"  # 기본값
        if _guide_valid:
            for _o in _guide_valid:
                if _o in _QTY_SYNONYMS_CHECK:
                    _qty_option_type = _o  # 카테고리가 요구하는 정확한 이름 사용
                    break

        # ── 고정값 옵션 제거 (번들 수와 무관하게 값이 1개뿐인 옵션 → Wing 중복 오류 유발) ──
        # 예: 개당 중량=32g이 14행 전부 동일 → 옵션 중복으로 등록 실패
        # 단, 번들이 1개뿐인 상품은 제거하지 않음 (비교 불가)
        # 제거된 값은 _removed_opt_vals에 백업 → Gemini 폴백 실패 시 복원에 사용
        _removed_opt_vals: dict = {}
        if len(entry.qtys) > 1:
            # 카테고리 필수 옵션은 값이 고정이어도 제거하지 않음
            _cat_required = set(_CAT_OPTIONS.get(entry.category_id, {}).get("required_options", []))
            _fixed_opts = [
                t for t, v in extra_options
                if extra_options.count((t, v)) == len([x for x in extra_options if x[0] == t])
                and t not in _cat_required
            ]
            if _fixed_opts:
                _removed_opt_vals = {t: v for t, v in extra_options if t in _fixed_opts}
                extra_options = [(t, v) for t, v in extra_options if t not in _fixed_opts]
                log_(f"[{entry.uid[:6]}] ⚠ 고정값 옵션 제거 (Wing 중복 방지): {_fixed_opts}")

        # 상품명에서 모델번호 패턴 추출 (예: KG79, KG521, WMF-100 등)
        _model_match = _re.search(r'[A-Z]{1,4}[-]?\d{2,6}[A-Z0-9]*', product.name)
        _extracted_model = _model_match.group(0) if _model_match else ""

        # 수량 옵션 불허 카테고리: 모델명/품번이 필수 옵션이면 모델번호를 extra_options에 추가
        if not _qty_as_option and _guide_valid:
            _first_opt = _guide_valid[0]
            # 이미 해당 옵션이 없으면 추출된 모델번호 or 브랜드명으로 채움
            if not any(t == _first_opt for t, _ in extra_options):
                _opt_val = _extracted_model or entry.brand or product.name[:20]
                extra_options.insert(0, (_first_opt, _opt_val))
                log_(f"[{entry.uid[:6]}] 수량 옵션 불허 카테고리 → {_first_opt}={_opt_val} 슬롯0 배치")

        # ── Gemini 옵션 값 폴백 ──────────────────────────────────────────
        # _guide_valid에 필요한 옵션 유형이 있는데 extra_options에 없는 경우 Gemini로 값 추출
        _existing_types_final = {t for t, _ in extra_options}
        # 수량 동의어는 수량 슬롯(slot 0)으로 처리되므로 missing_types에서 제외
        _missing_types = [
            t for t in (_guide_valid or [])
            if t not in _existing_types_final and t not in _QTY_SYNONYMS_CHECK
        ]
        if _missing_types:
            _gk_ov = getattr(_settings, "GEMINI_API_KEY", "")
            _gm_ov = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
            if _gk_ov:
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_ov)
                    _ov_prompt = (
                        f"상품명(한국어): {product.name}\n"
                        f"네이버 카테고리: {product.naver_category or '없음'}\n\n"
                        f"다음 옵션 유형의 값을 이 상품에서 추출하세요: {', '.join(_missing_types)}\n"
                        "형식: 옵션유형=값 (줄바꿈으로 구분)\n"
                        "예:\n향/맛=레몬\n개당 중량=69g\n색상=기본\n"
                        "규칙:\n"
                        "- 값을 알 수 없으면 해당 줄 생략.\n"
                        "- '개당 용량'은 액상·겔·크림·세럼·음료 등 실제 부피 있는 제품에만 기입. "
                        "고체 식품(과자·사탕·젤리·건어물 등)이나 고체 상품(스틱형 데오드란트·고체 비누·왁스·립밤 등)은 개당 용량 줄 생략.\n"
                        "- 정확한 용량을 모를 때 '1ml', '2ml' 같은 임의 소용량 기입 금지.\n"
                        "설명 금지."
                    )
                    _gresp_ov = await loop.run_in_executor(
                        None, lambda: genai.GenerativeModel(_gm_ov).generate_content(_ov_prompt)
                    )
                    # ── 폴백 결과 단위 검증 (고체 제품 할루시네이션 방지) ──
                    _fb_wt_keywords = {"개당 중량", "최소 중량", "중량", "무게", "순중량"}
                    _fb_vl_keywords = {"개당 용량", "최소 용량", "용량", "내용량"}
                    _fb_WT_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(g|kg)', _re.I)
                    _fb_VL_RE = _re.compile(r'(\d+(?:\.\d+)?)\s*(ml|mL|l|L|cc)', _re.I)
                    # 현재 extra_options 에서 중량 파악 (검증에 사용)
                    _fb_cur_wt_g = None
                    for _fbt, _fbv in extra_options:
                        if _fbt in _fb_wt_keywords:
                            _fbm = _fb_WT_RE.search(_fbv)
                            if _fbm:
                                _fbn = float(_fbm.group(1))
                                _fbu = _fbm.group(2).lower()
                                _fb_cur_wt_g = _fbn * 1000.0 if _fbu == 'kg' else _fbn
                            break
                    for _line in (_gresp_ov.text or "").strip().splitlines():
                        if "=" in _line:
                            _ot, _, _ov = _line.partition("=")
                            _ot = _ot.strip(); _ov = _ov.strip()
                            if not (_ot in _missing_types and _ov and _ot not in _existing_types_final):
                                continue
                            # 용량 옵션: 고체 제품 할루시네이션 방지
                            if _ot in _fb_vl_keywords:
                                _fbvl_m = _fb_VL_RE.search(_ov)
                                if _fbvl_m:
                                    _fbvl_n = float(_fbvl_m.group(1))
                                    _fbvl_u = _fbvl_m.group(2).lower()
                                    _fbvl_ml = _fbvl_n * 1000.0 if _fbvl_u == 'l' else _fbvl_n
                                    if _fbvl_ml <= 2.0:
                                        log_(
                                            f"[{entry.uid[:6]}] ⚠ Gemini 폴백 개당 용량 의심값 거부 "
                                            f"(중량 {_fb_cur_wt_g}g 대비 {_fbvl_ml}ml — 고체 할루시네이션): "
                                            f"{_ot}={_ov}"
                                        )
                                        continue
                                else:
                                    # 단위 없는 용량값 무시
                                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 폴백 용량 단위 없음 무시: {_ot}={_ov}")
                                    continue
                            # 중량 옵션: g/kg 단위 확인
                            if _ot in _fb_wt_keywords:
                                if not _fb_WT_RE.search(_ov):
                                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 폴백 중량 단위 없음 무시: {_ot}={_ov}")
                                    continue
                            extra_options.append((_ot, _ov))
                            _existing_types_final.add(_ot)
                            log_(f"[{entry.uid[:6]}] ✅ Gemini 옵션 값 폴백: {_ot}={_ov}")
                except Exception as _ove:
                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 옵션 값 폴백 실패: {_ove}")
                    # Gemini 실패 시 고정값 제거 전 백업값으로 복원
                    for _mt in _missing_types:
                        if _mt not in _existing_types_final and _mt in _removed_opt_vals:
                            _rv = _removed_opt_vals[_mt]
                            extra_options.append((_mt, _rv))
                            _existing_types_final.add(_mt)
                            log_(f"[{entry.uid[:6]}] ↩ 고정값 복원 (Gemini 실패 폴백): {_mt}={_rv}")

        # ── 최종 배타 필터 (이중 안전장치) ──────────────────────────────
        # Gemini가 g+ml 둘 다 추가했을 경우를 대비해 등록 직전 한 번 더 배타 처리
        _final_vol = next((v for t, v in extra_options if t == "개당 용량"), None)
        _final_wt  = next((v for t, v in extra_options if t == "개당 중량"), None)
        if _final_vol and _final_wt:
            _freq = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", [])
            _fneed_vol = "개당 용량" in _freq
            _fneed_wt  = "개당 중량" in _freq
            try:
                _fvn = _re.sub(r'[^\d.]', '', _final_vol)
                _fvu = _re.sub(r'[\d. ]', '', _final_vol).lower()
                _fvml = float(_fvn) * (1000 if _fvu == 'l' else 1)
                _f_is_liquid = _fvml >= 50
            except Exception:
                _f_is_liquid = False
            if _fneed_vol and not _fneed_wt:
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 최종 배타 필터: 개당 중량 제거 (카테고리 필수: 용량 우선)")
            elif _fneed_wt and not _fneed_vol:
                extra_options = [(t, v) for t, v in extra_options if t != "개당 용량"]
                log_(f"[{entry.uid[:6]}] 최종 배타 필터: 개당 용량 제거 (카테고리 필수: 중량 우선)")
            elif _fneed_vol and _fneed_wt and _f_is_liquid:
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 최종 배타 필터: 개당 중량 제거 (액체 식품 {_final_vol} ≥ 50ml)")
            elif not _fneed_vol and not _fneed_wt:
                extra_options = [(t, v) for t, v in extra_options if t != "개당 중량"]
                log_(f"[{entry.uid[:6]}] 최종 배타 필터: 개당 중량 제거 (required 미지정, 용량 우선)")

        # 최종 안전망: 개당 중량이 required_options에 없으면 마지막으로 한 번 더 제거
        _final_req_wt = _CAT_OPTIONS.get(entry.category_id, {}).get("required_options", []) if entry.category_id else []
        if '개당 중량' not in _final_req_wt:
            _last_wt = [v for t, v in extra_options if t == '개당 중량']
            if _last_wt:
                extra_options = [(t, v) for t, v in extra_options if t != '개당 중량']
                log_(f"[{entry.uid[:6]}] 최종 안전망: 개당 중량 제거 (required 아님, {_last_wt[0]})")

        if extra_options:
            log_(f"[{entry.uid[:6]}] 최종 추가옵션 {len(extra_options)}개: "
                 f"{[(t, v) for t, v in extra_options]}")
        else:
            log_(f"[{entry.uid[:6]}] 추가 옵션 없음 (수량 단일 옵션)")

        # ── 엑셀 등록 전 전 필드 Gemini 전수 검수 + 보완 ───────────────
        # 임시 BulkItem으로 _excel_issues 실행 → critical 오류 전부 Gemini로 수정
        _pre_item_check = BulkItem(
            naver_url=entry.url, product_name=product_name_50,
            brand=entry.brand, category_id=entry.category_id,
            bundles=bundles if 'bundles' in dir() else [],
            main_image_url=main_img if 'main_img' in dir() else "",
            extra_options=extra_options,
            gosisi_cat=entry.gosisi_cat,
        )
        entry.result_item = _pre_item_check
        _pre_issues = [i for i in _excel_issues(entry) if i["severity"] == "critical"]
        entry.result_item = None

        if _pre_issues:
            _gk_fix = getattr(_settings, "GEMINI_API_KEY", "")
            if _gk_fix:
                _fix_fields = {i["field"] for i in _pre_issues}
                log_(f"[{entry.uid[:6]}] ⚠ 엑셀 critical 오류 {len(_pre_issues)}개 → 전수 보완: {_fix_fields}")
                try:
                    import google.generativeai as genai
                    genai.configure(api_key=_gk_fix)
                    _gm_fix = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
                    _gcli_fix = genai.GenerativeModel(_gm_fix)

                    # ① 카테고리 재매핑
                    if "category_id" in _fix_fields:
                        _cp = (
                            f"상품명(한국어): {product.name}\n"
                            f"네이버 카테고리: {product.naver_category or '없음'}\n\n"
                            "이 상품을 쿠팡에 등록할 때 가장 적합한 카테고리 키워드를 쉼표로 3개 제안하세요.\n"
                            "예: 푸딩믹스,과자믹스,베이킹재료\n키워드만 답하세요."
                        )
                        _cr = await loop.run_in_executor(None, lambda: _gcli_fix.generate_content(_cp))
                        for _ckw in (_cr.text or "").strip().split(","):
                            _ckw = _ckw.strip().split()[0] if _ckw.strip() else ""
                            if len(_ckw) >= 2:
                                _cres = detector._fallback_wing_search(_ckw)
                                if _cres:
                                    entry.category_id, entry.gosisi_cat, _cm = _cres
                                    entry.detected_keyword = f"[보완]{_cm}"
                                    log_(f"[{entry.uid[:6]}] ✅ 카테고리 재보완: '{_ckw}' → ID={entry.category_id}")
                                    break
                        if not entry.category_id:
                            log_(f"[{entry.uid[:6]}] ⚠ 카테고리 재보완 실패 — 수동 입력 필요")

                    # ② 브랜드 재보완
                    if "brand" in _fix_fields:
                        _fp = (
                            f"상품명: {product.name}\n네이버 카테고리: {product.naver_category or '없음'}\n\n"
                            "이 상품의 제조사/브랜드를 한국어 또는 영문으로만 답하세요 (최대 30자). 모르면 '해당없음'."
                        )
                        _fr = await loop.run_in_executor(None, lambda: _gcli_fix.generate_content(_fp))
                        _ft = (_fr.text or "").strip().split("\n")[0].strip()
                        if _ft and _ft.lower() not in ("unknown", "해당없음", "없음", ""):
                            entry.brand = _ft
                            log_(f"[{entry.uid[:6]}] ✅ 브랜드 재보완: '{entry.brand}'")

                    # ③ 상품명 재보완
                    if "product_name" in _fix_fields:
                        _np = (
                            f"원본 상품명: {product.name}\n브랜드: {entry.brand or '없음'}\n\n"
                            "쿠팡 등록용 한국어 상품명 10~40자로만 답하세요."
                        )
                        _nr = await loop.run_in_executor(None, lambda: _gcli_fix.generate_content(_np))
                        _nt = (_nr.text or "").strip().split("\n")[0].strip()[:50]
                        if len(_nt.strip()) >= 5:
                            product_name_50 = _nt
                            log_(f"[{entry.uid[:6]}] ✅ 상품명 재보완: '{product_name_50}'")

                    # ④ 불허 옵션 재처리 — 허용 옵션 기준으로 Gemini 재추출
                    if "extra_options" in _fix_fields and entry.category_id:
                        _fv = _valid_option_types(entry.category_id)
                        if _fv:
                            # 불허 옵션 제거
                            extra_options = [(t, v) for t, v in extra_options if t in _fv]
                            # 여전히 빠진 필수 옵션이 있으면 Gemini로 값 추출
                            _fmissing = [t for t in _fv if t not in {tp for tp, _ in extra_options} and t not in _QTY_SYNONYMS_CHECK]
                            if _fmissing:
                                _op = (
                                    f"상품명: {product.name}\n\n"
                                    f"다음 옵션 유형의 값을 추출하세요: {', '.join(_fmissing)}\n"
                                    "형식: 옵션유형=값 (줄바꿈 구분)\n설명 금지."
                                )
                                _or = await loop.run_in_executor(None, lambda: _gcli_fix.generate_content(_op))
                                for _ol in (_or.text or "").strip().splitlines():
                                    if "=" in _ol:
                                        _ot, _, _ov = _ol.partition("=")
                                        _ot, _ov = _ot.strip(), _ov.strip()
                                        if _ot in _fmissing and _ov:
                                            extra_options.append((_ot, _ov))
                                            log_(f"[{entry.uid[:6]}] ✅ 옵션 재보완: {_ot}={_ov}")

                    # ⑤ gosisi_cat 보정 — category_options.json 우선, 없으면 Gemini
                    if entry.category_id:
                        _gg = _guide_gosisi_cat(entry.category_id)
                        if _gg:
                            if entry.gosisi_cat != _gg:
                                log_(f"[{entry.uid[:6]}] gosisi_cat 최종 보정: '{entry.gosisi_cat}' → '{_gg}'")
                            entry.gosisi_cat = _gg
                        elif not entry.gosisi_cat or entry.gosisi_cat in ("기타 재화", ""):
                            _gp = (
                                f"상품명: {product.name}\n쿠팡 카테고리ID: {entry.category_id}\n\n"
                                "이 상품의 쿠팡 상품고시정보 카테고리를 하나만 답하세요.\n"
                                "예: 가공식품  생활용품  화장품  기타 재화\n"
                                "카테고리명만 답하세요."
                            )
                            _gr = await loop.run_in_executor(None, lambda: _gcli_fix.generate_content(_gp))
                            _gcat = (_gr.text or "").strip().split("\n")[0].strip()
                            if _gcat and len(_gcat) < 40:
                                entry.gosisi_cat = _gcat
                                log_(f"[{entry.uid[:6]}] ✅ gosisi_cat Gemini 보완: '{_gcat}'")

                except Exception as _fe:
                    log_(f"[{entry.uid[:6]}] ⚠ Gemini 전수 보완 실패: {_fe}")
            else:
                log_(f"[{entry.uid[:6]}] ⚠ GEMINI_API_KEY 미설정 — critical 오류 수동 수정 필요")

        # ── 최종 안전망: category_options.json 기준으로 불허 옵션 재필터링 ──
        # Gemini 보완 등으로 불허 옵션이 끼어들어도 이 단계에서 제거
        _final_valid = _valid_option_types(entry.category_id) if entry.category_id else []
        if _final_valid:
            _before_final = len(extra_options)
            extra_options = [(t, v) for t, v in extra_options if t in _final_valid]
            if len(extra_options) < _before_final:
                log_(f"[{entry.uid[:6]}] 최종 안전망: 불허 옵션 {_before_final - len(extra_options)}개 제거")

        entry.result_item = BulkItem(
            naver_url=entry.url,
            product_name=product_name_50,
            brand=entry.brand,
            category_id=entry.category_id,    # 자동감지 or 수동입력 카테고리 ID
            bundles=bundles,
            main_image_url=main_img,
            extra_image_urls=[],   # 추가이미지 미등록 (1개 배지 이미지 중복 방지)
            origin="수입산",
            tags=[],               # 검색어 미입력 (형식 오류 방지)
            detail_description=detail_html,
            extra_options=extra_options,
            gosisi_cat=entry.gosisi_cat,
            draft=entry.draft,     # 임시저장 모드: True면 판매시작일 공란 → Wing 임시저장
            gtin=entry.gtin,
            qty_unit=_qty_unit,    # "개"(일반) / "세트"(교환세트)
            lead_time=entry.lead_time,  # 국내 3일 / 해외 10일 (처리 후 전환 가능)
            qty_as_option=_qty_as_option,
            qty_option_type=_qty_option_type,  # "수량" or "총 수량" 등 카테고리별 정확한 이름
            model_number=_extracted_model,  # 상품명에서 추출한 모델번호 (모델번호 컬럼 기입)
        )
        log_(f"[{entry.uid[:6]}] ✅ 완료: {product_name_50}")

        # ── 중복 감지 파이프라인 ──────────────────────────────────────
        # 수집 완료 직후 이력 파일과 비교 → 중복/유사 상품 경고
        try:
            _dup_api_key = getattr(_settings, "GEMINI_API_KEY", "")
            _dup_model   = getattr(_settings, "GEMINI_MODEL", "gemini-2.5-flash")
            _vol_str     = f"{entry.volume:g}{entry.volume_unit}" if entry.volume else ""
            log_(f"[{entry.uid[:6]}] 중복 감지 파이프라인 실행 중...")
            _dup_result = await loop.run_in_executor(
                None,
                lambda: _run_duplicate_check(
                    brand=entry.brand or "",
                    category_id=entry.category_id or "",
                    product_name=product_name_50,
                    api_key=_dup_api_key,
                    model=_dup_model,
                    gtin=entry.gtin or "",
                ),
            )
            _dup_status = _dup_result.get("status", "clean")
            if _dup_status != "clean":
                _matched    = _dup_result.get("matched") or {}
                entry.dup_status       = _dup_status
                entry.dup_reason       = _dup_result.get("reason", "")
                entry.dup_matched_name = _matched.get("product_name", "")
                entry.dup_matched_date = _matched.get("registered_at", "")[:10]
                _icon = "🔴" if _dup_status == "duplicate" else ("🟡" if _dup_status == "variant" else "🟠")
                log_(f"[{entry.uid[:6]}] {_icon} 중복 감지: {_dup_status} — {entry.dup_reason} "
                     f"(과거: {entry.dup_matched_name[:30]})")
            else:
                log_(f"[{entry.uid[:6]}] ✅ 중복 없음 (이력 비교 완료)")
        except Exception as _de:
            log_(f"[{entry.uid[:6]}] ⚠ 중복 감지 오류 (무시): {_de}")

    except Exception as exc:
        entry.error = str(exc)
        log_(f"[{entry.uid[:6]}] ❌ 오류: {exc}")
        log_(traceback.format_exc())


# ── 전역 앱 상태 (페이지 이동 후에도 유지) ───────────────────────
_global_queue:         list[QueueEntry] = []
_global_template_path: dict[str, str]  = {"v": ""}
_global_output_file:   dict[str, str]  = {"v": ""}
_global_running:       dict[str, bool] = {"v": False}
_global_stop_req:      dict[str, bool] = {"v": False}  # 중단 요청 플래그
_global_wing_running:  dict[str, bool] = {"v": False}  # Wing 자동화 전용 플래그
_global_price_checking: dict = {"v": False, "done": 0, "total": 0}  # 가격 체크 진행 중 플래그
_global_log_buffer:    list[str]       = []          # 로그 전역 버퍼
_global_wing_log_buffer: list[str]     = []          # Wing 판매요청 로그 전역 버퍼
_global_task:          "asyncio.Task | None" = None  # 백그라운드 처리 Task
# 마지막 배치 메타 (파일명·크기·URL별 수량) — 재작업 복원용
_last_batch_meta:      dict[str, object] = {"v": {}}
_pending_batch_meta:   dict[str, object] = {"v": {}}  # 업로드된 파일 식별 정보 (처리 시작 전 임시보관)
_global_timing:        dict = {"item_times": [], "item_start": 0.0}  # 상품별 처리시간 측정 (ETA 계산용)

_HISTORY_FILE = Path(__file__).parent / "data" / "collection_history.json"
_MAX_HISTORY  = 20  # 최근 N회 보관


def _save_collection_history(
    entries: list[QueueEntry],
    shipping_mode: str = "domestic",
    margin_rate: float = 1.35,
) -> None:
    """현재 큐를 최근 수집목록 JSON에 저장. 최대 _MAX_HISTORY 회 보관."""
    try:
        import json as _j
        history: list[dict] = []
        if _HISTORY_FILE.exists():
            try:
                history = _j.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []

        # 직렬화할 필드만 추출
        serialized = []
        for e in entries:
            serialized.append({
                "url":               e.url,
                "brand":             e.brand,
                "qtys":              e.qtys,
                "min_qty":           e.min_qty,
                "qty_locked":        e.qty_locked,
                "volume":            e.volume,
                "volume_unit":       e.volume_unit,
                "use_nobg":          e.use_nobg,
                "draft":             e.draft,
                "gtin":              e.gtin,
                "source_file":       e.source_file,
                "gosisi_cat":        e.gosisi_cat,
                "category_id":       e.category_id,
                "category_is_manual": e.category_is_manual,
                "manual_options":    e.manual_options,
                "lead_time":         e.lead_time,
                "watch_store":       getattr(e, "watch_store", "샵케이"),
                "price_extra":       getattr(e, "price_extra", 0),
                "extra_detail_images": list(getattr(e, "extra_detail_images", None) or []),
                "extra_detail_text": getattr(e, "extra_detail_text", ""),
            })

        if not serialized:
            return

        # 레이블: 소스 파일 목록 요약
        _files = [e["source_file"] for e in serialized if e.get("source_file")]
        _unique_files = list(dict.fromkeys(_files))  # 순서 유지 중복제거
        if _unique_files:
            _file_label = ", ".join(_unique_files[:2])
            if len(_unique_files) > 2:
                _file_label += f" 외 {len(_unique_files)-2}개"
        else:
            _file_label = "직접 추가"

        # 대표 브랜드/상품명 (최대 2개)
        _brands = [e["brand"] for e in serialized if e.get("brand") and e["brand"] != "해당없음"]
        _brand_label = ""
        if _brands:
            _uniq_b = list(dict.fromkeys(_brands))
            _brand_label = " · ".join(_uniq_b[:2])
            if len(_uniq_b) > 2:
                _brand_label += f" 외 {len(_uniq_b)-2}개"

        import datetime as _dt
        run: dict = {
            "id":            _dt.datetime.now().strftime("%Y%m%d_%H%M%S"),
            "timestamp":     _dt.datetime.now().isoformat(timespec="seconds"),
            "label":         _file_label,
            "brand_label":   _brand_label,
            "count":         len(serialized),
            "shipping_mode": shipping_mode,
            "margin_rate":   margin_rate,
            "entries":       serialized,
        }

        history.insert(0, run)        # 최신이 맨 앞
        history = history[:_MAX_HISTORY]
        _HISTORY_FILE.write_text(
            _j.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as _he:
        print(f"[히스토리저장] 오류 (무시): {_he}")


def _load_collection_history() -> list[dict]:
    """최근 수집목록 JSON 로드."""
    try:
        import json as _j
        if _HISTORY_FILE.exists():
            data = _j.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


# ── 큐 상태 자동저장/복원 ───────────────────────────────────────────
_QUEUE_STATE_FILE = Path(__file__).parent / "data" / "queue_state.json"

# 저장할 QueueEntry 필드 목록
_QUEUE_SAVE_FIELDS = (
    "uid", "url", "brand", "qtys", "min_qty",
    "volume", "volume_unit",
    "use_nobg", "draft", "gtin",
    "source_file", "gosisi_cat",
    "category_id", "category_is_manual",
    "qty_locked", "naver_price",
    "manual_options", "lead_time",
    "single_mode", "single_selected_imgs", "margin_override",
    "watch_store", "price_extra", "extra_detail_images", "extra_detail_text",
    "status",  # 복원 시 processing → pending 으로 리셋
)


def _persist_queue() -> None:
    """_global_queue 전체를 queue_state.json에 저장 (처리 중 포함)."""
    try:
        import json as _j
        rows = []
        for e in _global_queue:
            row: dict = {}
            for f in _QUEUE_SAVE_FIELDS:
                row[f] = getattr(e, f, None)
            rows.append(row)
        _QUEUE_STATE_FILE.write_text(
            _j.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as _pe:
        print(f"[큐상태저장] 오류 (무시): {_pe}")


def _restore_queue_from_state() -> list[QueueEntry]:
    """
    queue_state.json 을 읽어 QueueEntry 리스트로 반환.
    - done   → 그대로 done (이미 처리 완료)
    - error  → pending (재시도)
    - processing / pending → pending
    반환값이 빈 리스트면 복원할 항목 없음.
    """
    try:
        import json as _j
        if not _QUEUE_STATE_FILE.exists():
            return []
        rows = _j.loads(_QUEUE_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or not rows:
            return []
        result: list[QueueEntry] = []
        for row in rows:
            _st = row.get("status", "pending")
            if _st in ("processing", "error", "pending"):
                _st = "pending"
            # done 항목은 복원은 하되 결과 없으니 pending으로 (재수집 필요)
            # 사용자가 원하는 건 "URL 목록 복원" → 모두 pending
            _st = "pending"
            e = QueueEntry(
                uid              = row.get("uid") or __import__("uuid").uuid4().hex[:8],
                url              = row.get("url", ""),
                brand            = row.get("brand", ""),
                qtys             = row.get("qtys") or [1],
                min_qty          = int(row.get("min_qty") or 1),
                volume           = float(row.get("volume") or 0),
                volume_unit      = row.get("volume_unit", "L"),
                use_nobg         = bool(row.get("use_nobg", False)),
                draft            = bool(row.get("draft", False)),
                gtin             = row.get("gtin", ""),
                source_file      = row.get("source_file", ""),
                gosisi_cat       = row.get("gosisi_cat", "기타 재화"),
                category_id      = row.get("category_id", ""),
                category_is_manual = bool(row.get("category_is_manual", False)),
                qty_locked       = bool(row.get("qty_locked", False)),
                naver_price      = int(row.get("naver_price") or 0),
                manual_options   = row.get("manual_options") or [],
                lead_time        = int(row.get("lead_time") or 2),
                single_mode      = bool(row.get("single_mode", False)),
                single_selected_imgs = row.get("single_selected_imgs") or [],
                margin_override  = float(row.get("margin_override") or 0.0),
                watch_store      = row.get("watch_store", "샵케이"),
                price_extra      = int(row.get("price_extra") or 0),
                extra_detail_images = list(row.get("extra_detail_images") or []),
                extra_detail_text   = row.get("extra_detail_text", ""),
                status           = _st,
            )
            if e.url:
                result.append(e)
        return result
    except Exception as _re:
        print(f"[큐상태복원] 오류 (무시): {_re}")
        return []


class _GlobalStream(io.StringIO):
    """백그라운드 처리용 stdout — UI 위젯 없이 전역 버퍼에만 기록."""
    def write(self, text: str) -> int:
        sys.__stdout__.write(text)
        line = text.strip()
        if line:
            _global_log_buffer.append(line)
        return len(text)
    def flush(self) -> None:
        sys.__stdout__.flush()

# ── 백그라운드 처리 함수 (앱 수준 Task — 페이지 이동과 무관) ──────

async def _run_global_processing(margin_rate: float, lead_time: int, use_nobg: bool = False) -> None:
    """
    전체 처리 루프.  asyncio.create_task() 로 띄워 페이지 이동과 무관하게 실행.
    진행 상태는 _global_queue 엔트리에 직접 저장, 로그는 _global_log_buffer에 추가.
    """
    global _global_task
    old_stdout = sys.stdout
    sys.stdout = _GlobalStream()
    _global_running["v"] = True
    _global_stop_req["v"] = False
    _global_timing["item_times"].clear()
    _global_timing["item_start"] = 0.0
    try:
        total_init = sum(1 for e in _global_queue if e.status == "pending")
        print(f"[처리] 시작 — {total_init}개 항목 (연속 업로드 시 자동 추가됨)")

        i = 0
        while True:
            # 중단 요청 확인
            if _global_stop_req["v"]:
                print("[처리] 사용자 요청으로 중단됨")
                _global_stop_req["v"] = False
                break
            # 매 루프마다 새로 추가된 항목 포함해 다음 pending 항목 탐색
            entry = next((e for e in _global_queue if e.status == "pending"), None)
            if entry is None:
                break
            i += 1
            total_now = sum(1 for e in _global_queue if e.status != "pending") + \
                        sum(1 for e in _global_queue if e.status == "pending")
            print(f"[{i}/{total_now}] 처리: {entry.url[:60]}")
            entry.status = "processing"

            # 항목별 누끼 설정 사용 (파일 업로드 시점에 저장된 값)
            _entry_nobg = entry.use_nobg
            # 항목별 배송 설정:
            #   - lead_time_locked=True (뱃지 직접 토글 or 단일등록) → 개별 값 우선
            #   - lead_time_locked=False (기본값) → 좌측 패널 글로벌 설정 사용
            _entry_lead_time = entry.lead_time if entry.lead_time_locked else lead_time
            # 항목별 마진율: margin_override > 0 이면 글로벌 마진 대신 사용 (단일등록 전용)
            _entry_margin = entry.margin_override if entry.margin_override > 0 else margin_rate

            # ── 자동 재시도 (봇 차단 / Unknown 오류 한정) ────────────
            # 최대 3회 시도, 실패 시 점진적 대기 (5s → 15s → 30s)
            _MAX_RETRY  = 3
            _RETRY_WAIT = [5, 15, 30]
            import time as _time_mod
            _global_timing["item_start"] = _time_mod.perf_counter()
            for _attempt in range(_MAX_RETRY):
                entry.error       = ""
                entry.result_item = None
                await _process_entry(entry, None, _entry_margin, _entry_lead_time, _entry_nobg)
                if entry.result_item:
                    break   # 성공
                _is_unknown = "Unknown" in entry.error or "상품명 추출 실패" in entry.error
                if not _is_unknown or _attempt == _MAX_RETRY - 1:
                    break   # Unknown 아닌 오류거나 마지막 시도면 포기
                _wait = _RETRY_WAIT[_attempt]
                print(f"[재시도] ({_attempt+1}/{_MAX_RETRY}) {_wait}초 대기 후 재시도...")
                await asyncio.sleep(_wait)

            _elapsed = _time_mod.perf_counter() - _global_timing["item_start"]
            if 3.0 < _elapsed < 600.0:  # 3초~10분 사이만 신뢰 샘플로 저장
                _global_timing["item_times"].append(_elapsed)
                if len(_global_timing["item_times"]) > 15:
                    _global_timing["item_times"] = _global_timing["item_times"][-15:]

            entry.status = "done" if entry.result_item else "error"

        # ── 카테고리 재감지 (detector 최신 파일 반영 + 기존 done 항목 갱신) ──
        try:
            _fresh = _get_detector()
            _fresh._load()   # category_map.json 재로드 (앱 실행 중 파일 수정 반영)
            for _e in _global_queue:
                if _e.result_item is None or not _e.product_name:
                    continue
                if _e.category_is_manual:
                    continue  # 사용자 수동 선택값은 보존
                _nid, _ngo, _nkw = _fresh.detect(_e.product_name)
                if _nkw and _nid and _nid != _e.category_id:
                    # 세트 카테고리(113070)는 단품 오일 카테고리로 변경하지 않음
                    # — extra_options가 이미 세트용(차종·자동차제조사)으로 구성돼 있어
                    #   카테고리만 바꾸면 Wing에서 불허 옵션 오류 발생
                    if _e.category_id == "113070" and _nid in {"78889", "78897", "78903"}:
                        print(f"[카테고리 갱신] ⚠ 세트→단품 변경 차단 "
                              f"({_e.product_name[:25]}: 113070 → {_nid})")
                        continue
                    # fallback(_fallback_wing_search) 결과는 기존 카테고리가 있을 때 덮어쓰지 않음
                    # — naver_category 없이 상품명만으로 재탐색하면 단어 오매핑 위험이 있음
                    if _nkw.startswith("[자동]") and _e.category_id:
                        print(f"[카테고리 갱신] ⚠ fallback 결과 무시 (기존 카테고리 보존): "
                              f"{_e.product_name[:20]} → {_nkw} 무시")
                        continue
                    print(f"[카테고리 갱신] {_e.product_name[:30]}: "
                          f"{_e.category_id or '없음'} → {_nid} ({_nkw})")
                    _e.category_id = _nid
                    _e.result_item.category_id = _nid
                    _e.gosisi_cat = _ngo
                    _e.detected_keyword = _nkw
        except Exception as _ce:
            print(f"[카테고리 갱신] 오류 (무시): {_ce}")

        # ── 엑셀 생성 ─────────────────────────────────────────────
        success_items = [e.result_item for e in _global_queue if e.result_item]
        if success_items:
            tmpl = _global_template_path.get("v") or ""
            builder = ExcelBuilder(
                template_path=tmpl if tmpl and Path(tmpl).exists() else None,
                output_dir=_OUTPUT_ROOT,
                category_id="",
            )
            loop = asyncio.get_running_loop()
            out_path = await loop.run_in_executor(
                None,
                lambda: builder.build(success_items),  # lead_time은 BulkItem.lead_time per-item 사용
            )
            _global_output_file["v"] = out_path.name
            print(f"[Excel] ✅ 파일 생성 완료: {out_path}")

            # 자동 백업 제거 — 사용자가 다운로드 카드의 "백업 저장" 버튼으로 수동 저장

            # ── 가격 감시목록 자동 등록 ───────────────────────────
            watch_added = 0
            for _e in _global_queue:
                if _e.result_item is None or not _e.url:
                    continue
                # 기준가 = 네이버 원가(1개) — 가격감시는 소싱처 가격 변동을 추적
                # (쿠팡 판매가 기준으로 저장하면 네이버 1개가와 항상 불일치 → 오탐)
                _base_price = _e.naver_price or 0
                _pc.add_watch_with_info(
                    url=_e.url,
                    name=_e.result_item.product_name or _e.product_name,
                    base_price=_base_price,
                    store=getattr(_e, "watch_store", "샵케이"),
                )
                watch_added += 1
            if watch_added:
                print(f"[가격감시] {watch_added}개 URL 자동 등록")

            # ── 수집 완료 텔레그램 알림 ───────────────────────────────
            _done_cnt  = sum(1 for e in _global_queue if e.status == "done")
            _error_cnt = sum(1 for e in _global_queue if e.status == "error")
            _notif_msg = (
                f"✅ <b>[수집 완료]</b>\n"
                f"성공: {_done_cnt}개 / 오류: {_error_cnt}개\n"
                f"엑셀 다운로드 후 Wing에 업로드하세요."
            )
            _send_notification_with_btn(_notif_msg)
        else:
            print("[처리] 완료된 상품이 없습니다 — 오류 로그를 확인하세요.")

    except asyncio.CancelledError:
        # 중단 버튼으로 즉시 취소됨 — 처리 중이던 항목을 pending으로 복원
        for _e in _global_queue:
            if _e.status == "processing":
                _e.status = "pending"
        print("[처리] 즉시 중단됨")
    except Exception as exc:
        print(f"[처리] 오류: {exc}")
        print(traceback.format_exc())
    finally:
        sys.stdout = old_stdout
        _global_running["v"] = False
        _global_task = None
        print("[처리] 백그라운드 Task 종료")
        # ── 수집 완료 후 큐 상태 갱신 저장 ──────────────────────
        # (pending 0개 → 재시작 복원 시 빈 큐로 시작 가능하도록)
        try:
            _persist_queue()
        except Exception:
            pass


# ── Wing 카테고리 계층 트리 + 플랫 검색 목록 (lazy singleton) ────
_WING_CAT_TREE: dict | None = None
_WING_CAT_FLAT: list | None = None   # [{label, code, path}] 검색용


def _get_wing_cat_flat() -> list:
    """전체 카테고리를 플랫 리스트로 반환 (검색용)."""
    global _WING_CAT_FLAT
    if _WING_CAT_FLAT is not None:
        return _WING_CAT_FLAT

    import json as _json
    _cfg = Path(__file__).parent / "config"
    flat: list = []

    for fname in ("wing_categories.json", "wing_beauty_categories.json"):
        fpath = _cfg / fname
        if not fpath.exists():
            continue
        try:
            items = _json.loads(fpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in items:
            code = str(item.get("code", "")).strip()
            path_str = item.get("path", "")
            name = item.get("name", "")
            parts = [p.strip() for p in path_str.split(">") if p.strip() and p.strip() != "ROOT"]
            if not parts or not code:
                continue
            label = " > ".join(parts)
            flat.append({"label": label, "name": name, "code": code})

    _WING_CAT_FLAT = flat
    return flat

def _get_wing_cat_tree() -> dict:
    """
    config/wing_categories.json + wing_beauty_categories.json 로드 →
    { L1: { L2: { L3: "code" | { L4: "code" } } } } 형태의 중첩 dict 반환.
    리프 노드 = str(category code), 중간 노드 = dict.
    """
    global _WING_CAT_TREE
    if _WING_CAT_TREE is not None:
        return _WING_CAT_TREE

    import json as _json
    _cfg = Path(__file__).parent / "config"
    tree: dict = {}

    for fname in ("wing_categories.json", "wing_beauty_categories.json"):
        fpath = _cfg / fname
        if not fpath.exists():
            print(f"[CatTree] {fname} 없음 — 건너뜀")
            continue
        try:
            items = _json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as ex:
            print(f"[CatTree] {fname} 읽기 오류: {ex}")
            continue
        count = 0
        for item in items:
            code  = str(item.get("code", "")).strip()
            path_str = item.get("path", "")
            parts = [p.strip() for p in path_str.split(">")
                     if p.strip() and p.strip() != "ROOT"]
            if not parts or not code:
                continue
            node = tree
            for part in parts[:-1]:
                if not isinstance(node.get(part), dict):
                    node[part] = {}
                node = node[part]
            leaf = parts[-1]
            if leaf not in node:
                node[leaf] = code                   # 일반 리프
            elif isinstance(node[leaf], dict):
                node[leaf]["__code__"] = code        # 중간 노드이자 리프 (희귀)
            count += 1
        print(f"[CatTree] {fname}: {count}개 로드")

    _WING_CAT_TREE = tree
    return tree


# ── 공통 CSS / 헤더 유틸 ────────────────────────────────────────

_COMMON_CSS = """
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ══════════════════════════════════════════
   전체 기반
══════════════════════════════════════════ */
html, body {
  width:100%; margin:0; padding:0;
  background:#0a0a0f !important;
  color:#e2e8f0;
  font-size:15px;
  font-family:'Noto Sans KR', sans-serif;
}
* { box-sizing:border-box; }
.nicegui-content { width:100% !important; max-width:none !important; padding:0 !important; }
#page-wrap { width:100%; padding:0 0 40px; }

/* ══════════════════════════════════════════
   상단 네비바 — 풀 width 그라디언트 바
══════════════════════════════════════════ */
.topbar {
  width:100% !important;
  background: linear-gradient(135deg, #1a0533 0%, #0d1b4b 60%, #0a0a0f 100%) !important;
  border-bottom: 1px solid rgba(139,92,246,0.3) !important;
  padding: 0 24px !important;
  height: 62px !important;
  display: flex !important;
  align-items: center !important;
  gap: 12px !important;
  position: sticky !important;
  top: 0 !important;
  z-index: 100 !important;
  box-shadow: 0 2px 20px rgba(0,0,0,0.5) !important;
}
.topbar-logo {
  font-size:18px !important;
  font-weight:700 !important;
  color:#fff !important;
  letter-spacing:-0.3px;
  margin-right:8px;
  white-space:nowrap;
}
.topbar-logo span {
  background: linear-gradient(90deg, #a78bfa, #60a5fa) !important;
  -webkit-background-clip: text !important;
  -webkit-text-fill-color: transparent !important;
}
/* Quasar q-btn 오버라이드 — .nav-btn 적용 시 */
.topbar .q-btn.nav-btn,
.topbar .q-btn.nav-btn:before {
  padding: 8px 20px !important;
  border-radius: 20px !important;
  font-size: 14px !important;
  font-weight: 600 !important;
  background: transparent !important;
  background-color: transparent !important;
  color: rgba(255,255,255,0.65) !important;
  border: 1px solid transparent !important;
  box-shadow: none !important;
  transition: all .18s !important;
  white-space: nowrap !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
}
.topbar .q-btn.nav-btn .q-btn__content {
  color: rgba(255,255,255,0.65) !important;
}
.topbar .q-btn.nav-btn:hover,
.topbar .q-btn.nav-btn:hover:before {
  background: rgba(167,139,250,0.15) !important;
  background-color: rgba(167,139,250,0.15) !important;
  border-color: rgba(167,139,250,0.3) !important;
}
.topbar .q-btn.nav-btn:hover .q-btn__content { color: #c4b5fd !important; }
.topbar .q-btn.nav-btn.active,
.topbar .q-btn.nav-btn.active:before {
  background: linear-gradient(135deg, #7c3aed, #4f46e5) !important;
  background-color: #7c3aed !important;
  border-color: transparent !important;
  box-shadow: 0 0 14px rgba(124,58,237,0.45) !important;
}
.topbar .q-btn.nav-btn.active .q-btn__content { color: #fff !important; }
.nav-spacer { flex:1; }
.topbar .q-btn.topbar-restart,
.topbar .q-btn.topbar-restart:before {
  padding: 7px 16px !important;
  border-radius: 8px !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  border: 1px solid rgba(255,255,255,0.15) !important;
  background: rgba(255,255,255,0.06) !important;
  background-color: rgba(255,255,255,0.06) !important;
  color: rgba(255,255,255,0.6) !important;
  box-shadow: none !important;
  text-transform: none !important;
  letter-spacing: 0 !important;
}
.topbar .q-btn.topbar-restart:hover,
.topbar .q-btn.topbar-restart:hover:before {
  background: rgba(255,255,255,0.1) !important;
  background-color: rgba(255,255,255,0.1) !important;
}
.topbar .q-btn.topbar-restart .q-btn__content { color: rgba(255,255,255,0.6) !important; }
.topbar .q-btn.topbar-restart:hover .q-btn__content { color: #fff !important; }

/* ══════════════════════════════════════════
   메인 콘텐츠 영역
══════════════════════════════════════════ */
.content-wrap { padding: 24px 20px 40px; }

/* ══════════════════════════════════════════
   카드
══════════════════════════════════════════ */
body.dark-mode .q-card {
  background: #111118 !important;
  color: #e2e8f0 !important;
  border: 1px solid rgba(139,92,246,0.15) !important;
  border-radius: 14px !important;
  box-shadow: 0 4px 20px rgba(0,0,0,0.4) !important;
}
body.dark-mode .q-card__section { background: #111118 !important; }
body.dark-mode .q-separator   { background: rgba(139,92,246,0.15) !important; }

/* ══════════════════════════════════════════
   큐 카드 상태 색상
══════════════════════════════════════════ */
.queue-card {
  border-left: 3px solid #7c3aed !important;
  transition: border-color .2s, box-shadow .2s;
}
.queue-card:hover { box-shadow: 0 4px 24px rgba(124,58,237,0.2) !important; }
.queue-card.done       { border-left-color: #10b981 !important; }
.queue-card.error      { border-left-color: #ef4444 !important; }
.queue-card.processing { border-left-color: #f59e0b !important; box-shadow: 0 0 12px rgba(245,158,11,0.15) !important; }

/* ══════════════════════════════════════════
   텍스트 색상
══════════════════════════════════════════ */
body.dark-mode { background:#0a0a0f !important; color:#e2e8f0 !important; }
body.dark-mode .nicegui-content { background:#0a0a0f !important; }
body.dark-mode .text-slate-800 { color:#e2e8f0 !important; }
body.dark-mode .text-slate-700 { color:#cbd5e1 !important; }
body.dark-mode .text-slate-600 { color:#94a3b8 !important; }
body.dark-mode .text-slate-500 { color:#64748b !important; }
body.dark-mode .text-slate-400 { color:#475569 !important; }

/* ══════════════════════════════════════════
   입력 필드
══════════════════════════════════════════ */
body.dark-mode .q-field__native,
body.dark-mode .q-field__input  { color:#e2e8f0 !important; font-size:15px !important; }
body.dark-mode .q-field__label  { font-size:14px !important; color:#94a3b8 !important; }
body.dark-mode .q-field--outlined .q-field__control {
  border-color: rgba(139,92,246,0.25) !important;
  background: #0d0d16 !important;
  border-radius: 10px !important;
}
body.dark-mode .q-field--outlined .q-field__control:hover { border-color: #7c3aed !important; }
body.dark-mode .q-field--outlined.q-field--focused .q-field__control { border-color:#a78bfa !important; }

/* ══════════════════════════════════════════
   탭
══════════════════════════════════════════ */
body.dark-mode .q-tab-panels { background:#111118 !important; }
body.dark-mode .q-tab-panel  { background:#111118 !important; }
body.dark-mode .q-tab        { color:#64748b !important; font-size:14px !important; font-weight:600; }
body.dark-mode .q-tab.q-tab--active { color:#a78bfa !important; }
body.dark-mode .q-tabs__indicator   { background:#7c3aed !important; height:3px !important; border-radius:2px; }

/* ══════════════════════════════════════════
   버튼
══════════════════════════════════════════ */
body.dark-mode .q-btn { font-size:14px !important; border-radius:10px !important; }
.q-btn.q-btn--rectangle { border-radius:10px !important; }

/* ══════════════════════════════════════════
   수량 suffix / 로그 / URL
══════════════════════════════════════════ */
.q-field__suffix { color:#a78bfa !important; font-weight:700; font-size:15px !important; }
.nicegui-log { white-space:pre-wrap !important; word-break:break-all !important; overflow-x:hidden !important; font-size:13px !important; line-height:1.7 !important; }
.nicegui-log > div { white-space:pre-wrap !important; word-break:break-all !important; }
a.naver-url-link:visited       { color:#475569 !important; }
a.naver-url-link:visited:hover { color:#64748b !important; }

/* ══════════════════════════════════════════
   드롭다운 메뉴
══════════════════════════════════════════ */
.q-menu .q-item        { color:#1e293b !important; font-size:15px !important; }
.q-menu .q-item__label { color:#1e293b !important; }
.q-menu .q-item:hover  { background:#ede9fe !important; }
.q-menu .q-item.q-item--active { color:#7c3aed !important; background:#ede9fe !important; }
.q-menu.bg-dark                        { background:#111118 !important; border:1px solid rgba(139,92,246,0.2) !important; border-radius:10px !important; }
.q-menu.bg-dark .q-item                { color:#e2e8f0 !important; font-size:15px !important; }
.q-menu.bg-dark .q-item__label         { color:#e2e8f0 !important; }
.q-menu.bg-dark .q-item:hover          { background:rgba(124,58,237,0.15) !important; }
.q-menu.bg-dark .q-item.q-item--active { color:#a78bfa !important; background:rgba(124,58,237,0.2) !important; }
.q-field--dark .q-field__native,
.q-field--dark .q-field__input,
.q-field--dark .q-field__label         { color:#e2e8f0 !important; font-size:15px !important; }
.q-field--dark .q-field__control       { border-color:rgba(139,92,246,0.3) !important; }

/* ══════════════════════════════════════════
   깜빡임 애니메이션
══════════════════════════════════════════ */
@keyframes blink-alert {
  0%,100% { opacity:1; }
  50%     { opacity:0.2; }
}
.nav-alert-blink {
  animation: blink-alert 1.1s ease-in-out infinite;
  color: #f87171 !important;
}
/* ── 카테고리 선택 다이얼로그 ─────────────────────────── */
.cat-dlg-select .q-field__control { background:#0f172a !important; }
.cat-dlg-select .q-field__native,
.cat-dlg-select .q-field__input,
.cat-dlg-select input,
.cat-dlg-select .q-field__label { color:#f1f5f9 !important; }
.cat-dlg-select .q-field__marginal,
.cat-dlg-select .q-select__dropdown-icon { color:#94a3b8 !important; }
.cat-search .q-field__control { background:#0f172a !important; }
.cat-search .q-field__native,
.cat-search .q-field__input,
.cat-search input,
.cat-search .q-field__label { color:#f1f5f9 !important; }
.cat-search .q-field__marginal,
.cat-search .q-select__dropdown-icon { color:#94a3b8 !important; }
/* 드롭다운 팝업 전체 — 텍스트 강제 white */
.cat-popup { background:#1e293b !important; border:1px solid #334155 !important; }
.cat-popup .q-item { color:#f1f5f9 !important; background:transparent !important; }
.cat-popup .q-item__label,
.cat-popup .q-item__section,
.cat-popup .q-item span,
.cat-popup .q-item div { color:#f1f5f9 !important; }
.cat-popup .q-item:hover { background:#334155 !important; }
.cat-popup .q-item:hover .q-item__label,
.cat-popup .q-item:hover .q-item__section { color:#f8fafc !important; }
.cat-popup .q-item.q-item--active { background:#0f766e !important; }
.cat-popup .q-item.q-item--active .q-item__label,
.cat-popup .q-item.q-item--active .q-item__section { color:#ccfbf1 !important; }
.cat-tab { color:#94a3b8 !important; }
.cat-tab.q-tab--active { color:#38bdf8 !important; border-bottom:2px solid #38bdf8; }
</style>
"""


def _add_common_head():
    import os as _os
    ui.add_head_html(_COMMON_CSS)
    # 재시작 시 show=True 로 열리는 여분 탭 자동 닫기
    if _os.environ.get("NICEGUI_AUTO_CLOSE_TAB") == "1":
        ui.add_head_html("<script>window.addEventListener('load',function(){window.close();});</script>")
    # 기본 다크모드 — 라이트로 바꾸려면 🌙 버튼 클릭
    ui.add_head_html(
        "<script>document.addEventListener('DOMContentLoaded',function(){"
        "document.body.classList.add('dark-mode');});</script>"
    )
    ui.query("body").style("background:#0a0a0f")
    # WebSocket 킵얼라이브 — 수집 중 브라우저 탭 스로틀링/타임아웃으로 끊기는 현상 방지
    # Chrome/Edge는 백그라운드 탭 WebSocket 을 throttle 할 수 있음.
    # 25초마다 빈 메시지를 보내 브라우저가 연결을 닫지 않도록 유지.
    ui.add_head_html("""<script>
(function(){
  var _kaTimer = null;
  function _startKeepalive(){
    if(_kaTimer) return;
    _kaTimer = setInterval(function(){
      try {
        // NiceGUI 소켓이 열려 있으면 빈 문자열로 ping
        var ws = (window.__ng_ws !== undefined) ? window.__ng_ws
               : (window.sio && window.sio.io && window.sio.io.engine
                  ? window.sio.io.engine.transport
                  : null);
        // socket.io transport ws ping
        if(window.sio && window.sio.io && window.sio.connected){
          window.sio.emit('heartbeat', {});
        }
      } catch(e){}
    }, 25000);
  }
  // DOM 로드 후 시작 (sio 초기화 완료 시점 보장)
  if(document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _startKeepalive);
  } else {
    _startKeepalive();
  }
})();
</script>""")

    # 탭 깜빡임 + 브라우저 알림 함수
    ui.add_head_html("""<script>
(function(){
  var _blinkTimer = null;
  var _origTitle  = document.title;

  window._notifyDone = function(msg, icon) {
    msg  = msg  || '✅ 작업 완료!';
    icon = icon || '/favicon.ico';
    _origTitle = document.title;

    // 1) 탭 제목 깜빡임 (다른 탭에 있어도 보임)
    var blink = true;
    if (_blinkTimer) clearInterval(_blinkTimer);
    _blinkTimer = setInterval(function(){
      document.title = blink ? '🔔 ' + msg : _origTitle;
      blink = !blink;
    }, 800);

    // 탭에 돌아오면 깜빡임 멈춤
    document.addEventListener('visibilitychange', function _stop(){
      if (!document.hidden){
        clearInterval(_blinkTimer);
        _blinkTimer = null;
        document.title = _origTitle;
        document.removeEventListener('visibilitychange', _stop);
      }
    });

    // 2) 브라우저 데스크탑 알림
    function _send(){
      new Notification('네이버→쿠팡 자동화', {body: msg, icon: icon, silent: false});
    }
    if (Notification && Notification.permission === 'granted') {
      _send();
    } else if (Notification && Notification.permission !== 'denied') {
      Notification.requestPermission().then(function(p){
        if (p === 'granted') _send();
      });
    }
  };

  // 페이지 로드 시 알림 권한 미리 요청
  window.addEventListener('load', function(){
    if (Notification && Notification.permission === 'default'){
      Notification.requestPermission();
    }
  });
})();
</script>""")



def _make_nav_header(current: str):
    """공통 상단 네비게이션 바."""
    counts = _pc.alert_counts()
    risen_n     = counts["risen"]
    fallen_n    = counts["fallen"]
    sold_n      = counts["soldout"]
    restocked_n = counts["restocked"]
    has_any  = risen_n + fallen_n + sold_n + restocked_n > 0

    async def _restart_app():
        ui.notify("🔄 앱을 재시작합니다...", type="info", timeout=4000)
        await asyncio.sleep(0.3)
        import os as _os
        _cwd = str(Path(sys.argv[0]).resolve().parent)
        if sys.platform == "win32":
            _bat = str(Path(_cwd) / "run.bat")
            _my_pid = _os.getpid()
            _parent_pids: list[str] = []
            try:
                _r = subprocess.run(
                    f'wmic process where "ProcessId={_my_pid}" get ParentProcessId /value',
                    shell=True, capture_output=True, text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                for _line in _r.stdout.splitlines():
                    _line = _line.strip()
                    if _line.startswith("ParentProcessId="):
                        _ppid = _line.split("=")[1].strip()
                        if _ppid.isdigit() and int(_ppid) > 1:
                            _parent_pids.append(_ppid)
            except Exception:
                pass
            _DETACHED = 0x00000008
            _NEW_CON  = subprocess.CREATE_NEW_CONSOLE
            subprocess.Popen(
                ['cmd', '/c', f'timeout /t 3 /nobreak > nul && call "{_bat}"'],
                cwd=_cwd, creationflags=_DETACHED | _NEW_CON, close_fds=True,
            )
            await ui.run_javascript("window.close()")
            await asyncio.sleep(0.3)
            for _ppid in _parent_pids:
                try:
                    subprocess.run(f"taskkill /PID {_ppid} /F /T", shell=True,
                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=3)
                except Exception:
                    pass
        else:
            _bat = str(Path(sys.argv[0]).resolve().parent / "run.bat")
            subprocess.Popen(
                f'sleep 3 && git -C "{_cwd}" pull --rebase origin main && bash "{_bat}"',
                shell=True, cwd=_cwd, start_new_session=True,
            )
            await ui.run_javascript("window.close()")
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)
        _app.shutdown()

    # ── 풀 width 그라디언트 탑바 ────────────────────────────
    with ui.element("div").classes("topbar"):
        # 로고
        ui.html('<div class="topbar-logo">🛒 <span>네이버 → 쿠팡</span></div>')

        # 네비 버튼
        home_cls = "nav-btn active" if current == "main" else "nav-btn"
        ui.button("📦 등록 자동화", on_click=lambda: ui.navigate.to("/")).classes(home_cls)

        mon_cls = "nav-btn active" if current == "monitor" else "nav-btn"
        if has_any:
            mon_cls += " nav-alert-blink"
        alert_label = "🔔 가격변동알림"
        if risen_n:  alert_label += f" ▲{risen_n}"
        if fallen_n: alert_label += f" ▼{fallen_n}"
        if sold_n:   alert_label += f" 品{sold_n}"
        if restocked_n: alert_label += f" 🟢{restocked_n}"
        ui.button(alert_label, on_click=lambda: ui.navigate.to("/monitor")).classes(mon_cls)

        ui.html('<div class="nav-spacer"></div>')

        ui.button("🔄 재시작", on_click=_restart_app).classes("topbar-restart")


# ── 가격변동 모니터링 페이지 ─────────────────────────────────────

@ui.page("/monitor")
def page_monitor() -> None:
    _add_common_head()

    with ui.element("div").props("id=page-wrap"):
        _make_nav_header("monitor")
        ui.separator()

        ui.label("가격변동 알림").classes("text-2xl font-bold text-slate-800 mb-1")
        ui.label(
            "하루 2회(00:00 / 12:00) 자동 체크 — 가격 상승 / 품절 시 탭에 표시됩니다"
        ).classes("text-sm text-slate-500 mb-3")

        # ── 중단 감지 배너 (자리만 잡아두고, _bg_check_all 정의 후 채움) ──
        _interrupted_banner = ui.element("div").classes("w-full")

        # ── URL 추가 패널 ─────────────────────────────────────────
        with ui.card().classes("shadow-sm w-full mb-4"):
            with ui.card_section():
                ui.label("감시할 URL 추가").classes("font-bold text-slate-700 mb-2")
                with ui.row().classes("items-center gap-2 mb-2 flex-wrap"):
                    ui.label("스토어:").classes("text-xs text-slate-500 font-semibold")
                    _mon_store_sel = ui.select(
                        ["샵케이", "제니스 트레이딩"],
                        value="샵케이",
                    ).props("dense outlined").style("min-width:140px")
                with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                    mon_url_input = ui.input(
                        placeholder="https://smartstore.naver.com/..."
                    ).props("dense outlined clearable").style("flex:1; min-width:280px")

                    async def _on_add_url():
                        raw = (mon_url_input.value or "").strip()
                        if not raw:
                            ui.notify("URL을 입력하세요", type="warning")
                            return
                        # 줄 단위 다중 입력 지원
                        urls = [u.strip() for u in raw.splitlines() if u.strip().startswith("http")]
                        if not urls:
                            urls = [raw]
                        added = 0
                        _sel_store = _mon_store_sel.value or "샵케이"
                        for u in urls:
                            if "smartstore.naver.com" not in u:
                                ui.notify(f"네이버 스마트스토어 URL이 아닙니다: {u[:50]}", type="warning")
                                continue
                            _pc.add_watch(u, store=_sel_store)
                            added += 1
                        mon_url_input.set_value("")
                        ui.notify(f"{added}개 URL 추가 완료", type="positive")
                        _refresh_watch_list()

                    ui.button("추가", icon="add", on_click=_on_add_url).props("color=blue dense")

                    async def _bg_check_all():
                        """탭 이동해도 끊기지 않는 백그라운드 체크 태스크 (병렬 5개)."""
                        loop = asyncio.get_running_loop()

                        def _on_progress(msg, done=None, total=None):
                            """체크 진행 시 UI 상태 업데이트 (스레드 → 메인루프)."""
                            if done is not None and total:
                                _global_price_checking["done"] = done
                                _global_price_checking["total"] = total

                        try:
                            result = await loop.run_in_executor(
                                None, lambda: _pc.check_all(log_cb=_on_progress)
                            )
                        finally:
                            _global_price_checking["v"] = False
                            _global_price_checking["done"] = 0
                            _global_price_checking["total"] = 0

                        r, f_, s = result["risen"], result.get("fallen", 0), result["soldout"]
                        try:
                            check_status.set_text(
                                f"완료 — 상승:{r} 하락:{f_} 품절:{s} 정상:{result['ok']} 오류:{result['errors']}"
                            )
                            check_btn.set_enabled(True)
                            check_spinner.set_visibility(False)
                            _refresh_watch_list()
                            _refresh_risen()
                            _refresh_fallen()
                            _refresh_soldout()
                            _refresh_restocked()
                        except Exception:
                            pass  # 탭 이동으로 UI 소멸 시 무시 — 체크 자체는 완료됨

                    async def _on_check_now():
                        if _global_price_checking["v"]:
                            ui.notify("이미 체크 중입니다.", type="warning")
                            return
                        _global_price_checking["v"] = True
                        _global_price_checking["done"] = 0
                        _global_price_checking["total"] = len(_pc.all_watches())
                        check_btn.set_enabled(False)
                        check_spinner.set_visibility(True)
                        total = _global_price_checking["total"]
                        check_status.set_text(f"체크 중... 0/{total}  (5개 병렬 진행)")
                        asyncio.create_task(_bg_check_all())

                    check_btn = ui.button(
                        "지금 체크", icon="refresh", on_click=_on_check_now
                    ).props("color=teal dense outline")
                    check_spinner = ui.spinner("dots", size="sm", color="teal")
                    check_spinner.set_visibility(False)
                    check_status = ui.label("").classes("text-xs text-slate-500")

                    # ── 중단 감지 배너 채우기 (_bg_check_all 정의 후) ──
                    _cs = _pc.get_check_state()
                    if _cs.get("in_progress"):
                        _cs_done  = _cs.get("done", 0)
                        _cs_total = _cs.get("total", 0)
                        _cs_time  = (_cs.get("started_at") or "")[:16].replace("T", " ")
                        with _interrupted_banner:
                            with ui.card().classes("w-full mb-3 border-l-4 border-orange-400").style("background:#fff7ed"):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-3 flex-wrap"):
                                        ui.icon("warning", color="orange", size="sm")
                                        ui.label(
                                            f"⚠️ 이전 체크가 중단됐습니다 — {_cs_done}/{_cs_total} 완료"
                                            + (f"  ({_cs_time} 시작)" if _cs_time else "")
                                        ).classes("text-sm font-bold text-orange-700 flex-1")
                                        async def _restart_interrupted():
                                            _pc.clear_check_state()
                                            _interrupted_banner.clear()
                                            ui.notify("🔄 감시 재시작 중...", type="info")
                                            await _bg_check_all()
                                        ui.button(
                                            "감시 재시작", icon="refresh",
                                            on_click=_restart_interrupted,
                                        ).props("color=orange dense size=sm")

                    # 탭 돌아왔을 때 체크 중이면 상태 복원
                    if _global_price_checking["v"]:
                        check_btn.set_enabled(False)
                        check_spinner.set_visibility(True)
                        _d = _global_price_checking["done"]
                        _t = _global_price_checking["total"]
                        _prog = f"{_d}/{_t}" if _t else "진행 중"
                        check_status.set_text(f"체크 중... {_prog}  (5개 병렬 진행)")

                    async def _on_reset_all_base():
                        n = await asyncio.get_running_loop().run_in_executor(
                            None, _pc.reset_all_base_prices
                        )
                        ui.notify(f"✅ {n}개 기준가를 현재가로 리셋했습니다.", type="positive")
                        _refresh_watch_list()
                        _refresh_risen()
                        _refresh_fallen()
                        _refresh_soldout()

                    ui.button(
                        "전체 기준가 리셋", icon="restart_alt", on_click=_on_reset_all_base
                    ).props("color=orange dense outline").tooltip("오탐 일괄 정리 — 현재가를 새 기준가로 설정")

        # ── 전체 확인 버튼 ────────────────────────────────────────
        counts_now  = _pc.alert_counts()
        _risen_init      = counts_now["risen"]
        _fallen_init     = counts_now["fallen"]
        _sold_init       = counts_now["soldout"]
        _restocked_init  = counts_now["restocked"]

        if _risen_init + _fallen_init + _sold_init > 0:
            with ui.row().classes("items-center gap-3 mb-3"):
                ui.icon("warning_amber", color="orange", size="sm")
                _banner_parts = []
                if _risen_init:  _banner_parts.append(f"가격상승 {_risen_init}건")
                if _fallen_init: _banner_parts.append(f"가격하락 {_fallen_init}건")
                if _sold_init:   _banner_parts.append(f"품절 {_sold_init}건")
                ui.label(
                    f"미확인 알림: {'  '.join(_banner_parts)}"
                ).classes("text-sm font-bold text-orange-600")
                async def _ack_all():
                    """모든 알림 일괄 확인 완료."""
                    watches = _pc.all_watches()
                    for w in watches:
                        if w.status in ("risen", "fallen", "soldout"):
                            _pc.reset_base_price(w.uid)
                    ui.notify("✅ 모든 알림 확인 완료 — 깜빡임 해제", type="positive")
                    _refresh_risen()
                    _refresh_fallen()
                    _refresh_soldout()
                    for _t, _ico, _key in [
                        (tab_risen, "📈 가격 상승", "risen"),
                        (tab_fallen, "📉 가격 하락", "fallen"),
                        (tab_sold, "❌ 품절", "soldout"),
                    ]:
                        _t._props['label'] = _ico
                        _t.update()
                ui.button(
                    "✅ 전체 확인 완료",
                    icon="done_all",
                    on_click=_ack_all,
                ).props("color=positive dense")

        # ── 탭: 감시 목록 / 가격 상승 / 품절 ─────────────────────
        _risen_lbl      = f"📈 가격 상승 ({_risen_init})"   if _risen_init      else "📈 가격 상승"
        _fallen_lbl     = f"📉 가격 하락 ({_fallen_init})"  if _fallen_init     else "📉 가격 하락"
        _sold_lbl       = f"❌ 품절 ({_sold_init})"         if _sold_init       else "❌ 품절"
        _restocked_lbl  = f"🟢 재입고 ({_restocked_init})"  if _restocked_init  else "🟢 재입고"
        with ui.tabs().classes("w-full") as tabs:
            tab_list      = ui.tab("📋 감시 목록")
            tab_risen     = ui.tab(_risen_lbl)
            tab_fallen    = ui.tab(_fallen_lbl)
            tab_sold      = ui.tab(_sold_lbl)
            tab_restocked = ui.tab(_restocked_lbl)

        with ui.tab_panels(tabs, value=tab_list).classes("w-full"):

            # ━━━ 탭 1: 감시 목록 (검색 + 페이지네이션) ━━━━━━━━━━━━━
            with ui.tab_panel(tab_list):

                # ── 검색 + 페이지 크기 설정 ──────────────────────────
                _wl_page:     dict = {"v": 0}           # 현재 페이지 (0-based)
                _wl_per_page: dict = {"v": 30}          # 페이지당 항목 수

                with ui.row().classes("items-center gap-2 w-full flex-wrap mb-2"):
                    _wl_search = ui.input(
                        placeholder="🔍 상품명 또는 URL 검색..."
                    ).props("dense outlined clearable").style("flex:1; min-width:200px")

                    ui.label("페이지당").classes("text-xs text-slate-500 shrink-0")
                    _wl_per_sel = ui.select(
                        [10, 30, 50, 100, 500],
                        value=30,
                    ).props("dense outlined").style("width:80px")
                    ui.label("개").classes("text-xs text-slate-500 shrink-0")

                watch_container = ui.column().classes("w-full gap-2")
                _wl_pager_row  = ui.row().classes("items-center gap-2 mt-2 flex-wrap")

                def _refresh_watch_list():
                    watch_container.clear()
                    _wl_pager_row.clear()

                    _q = (_wl_search.value or "").strip().lower()
                    _all = _pc.all_watches()
                    # 검색 필터
                    if _q:
                        _all = [w for w in _all
                                if _q in (w.name or "").lower()
                                or _q in w.url.lower()]

                    total     = len(_all)
                    per_page  = int(_wl_per_page["v"])
                    cur_page  = int(_wl_page["v"])
                    total_pages = max(1, (total + per_page - 1) // per_page)
                    cur_page  = max(0, min(cur_page, total_pages - 1))
                    _wl_page["v"] = cur_page

                    page_items = _all[cur_page * per_page : (cur_page + 1) * per_page]

                    if not _all:
                        with watch_container:
                            msg = "검색 결과 없음" if _q else "감시 중인 URL이 없습니다."
                            ui.label(msg).classes(
                                "text-slate-400 text-sm text-center py-6"
                            )
                        return

                    with watch_container:
                        # 요약 바
                        with ui.row().classes("items-center gap-2 mb-1"):
                            _qmsg = f" ('{_q}' 검색)" if _q else ""
                            ui.label(
                                f"총 {total}개{_qmsg}  |  {cur_page+1}/{total_pages} 페이지  "
                                f"({cur_page*per_page+1}~{min((cur_page+1)*per_page, total)}번째)"
                            ).classes("text-xs text-slate-500")

                        for w in page_items:
                            status_color = {
                                "ok":      "positive",
                                "risen":   "orange",
                                "fallen":  "blue",
                                "soldout": "negative",
                            }.get(w.status, "grey")
                            status_label = {
                                "ok":      "정상",
                                "risen":   f"▲ +{w.change:,}원",
                                "fallen":  f"▼ {w.change:,}원",
                                "soldout": "품절",
                            }.get(w.status, "대기")

                            with ui.card().classes("w-full shadow-sm"):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-2 w-full flex-wrap"):
                                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                                            with ui.row().classes("items-center gap-1"):
                                                _store_val = getattr(w, "store", "샵케이") or "샵케이"
                                                _store_color = "blue" if _store_val == "샵케이" else "orange"
                                                ui.badge(_store_val).props(f"color={_store_color}").style("font-size:10px")
                                                ui.label(w.name or w.url[:60]).classes(
                                                    "text-sm font-semibold text-slate-700 truncate"
                                                )
                                            ui.link(w.url[:70], w.url, new_tab=True).classes(
                                                "text-xs text-blue-400 font-mono truncate hover:underline"
                                            )
                                            chk = w.last_checked or "미체크"
                                            ui.label(
                                                f"기준가: {w.base_price:,}원  "
                                                f"현재: {w.current_price:,}원  "
                                                f"마지막 체크: {chk[:16]}"
                                            ).classes("text-xs text-slate-500 mt-1")

                                        ui.badge(status_label).props(f"color={status_color}")

                                        def _make_del(uid=w.uid):
                                            def _h():
                                                _pc.remove_watch(uid)
                                                _refresh_watch_list()
                                                ui.notify("삭제 완료", type="info", timeout=1500)
                                            return _h
                                        ui.button(
                                            icon="delete_outline",
                                            on_click=_make_del(),
                                        ).props("flat dense size=xs color=red").tooltip("감시 삭제")

                    # ── 페이지 네비게이션 ──────────────────────────
                    with _wl_pager_row:
                        def _go_page(p: int):
                            _wl_page["v"] = p
                            _refresh_watch_list()

                        ui.button(icon="first_page", on_click=lambda: _go_page(0)).props(
                            "flat dense size=sm"
                        ).set_enabled(cur_page > 0)
                        ui.button(icon="chevron_left",
                                  on_click=lambda: _go_page(max(0, cur_page - 1))).props(
                            "flat dense size=sm"
                        ).set_enabled(cur_page > 0)

                        # 페이지 번호 버튼 (최대 7개 표시)
                        _start_p = max(0, min(cur_page - 3, total_pages - 7))
                        _end_p   = min(total_pages, _start_p + 7)
                        for _p in range(_start_p, _end_p):
                            _pp = _p
                            _btn = ui.button(
                                str(_pp + 1),
                                on_click=lambda p=_pp: _go_page(p),
                            ).props(
                                f"{'color=blue' if _pp == cur_page else 'flat'} dense size=sm"
                            )

                        ui.button(icon="chevron_right",
                                  on_click=lambda: _go_page(min(total_pages - 1, cur_page + 1))).props(
                            "flat dense size=sm"
                        ).set_enabled(cur_page < total_pages - 1)
                        ui.button(icon="last_page",
                                  on_click=lambda: _go_page(total_pages - 1)).props(
                            "flat dense size=sm"
                        ).set_enabled(cur_page < total_pages - 1)

                        ui.label(f"/ {total_pages}p").classes("text-xs text-slate-500 ml-1")

                # 검색어 변경 → 1페이지로 이동 후 새로고침
                def _on_search_change(e=None):
                    _wl_page["v"] = 0
                    _refresh_watch_list()
                _wl_search.on_value_change(_on_search_change)

                # 페이지당 개수 변경 → 1페이지로 이동 후 새로고침
                def _on_per_page_change(e=None):
                    _wl_per_page["v"] = int(_wl_per_sel.value or 30)
                    _wl_page["v"] = 0
                    _refresh_watch_list()
                _wl_per_sel.on_value_change(_on_per_page_change)

                _refresh_watch_list()

            # ━━━ 탭 2: 가격 상승 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            with ui.tab_panel(tab_risen):
                risen_container = ui.column().classes("w-full gap-2 mt-2")

                def _refresh_risen():
                    risen_container.clear()
                    watches = [w for w in _pc.all_watches() if w.status == "risen"]
                    if not watches:
                        with risen_container:
                            ui.label("가격 상승 알림 없음 ✅").classes(
                                "text-green-600 text-sm text-center py-6"
                            )
                        return
                    with risen_container:
                        ui.label(
                            f"⚠️  총 {len(watches)}개 상품 가격 상승 감지"
                        ).classes("text-sm font-bold text-orange-600 mb-1")
                        for w in watches:
                            pct = ((w.current_price - w.base_price) / w.base_price * 100) if w.base_price else 0
                            with ui.card().classes(
                                "w-full shadow-sm border-l-4 border-orange-400"
                            ):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                                            ui.label(w.name or w.url[:60]).classes(
                                                "text-sm font-semibold text-slate-700 truncate"
                                            )
                                            ui.link(w.url[:60], w.url, new_tab=True).classes(
                                                "text-xs text-blue-500 font-mono"
                                            )
                                        with ui.column().classes("items-end gap-0 shrink-0"):
                                            ui.label(
                                                f"등록 시: {w.base_price:,}원"
                                            ).classes("text-xs text-slate-500")
                                            ui.label(
                                                f"현재: {w.current_price:,}원"
                                            ).classes("text-sm font-bold text-orange-600")
                                            ui.label(
                                                f"+{w.change:,}원  ({pct:+.1f}%)"
                                            ).classes("text-xs font-bold text-orange-500")

                                        def _make_ack(uid=w.uid):
                                            def _h():
                                                _pc.reset_base_price(uid)
                                                ui.notify("✅ 확인 완료 — 기준가 업데이트", type="positive", timeout=2000)
                                                _refresh_risen()
                                                _cnt = len([x for x in _pc.all_watches() if x.status == "risen"])
                                                tab_risen._props['label'] = f"📈 가격 상승 ({_cnt})" if _cnt else "📈 가격 상승"
                                                tab_risen.update()
                                            return _h
                                        ui.button(
                                            "✅ 확인 완료",
                                            on_click=_make_ack(),
                                        ).props("color=teal dense size=sm").tooltip(
                                            "현재가를 새 기준가로 설정 — 다음 체크부터 이 가격 기준"
                                        )

                _refresh_risen()

            # ━━━ 탭 3: 가격 하락 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            with ui.tab_panel(tab_fallen):
                fallen_container = ui.column().classes("w-full gap-2 mt-2")

                def _refresh_fallen():
                    fallen_container.clear()
                    watches = [w for w in _pc.all_watches() if w.status == "fallen"]
                    if not watches:
                        with fallen_container:
                            ui.label("가격 하락 알림 없음 ✅").classes(
                                "text-green-600 text-sm text-center py-6"
                            )
                        return
                    with fallen_container:
                        ui.label(
                            f"📉  총 {len(watches)}개 상품 가격 하락 감지"
                        ).classes("text-sm font-bold text-blue-600 mb-1")
                        for w in watches:
                            pct = ((w.current_price - w.base_price) / w.base_price * 100) if w.base_price else 0
                            with ui.card().classes(
                                "w-full shadow-sm border-l-4 border-blue-400"
                            ):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                                            ui.label(w.name or w.url[:60]).classes(
                                                "text-sm font-semibold text-slate-700 truncate"
                                            )
                                            ui.link(w.url[:60], w.url, new_tab=True).classes(
                                                "text-xs text-blue-500 font-mono"
                                            )
                                        with ui.column().classes("items-end gap-0 shrink-0"):
                                            ui.label(
                                                f"기준가: {w.base_price:,}원"
                                            ).classes("text-xs text-slate-500")
                                            ui.label(
                                                f"현재: {w.current_price:,}원"
                                            ).classes("text-sm font-bold text-blue-600")
                                            ui.label(
                                                f"{w.change:,}원  ({pct:+.1f}%)"
                                            ).classes("text-xs font-bold text-blue-500")

                                        def _make_fallen_ack(uid=w.uid):
                                            def _h():
                                                _pc.reset_base_price(uid)
                                                ui.notify("✅ 확인 완료 — 기준가 업데이트", type="positive", timeout=2000)
                                                _refresh_fallen()
                                                _cnt = len([x for x in _pc.all_watches() if x.status == "fallen"])
                                                tab_fallen._props['label'] = f"📉 가격 하락 ({_cnt})" if _cnt else "📉 가격 하락"
                                                tab_fallen.update()
                                            return _h
                                        ui.button(
                                            "✅ 확인 완료",
                                            on_click=_make_fallen_ack(),
                                        ).props("color=teal dense size=sm").tooltip(
                                            "현재가를 새 기준가로 설정 — 다음 체크부터 이 가격 기준"
                                        )

                _refresh_fallen()

            # ━━━ 탭 4: 품절 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            with ui.tab_panel(tab_sold):
                sold_container = ui.column().classes("w-full gap-2 mt-2")

                def _refresh_soldout():
                    sold_container.clear()
                    watches = [w for w in _pc.all_watches() if w.status == "soldout"]
                    if not watches:
                        with sold_container:
                            ui.label("품절 알림 없음 ✅").classes(
                                "text-green-600 text-sm text-center py-6"
                            )
                        return
                    with sold_container:
                        ui.label(
                            f"🚫  총 {len(watches)}개 상품 품절 감지"
                        ).classes("text-sm font-bold text-red-600 mb-1")
                        for w in watches:
                            with ui.card().classes(
                                "w-full shadow-sm border-l-4 border-red-400"
                            ):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                                            ui.label(w.name or w.url[:60]).classes(
                                                "text-sm font-semibold text-slate-700 truncate"
                                            )
                                            ui.link(w.url[:60], w.url, new_tab=True).classes(
                                                "text-xs text-blue-500 font-mono"
                                            )
                                            ui.label(
                                                f"기준가: {w.base_price:,}원  마지막 체크: {(w.last_checked or '')[:16]}"
                                            ).classes("text-xs text-slate-500 mt-1")

                                        def _make_sold_ack(uid=w.uid):
                                            def _h():
                                                _pc.reset_base_price(uid)
                                                ui.notify("✅ 확인 완료", type="positive", timeout=2000)
                                                _refresh_soldout()
                                                _cnt = len([x for x in _pc.all_watches() if x.status == "soldout"])
                                                tab_sold._props['label'] = f"❌ 품절 ({_cnt})" if _cnt else "❌ 품절"
                                                tab_sold.update()
                                            return _h
                                        ui.button(
                                            "✅ 확인 완료",
                                            on_click=_make_sold_ack(),
                                        ).props("color=teal dense size=sm")

                _refresh_soldout()

            # ━━━ 탭 5: 재입고 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            with ui.tab_panel(tab_restocked):
                restocked_container = ui.column().classes("w-full gap-2 mt-2")

                def _refresh_restocked():
                    restocked_container.clear()
                    watches = [w for w in _pc.all_watches() if w.status == "restocked"]
                    if not watches:
                        with restocked_container:
                            ui.label("재입고 알림 없음 ✅").classes(
                                "text-green-600 text-sm text-center py-6"
                            )
                        return
                    with restocked_container:
                        ui.label(
                            f"🟢  총 {len(watches)}개 상품 재입고 감지"
                        ).classes("text-sm font-bold text-green-700 mb-1")
                        for w in watches:
                            with ui.card().classes(
                                "w-full shadow-sm border-l-4 border-green-400"
                            ):
                                with ui.card_section().classes("py-2"):
                                    with ui.row().classes("items-center gap-3 w-full flex-wrap"):
                                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                                            ui.label(w.name or w.url[:60]).classes(
                                                "text-sm font-semibold text-slate-700 truncate"
                                            )
                                            ui.link(w.url[:60], w.url, new_tab=True).classes(
                                                "text-xs text-blue-500 font-mono"
                                            )
                                            ui.label(
                                                f"현재가: {w.current_price:,}원  마지막 체크: {(w.last_checked or '')[:16]}"
                                            ).classes("text-xs text-slate-500 mt-1")

                                        def _make_restock_ack(uid=w.uid):
                                            def _h():
                                                _pc.reset_base_price(uid)
                                                ui.notify("✅ 확인 완료", type="positive", timeout=2000)
                                                _refresh_restocked()
                                            return _h
                                        ui.button(
                                            "✅ 확인 완료",
                                            on_click=_make_restock_ack(),
                                        ).props("color=green dense size=sm")

                _refresh_restocked()


# ── 상세페이지 생성 탭 빌더 ────────────────────────────────────────

def _build_detail_page_tab(settings) -> None:
    """
    '🖼 상세페이지 생성' 탭 UI 빌더 (v3).

    입력 방식:
      ① 파일 업로드 (multiple)
      ② Ctrl+V 붙여넣기 (JS paste → /api/dp/paste_upload → 큐 폴링)
      ③ URL 붙여넣기 (네이버 CDN 등 직접 다운로드)

    자동 처리:
      - 세로 긴 이미지 감지(height/width > 2.5) → 자동 분할 미리보기 제공
      - 조각별 포함/제외 선택 후 [분할 적용]
      - Gemini API에는 일반 크기 이미지만 전달 (긴 이미지 그대로 전달 금지)

    기존 코드(크롤링·다중등록·공유 함수)는 일절 수정하지 않음.
    """
    import asyncio   as _asyncio
    import functools as _functools
    import tempfile  as _tempfile
    import urllib.request as _urlreq
    from pathlib import Path as _Path

    _MAX_IMAGES  = 5
    _IMG_DIR     = _Path(__file__).parent / "data" / "images"

    # ── 상태 ─────────────────────────────────────────────────────
    _dp_state: dict = {
        "running":           False,
        "image_paths":       [],    # 전체 이미지 경로 (표시용)
        "product_ref_paths": [],    # 제품 사진으로 분류된 경로만 (AI 레퍼런스용)
        "result_path":       "",
        "result_url":        "",
    }
    # 분할 미리보기 상태
    _split_state: dict = {
        "pieces":      [],    # list[PIL.Image]
        "included":    [],    # list[bool]  (조각별 포함 여부)
        "chunk_types": [],    # list[str]   "product" | "text_graphic" | "unknown"
        "orig_path":   "",    # 분할 원본 경로
        "saved_paths": [],    # 저장된 조각 경로
    }

    # ══════════════════════════════════════════════════════════════
    # § 1 — 상품 정보
    # ══════════════════════════════════════════════════════════════
    with ui.card_section():
        ui.label("① 상품 정보").classes("font-bold text-slate-700 text-sm mb-2")
        dp_name_input = ui.input(
            label="상품명 *",
            placeholder="예: 메팔 후르츠팟 런치 3종 세트 도시락통",
        ).classes("w-full")
        dp_specs_input = ui.textarea(
            label="스펙/속성 (선택) — 키: 값 형식, 줄바꿈 구분",
            placeholder="브랜드: MEPAL\n용량: 600ml\n재질: 폴리프로필렌\n구성: 도시락통 1개, 간식통 2개",
        ).classes("w-full").style("height:80px; font-size:12px;")

    # ══════════════════════════════════════════════════════════════
    # § 2 — 제품 이미지 입력 (파일 / 붙여넣기 / URL)
    # ══════════════════════════════════════════════════════════════
    with ui.card_section():
        with ui.row().classes("items-center justify-between mb-1"):
            ui.label("② 제품 이미지").classes("font-bold text-slate-700 text-sm")
            dp_img_count_lbl = ui.label("0 / 5").classes("text-xs text-slate-400 font-mono")

        ui.label(
            "전체컷·개별컷 모두 올리세요 — "
            "히어로/클로즈업은 Gemini 레퍼런스, 특징/스펙 섹션은 실사진 그대로 합성."
        ).classes("text-xs text-slate-400 mb-2")

        # 썸네일 갤러리
        dp_thumb_row  = ui.row().classes("gap-1 flex-wrap mb-2 min-h-8")
        dp_img_status = ui.label("이미지 없음 (AI가 임의 생성)").classes(
            "text-xs text-slate-400 w-full"
        )

        # ── 입력 방법 1: 파일 업로드 ─────────────────────────────
        ui.upload(
            label="파일 선택 (jpg/png/webp, 여러 장 가능)",
            auto_upload=True,
            max_file_size=20_000_000,
            multiple=True,
            on_upload=lambda e: _handle_file_upload(e),
        ).props("accept=image/* flat dense color=teal").classes("w-full text-xs mt-1")

        # ── 입력 방법 2: Ctrl+V 붙여넣기 안내 ────────────────────
        with ui.row().classes(
            "items-center gap-2 mt-2 px-3 py-2 rounded border border-dashed"
            " border-slate-300 bg-slate-50 cursor-pointer w-full"
        ).on("click", lambda: None):   # 클릭으로 포커스 유도
            ui.icon("content_paste").classes("text-slate-400 text-sm")
            ui.label(
                "이 영역 클릭 후 Ctrl+V — 클립보드 이미지가 자동으로 추가됩니다"
            ).classes("text-xs text-slate-500")
        dp_paste_status = ui.label("").classes("text-xs text-teal-600 mt-1")

        # ── 입력 방법 3: URL 붙여넣기 ────────────────────────────
        with ui.expansion("URL로 이미지 추가 (펼치기)", icon="link").classes(
            "w-full text-xs mt-1"
        ):
            dp_url_input = ui.textarea(
                label="이미지 URL (한 줄에 1개)",
                placeholder=(
                    "https://shop-phinf.pstatic.net/...\n"
                    "https://..."
                ),
            ).classes("w-full").style("height:70px; font-size:11px;")
            dp_url_status = ui.label("").classes("text-xs text-slate-400 mt-1")

            async def _on_url_download():
                urls_raw = (dp_url_input.value or "").strip()
                if not urls_raw:
                    ui.notify("URL을 입력하세요.", type="warning", timeout=2000)
                    return
                urls = [u.strip() for u in urls_raw.splitlines() if u.strip().startswith("http")]
                if not urls:
                    ui.notify("유효한 URL이 없습니다.", type="warning", timeout=2000)
                    return
                dp_url_status.set_text(f"{len(urls)}개 URL 다운로드 중...")
                loop = _asyncio.get_running_loop()
                added = await loop.run_in_executor(None, lambda: _download_urls(urls))
                dp_url_status.set_text(
                    f"{added}장 추가됨" if added else "다운로드 실패 — URL/네트워크 확인"
                )
                _refresh_thumbs()

            ui.button("다운로드", icon="cloud_download", on_click=_on_url_download).props(
                "color=teal dense size=sm"
            ).classes("mt-1")

        # 전체 삭제
        ui.button(
            "전체 삭제", icon="delete_outline",
        ).props("flat dense size=xs color=red").classes("mt-2").on_click(lambda: _clear_images())

    # ── 분할 미리보기 섹션 (긴 이미지 감지 시 표시) ───────────────────
    dp_split_section = ui.card_section()
    dp_split_section.set_visibility(False)
    with dp_split_section:
        with ui.row().classes("items-center justify-between mb-1"):
            ui.label("📐 긴 이미지 자동 분할").classes("font-bold text-orange-600 text-sm")
            dp_split_count_lbl = ui.label("").classes("text-xs text-slate-400")
        ui.label(
            "세로로 긴 이미지가 감지됐습니다. 조각 수 = 생성될 섹션 수. 사용할 것을 선택하고 [분할 적용]을 누르세요."
        ).classes("text-xs text-slate-500 mb-2")
        dp_split_thumb_row = ui.row().classes("gap-2 flex-wrap mb-2")
        dp_read_status = ui.label("").classes("text-xs text-teal-600 w-full mt-1")
        with ui.row().classes("gap-2 flex-wrap"):
            ui.button(
                "분할 적용", icon="check_circle",
            ).props("color=orange size=sm").on_click(lambda: _apply_split())
            dp_read_btn = ui.button(
                "🔍 텍스트 자동 읽기", icon="auto_awesome",
            ).props("color=teal size=sm outline")
            ui.button(
                "전부 포함", icon="select_all",
            ).props("flat dense size=sm color=grey").on_click(lambda: _select_all_splits(True))
            ui.button(
                "전부 제외", icon="deselect",
            ).props("flat dense size=sm color=grey").on_click(lambda: _select_all_splits(False))

    # ══════════════════════════════════════════════════════════════
    # § 3 — 생성 옵션
    # ══════════════════════════════════════════════════════════════
    with ui.card_section():
        ui.label("③ 생성 옵션").classes("font-bold text-slate-700 text-sm mb-2")
        with ui.row().classes("items-center gap-3 flex-wrap"):
            with ui.row().classes("items-center gap-1"):
                ui.label("섹션 수").classes("text-xs text-slate-600")
                dp_section_count = ui.number(
                    value=4, min=1, step=1,
                ).props("dense outlined").style("width:60px; font-size:12px;")
        dp_cost_label = ui.label("4섹션 예정 | Gemini API 4회 호출").classes(
            "text-xs text-slate-400 mt-1"
        )
        with ui.row().classes("items-center gap-2 mt-2"):
            dp_composite = ui.switch(
                "실사진 합성 ON (특징·스펙 섹션 실제 사진 배치)",
                value=True,
            ).props("color=deep-orange dense")
        ui.label(
            "ON: 특징/스펙/사용법 섹션은 AI 배경 + 실사진 PIL 합성 (제품 왜곡 없음)\n"
            "OFF: 모든 섹션 Gemini 전체 생성"
        ).classes("text-xs text-slate-400 whitespace-pre-line ml-1 mb-1")

    # ══════════════════════════════════════════════════════════════
    # § 4 — 실행 / 진행 / 결과
    # ══════════════════════════════════════════════════════════════
    with ui.card_section():
        dp_progress_label = ui.label("").classes("text-xs text-slate-500 mb-1")
        dp_progress_bar   = ui.linear_progress(value=0).props("color=teal").style("height:5px")
        dp_progress_bar.set_visibility(False)
        with ui.row().classes("gap-2 mt-2 flex-wrap"):
            dp_gen_btn = ui.button(
                "🎨 생성", icon="auto_awesome",
            ).props("color=teal size=md").classes("font-bold")
            dp_upload_btn = ui.button(
                "☁️ R2 업로드", icon="cloud_upload",
            ).props("color=indigo size=md outline").classes("font-bold")
            dp_upload_btn.set_visibility(False)

    with ui.card_section():
        dp_preview_label    = ui.label("").classes("text-xs text-slate-500 mb-1")
        dp_preview_img      = ui.image("").style(
            "width:100%; max-height:600px; object-fit:contain; display:none;"
        )
        dp_result_url_label = ui.label("").classes(
            "text-xs text-teal-600 font-mono break-all mt-1"
        )

    # ══════════════════════════════════════════════════════════════
    # 내부 헬퍼 함수들
    # ══════════════════════════════════════════════════════════════

    # ── 이미지 추가 공통 진입점 ───────────────────────────────────
    def _add_image_path(path: str, name: str = "") -> bool:
        """이미지 경로를 상태에 추가. 최대 초과 시 False 반환."""
        if len(_dp_state["image_paths"]) >= _MAX_IMAGES:
            ui.notify(f"최대 {_MAX_IMAGES}장까지 추가 가능합니다.", type="warning", timeout=2000)
            return False
        _dp_state["image_paths"].append(path)
        _check_long_image(path)
        return True

    def _handle_file_upload(e):
        try:
            suffix = _Path(e.name).suffix.lower() or ".jpg"
            tmp    = _tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, dir=str(_IMG_DIR)
            )
            tmp.write(e.content.read())
            tmp.close()
            if _add_image_path(tmp.name, e.name):
                _refresh_thumbs()
                ui.notify(f"업로드: {e.name}", type="positive", timeout=1500)
        except Exception as ex:
            ui.notify(f"업로드 실패: {ex}", type="negative", timeout=3000)

    def _download_urls(urls: list[str]) -> int:
        """URL 리스트를 다운로드해 로컬 저장. 추가된 수 반환."""
        added = 0
        for url in urls:
            if len(_dp_state["image_paths"]) >= _MAX_IMAGES:
                break
            try:
                req  = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                resp = _urlreq.urlopen(req, timeout=10)
                data = resp.read()
                ct   = resp.headers.get("Content-Type", "image/jpeg")
                ext  = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg"
                tmp  = _tempfile.NamedTemporaryFile(
                    delete=False, suffix=ext, dir=str(_IMG_DIR)
                )
                tmp.write(data)
                tmp.close()
                _dp_state["image_paths"].append(tmp.name)
                _check_long_image(tmp.name)
                added += 1
            except Exception as e:
                print(f"[DP] URL 다운로드 실패 ({url[:60]}): {e}")
        return added

    def _clear_images():
        _dp_state["image_paths"].clear()
        _split_state["pieces"].clear()
        _split_state["included"].clear()
        _split_state["orig_path"] = ""
        dp_split_section.set_visibility(False)
        _refresh_thumbs()

    def _refresh_thumbs():
        paths = _dp_state["image_paths"]
        n     = len(paths)
        dp_img_count_lbl.set_text(f"{n} / {_MAX_IMAGES}")
        dp_thumb_row.clear()
        with dp_thumb_row:
            for idx, p in enumerate(paths):
                try:
                    rel = _Path(p).relative_to(_IMG_DIR)
                    with ui.column().classes("gap-0 items-center"):
                        ui.image(f"/img/{rel.as_posix()}").style(
                            "width:58px; height:58px; object-fit:cover;"
                            "border-radius:4px; border:1px solid #e2e8f0;"
                        )
                        # 개별 삭제 버튼
                        _make_del_btn(idx)
                except Exception:
                    pass
        dp_img_status.set_text(
            f"{n}장 준비됨" if n else "이미지 없음 (AI가 임의 생성)"
        )

    def _make_del_btn(idx: int):
        def _do_del(i=idx):
            if 0 <= i < len(_dp_state["image_paths"]):
                _dp_state["image_paths"].pop(i)
                _refresh_thumbs()
        ui.button(icon="close").props(
            "flat dense round size=xs color=red"
        ).style("font-size:9px; min-height:16px; min-width:16px;").on_click(_do_del)

    # ── Ctrl+V 폴링 타이머 (0.5초마다 큐 확인) ───────────────────
    def _drain_paste_queue():
        while _DP_PASTE_QUEUE:
            path = _DP_PASTE_QUEUE.pop(0)
            if len(_dp_state["image_paths"]) < _MAX_IMAGES:
                _dp_state["image_paths"].append(path)
                _check_long_image(path)
                dp_paste_status.set_text(f"붙여넣기 완료: {_Path(path).name}")
                _refresh_thumbs()
            else:
                dp_paste_status.set_text(f"최대 {_MAX_IMAGES}장 초과 — 붙여넣기 무시됨")

    ui.timer(0.5, _drain_paste_queue)

    # JS paste 이벤트 리스너 등록 (페이지당 한 번만)
    ui.add_body_html("""
<script>
(function(){
  if(window._dp_paste_registered) return;
  window._dp_paste_registered = true;
  document.addEventListener('paste', function(e){
    var cd = e.clipboardData || window.clipboardData;
    if(!cd) return;
    var items = cd.items || [];

    // ① binary image blobs — 여러 장 모두 업로드
    var blobs = [];
    for(var i=0; i<items.length; i++){
      if(items[i].type && items[i].type.startsWith('image/')){
        var b = items[i].getAsFile();
        if(b) blobs.push(b);
      }
    }
    if(blobs.length > 0){
      e.preventDefault();
      blobs.forEach(function(blob, idx){
        var fd = new FormData();
        fd.append('file', blob, 'paste_'+Date.now()+'_'+idx+'.png');
        fetch('/api/dp/paste_upload',{method:'POST',body:fd})
          .then(function(r){return r.json();})
          .then(function(d){if(d&&d.ok) console.log('[DP] paste saved:',d.path);})
          .catch(function(err){console.warn('[DP] paste blob err:',err);});
      });
      return;
    }

    // ② HTML paste — 네이버 JS비활성 후 여러 이미지 선택 복사 시
    for(var i=0; i<items.length; i++){
      if(items[i].type === 'text/html'){
        e.preventDefault();
        items[i].getAsString(function(html){
          var doc = new DOMParser().parseFromString(html,'text/html');
          var imgs = Array.from(doc.querySelectorAll('img'));
          if(imgs.length === 0){ console.log('[DP] HTML paste: img 없음'); return; }
          console.log('[DP] HTML paste: '+imgs.length+'장 감지');
          imgs.forEach(function(img){
            var src = img.src || img.getAttribute('src') || '';
            if(!src.startsWith('http')) return;
            fetch('/api/dp/url_upload',{
              method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({url: src})
            }).then(function(r){return r.json();})
              .then(function(d){if(d&&d.ok) console.log('[DP] url saved:',d.path);
                                else console.warn('[DP] url upload fail:',d.error);})
              .catch(function(err){console.warn('[DP] url upload err:',err);});
          });
        });
        break;
      }
    }
  });
})();
</script>
""")

    # ── 긴 이미지 감지 + 분할 미리보기 ──────────────────────────
    def _check_long_image(path: str):
        """추가된 이미지가 긴 이미지인지 확인. 맞으면 분할 미리보기 섹션 표시."""
        try:
            from PIL import Image as _PILImg
            from detail_page import is_long_image, split_long_image
            img = _PILImg.open(path)
            if not is_long_image(img):
                return
            pieces = split_long_image(path)
            if len(pieces) <= 1:
                return
            _split_state["pieces"]      = pieces
            _split_state["included"]    = [True] * len(pieces)
            _split_state["chunk_types"] = ["unknown"] * len(pieces)
            _split_state["orig_path"]   = path
            _split_state["saved_paths"] = []
            _render_split_preview()
            dp_split_section.set_visibility(True)
            ui.notify(
                f"긴 이미지 감지 — {len(pieces)}개 조각으로 분할 미리보기 준비됨. "
                "[🔍 텍스트 자동 읽기]를 눌러 분류 + 텍스트 추출을 하세요.",
                type="info", timeout=4000,
            )
        except Exception as ex:
            print(f"[DP] 긴 이미지 감지 오류: {ex}")

    def _render_split_preview():
        pieces      = _split_state["pieces"]
        included    = _split_state["included"]
        chunk_types = _split_state.get("chunk_types", ["unknown"] * len(pieces))
        # 길이 보정
        while len(chunk_types) < len(pieces):
            chunk_types.append("unknown")
        _split_state["chunk_types"] = chunk_types

        n_product = sum(1 for t in chunk_types if t == "product")
        n_text    = sum(1 for t in chunk_types if t == "text_graphic")
        n_unk     = sum(1 for t in chunk_types if t == "unknown")
        status    = f"{sum(included)}/{len(pieces)}개 선택"
        if n_unk == 0:
            status += f" | 🛒제품 {n_product}개 / 📝텍스트 {n_text}개"
        dp_split_count_lbl.set_text(status)

        dp_split_thumb_row.clear()
        with dp_split_thumb_row:
            for idx, (piece, inc, ctype) in enumerate(zip(pieces, included, chunk_types)):
                tmp = _tempfile.NamedTemporaryFile(
                    delete=False, suffix="_split_thumb.jpg", dir=str(_IMG_DIR)
                )
                thumb = piece.copy()
                thumb.thumbnail((80, 160))
                thumb.save(tmp.name, "JPEG", quality=80)
                tmp.close()
                rel = _Path(tmp.name).relative_to(_IMG_DIR)
                _make_split_thumb(idx, rel, inc, ctype, piece.size)

    def _make_split_thumb(idx, rel, inc, ctype, size):
        # 테두리 색: 제품=초록, 텍스트=파랑, 미분류=회색
        if not inc:
            border, badge_txt, badge_cls = "#e2e8f0", "✗ 제외", "text-slate-400"
        elif ctype == "product":
            border, badge_txt, badge_cls = "#10b981", "🛒 제품", "text-emerald-600"
        elif ctype == "text_graphic":
            border, badge_txt, badge_cls = "#3b82f6", "📝 텍스트", "text-blue-500"
        else:
            border, badge_txt, badge_cls = "#94a3b8", "❓ 미분류", "text-slate-400"

        opacity = "1" if inc else "0.4"
        with ui.column().classes("gap-0 items-center"):
            ui.image(f"/img/{rel.as_posix()}").style(
                f"width:70px; object-fit:contain; border:2px solid {border};"
                f"border-radius:4px; opacity:{opacity}; cursor:pointer;"
            ).on("click", lambda i=idx: _toggle_split(i))
            ui.label(f"{size[0]}×{size[1]}").classes("text-[9px] text-slate-400 mt-0")
            # 포함 토글 (클릭: 포함/제외)
            ui.label(badge_txt).classes(f"text-[9px] font-bold {badge_cls} cursor-pointer").on(
                "click", lambda i=idx: _toggle_split(i)
            )
            # 타입 토글 버튼 (제품 ↔ 텍스트)
            if inc and ctype != "unknown":
                swap_lbl = "📝→" if ctype == "product" else "🛒→"
                ui.label(swap_lbl).classes(
                    "text-[9px] text-slate-400 cursor-pointer underline"
                ).on("click", lambda i=idx: _toggle_chunk_type(i))

    def _toggle_split(idx: int):
        _split_state["included"][idx] = not _split_state["included"][idx]
        _render_split_preview()

    def _toggle_chunk_type(idx: int):
        types = _split_state["chunk_types"]
        if idx < len(types):
            types[idx] = "text_graphic" if types[idx] == "product" else "product"
        _render_split_preview()

    def _select_all_splits(include: bool):
        _split_state["included"] = [include] * len(_split_state["included"])
        _render_split_preview()

    def _apply_split():
        pieces      = _split_state["pieces"]
        included    = _split_state["included"]
        chunk_types = _split_state.get("chunk_types", ["unknown"] * len(pieces))
        orig        = _split_state["orig_path"]

        sel_idx  = [i for i, inc in enumerate(included) if inc]
        selected = [pieces[i]      for i in sel_idx]
        sel_type = [chunk_types[i] for i in sel_idx]

        if not selected:
            ui.notify("포함할 조각을 하나 이상 선택하세요.", type="warning", timeout=2000)
            return

        # 원본 제거 후 선택 조각 저장
        paths = _dp_state["image_paths"]
        if orig in paths:
            paths.remove(orig)

        saved_paths    = []
        product_paths  = []   # 제품 사진만
        text_paths     = []   # 텍스트·그래픽만

        for piece, ctype in zip(selected, sel_type):
            tmp = _tempfile.NamedTemporaryFile(
                delete=False, suffix=".jpg", dir=str(_IMG_DIR)
            )
            piece.save(tmp.name, "JPEG", quality=92)
            tmp.close()
            saved_paths.append(tmp.name)
            paths.append(tmp.name)
            if ctype == "text_graphic":
                text_paths.append(tmp.name)
            else:
                # "product" 또는 "unknown"은 제품 레퍼런스로 처리
                product_paths.append(tmp.name)

        # 제품 레퍼런스 풀 저장
        _dp_state["product_ref_paths"] = product_paths

        # 경고: 제품 사진이 하나도 없으면
        if not product_paths:
            ui.notify(
                "⚠️ 제품 사진으로 분류된 조각이 없습니다! "
                "미리보기에서 🛒→ 버튼으로 제품 조각을 수동 지정하거나, "
                "분리된 제품 사진을 직접 업로드하세요.",
                type="warning", timeout=6000,
            )
        else:
            n_prod = len(product_paths)
            n_text = len(text_paths)
            ui.notify(
                f"{len(selected)}개 조각 적용 — 🛒제품 레퍼런스 {n_prod}개 / 📝텍스트 {n_text}개",
                type="positive", timeout=3000,
            )

        # 섹션 수 자동 설정, 상태 정리
        dp_section_count.set_value(len(selected))
        _update_cost_label()
        dp_split_section.set_visibility(False)
        _split_state["pieces"].clear()
        _split_state["saved_paths"]   = saved_paths
        _split_state["product_paths"] = product_paths
        _split_state["text_paths"]    = text_paths
        _refresh_thumbs()

    def _update_cost_label():
        n = int(dp_section_count.value or 4)
        dp_cost_label.set_text(f"{n}섹션 예정 | Gemini API {n}회 호출")

    async def _read_texts_from_split():
        """
        분할 미리보기 중인 조각(또는 적용된 조각)에 대해:
          1. 조각 타입 분류 (제품/텍스트·그래픽)
          2. 텍스트·그래픽 조각에서 상품명·스펙 추출 → 필드 자동 채움
          3. 분류 결과를 미리보기에 즉시 반영
        """
        # 분할 미리보기 중이면 pieces 기준, 아니면 현재 image_paths
        in_preview   = bool(_split_state.get("pieces"))
        if in_preview:
            # 썸네일 임시 파일로 분류하면 화질 손실 → 원본 조각을 temp 저장 후 분류
            pieces   = _split_state["pieces"]
            tmp_paths = []
            for piece in pieces:
                tmp = _tempfile.NamedTemporaryFile(
                    delete=False, suffix="_cls.jpg", dir=str(_IMG_DIR)
                )
                piece.save(tmp.name, "JPEG", quality=90)
                tmp.close()
                tmp_paths.append(tmp.name)
            classify_targets = tmp_paths
            text_targets     = tmp_paths    # 전체 조각 대상으로 텍스트 추출
        else:
            classify_targets = list(_dp_state["image_paths"])
            text_targets     = list(_dp_state["image_paths"])

        if not classify_targets:
            ui.notify("먼저 이미지를 분할하거나 추가하세요.", type="warning", timeout=2000)
            return

        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            ui.notify("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.", type="negative", timeout=3000)
            return

        dp_read_btn.props("disabled loading")
        dp_read_status.set_text("조각 분류 중...")
        loop = _asyncio.get_running_loop()
        try:
            from detail_page import classify_image_chunks, read_product_info_from_images

            # ── 1. 조각 분류 ──────────────────────────────────────
            types_result = await loop.run_in_executor(
                None,
                lambda: classify_image_chunks(classify_targets, api_key),
            )

            if in_preview and types_result:
                _split_state["chunk_types"] = list(types_result)
                _render_split_preview()  # 분류 배지 즉시 표시

            # 분류 결과 요약
            n_prod = types_result.count("product")
            n_text = types_result.count("text_graphic")
            dp_read_status.set_text(
                f"분류 완료: 🛒제품 {n_prod}개 / 📝텍스트 {n_text}개 | 텍스트 추출 중..."
            )

            # ── 2. 텍스트 추출 (전체 조각 대상) ───────────────────
            result = await loop.run_in_executor(
                None,
                lambda: read_product_info_from_images(text_targets, api_key),
            )
            name  = result.get("product_name", "")
            specs = result.get("specs", {})
            descs = result.get("descriptions", [])

            # 상품명 필드 채움 (비어 있을 때만)
            if name and not (dp_name_input.value or "").strip():
                dp_name_input.set_value(name)

            # 스펙 필드 채움
            if specs or descs:
                existing = (dp_specs_input.value or "").strip()
                lines = [f"{k}: {v}" for k, v in specs.items()]
                lines += [f"설명: {d}" for d in descs[:4]]
                new_text = "\n".join(lines)
                dp_specs_input.set_value((existing + "\n" + new_text).strip() if existing else new_text)

            summary = (
                f"완료 — 🛒제품 {n_prod}개 / 📝텍스트 {n_text}개 | "
                f"상품명: '{name}' | 스펙 {len(specs)}개"
            )
            if not name and not specs:
                summary += " (텍스트 없음)"
            dp_read_status.set_text(summary)
            ui.notify(summary, type="positive", timeout=4000)

        except Exception as ex:
            dp_read_status.set_text(f"오류: {ex}")
            ui.notify(f"분류/추출 오류: {ex}", type="negative", timeout=4000)
        finally:
            dp_read_btn.props(remove="disabled loading")

    dp_read_btn.on_click(_read_texts_from_split)

    dp_section_count.on_value_change(lambda _: _update_cost_label())
    _update_cost_label()

    # ── 생성 버튼 ─────────────────────────────────────────────────
    async def _on_generate():
        if _dp_state["running"]:
            ui.notify("이미 생성 중입니다.", type="warning", timeout=2000)
            return
        product_name = (dp_name_input.value or "").strip()
        if not product_name:
            ui.notify("상품명을 입력하세요.", type="warning", timeout=2000)
            return
        api_key = getattr(settings, "GEMINI_API_KEY", "")
        if not api_key:
            ui.notify("GEMINI_API_KEY 환경변수가 설정되지 않았습니다.", type="negative", timeout=4000)
            return

        specs: dict[str, str] = {}
        for line in (dp_specs_input.value or "").splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                k, v    = k.strip(), v.strip()
                if k and v:
                    specs[k] = v

        section_count = int(dp_section_count.value or 4)
        # 제품 레퍼런스 풀 우선 사용 (분류된 경우) — 없으면 전체 image_paths 폴백
        product_refs  = _dp_state.get("product_ref_paths") or []
        image_paths   = product_refs if product_refs else list(_dp_state["image_paths"])
        use_composite = dp_composite.value

        if not product_refs and _dp_state["image_paths"]:
            ui.notify(
                "제품 레퍼런스 분류 없이 생성합니다. "
                "[🔍 텍스트 자동 읽기]로 분류하면 품질이 향상됩니다.",
                type="info", timeout=3000,
            )

        _dp_state["running"]     = True
        _dp_state["result_path"] = ""
        _dp_state["result_url"]  = ""
        dp_upload_btn.set_visibility(False)
        dp_result_url_label.set_text("")
        dp_preview_img.style("display:none")
        dp_preview_label.set_text("")
        dp_progress_bar.set_visibility(True)
        dp_progress_bar.set_value(0)
        dp_gen_btn.props("disabled loading")

        def _progress_cb(cur: int, total: int, msg: str):
            dp_progress_label.set_text(msg)
            dp_progress_bar.set_value(cur / max(total, 1))

        try:
            from detail_page import generate_detail_page as _gen_dp
            loop        = _asyncio.get_running_loop()
            result_path = await loop.run_in_executor(
                None,
                _functools.partial(
                    _gen_dp,
                    product_name  = product_name,
                    specs         = specs,
                    image_paths   = image_paths,
                    section_count = section_count,
                    api_key       = api_key,
                    use_composite = use_composite,
                    progress_cb   = _progress_cb,
                ),
            )
            if result_path:
                _dp_state["result_path"] = result_path
                rel         = _Path(result_path).relative_to(_IMG_DIR)
                preview_url = f"/img/{rel.as_posix()}"
                dp_preview_img.set_source(preview_url)
                dp_preview_img.style(
                    "width:100%; max-height:600px; object-fit:contain; display:block"
                )
                dp_preview_label.set_text(f"생성 완료 — {_Path(result_path).name}")
                dp_upload_btn.set_visibility(True)
                dp_progress_bar.set_value(1.0)
                ui.notify("상세페이지 이미지 생성 완료!", type="positive", timeout=3000)
            else:
                dp_preview_label.set_text("생성 실패 — 로그를 확인하세요.")
                ui.notify("생성 실패", type="negative", timeout=3000)
        except Exception as ex:
            dp_preview_label.set_text(f"오류: {ex}")
            ui.notify(f"오류: {ex}", type="negative", timeout=4000)
        finally:
            _dp_state["running"] = False
            dp_gen_btn.props(remove="disabled loading")
            dp_progress_bar.set_visibility(False)

    dp_gen_btn.on_click(_on_generate)

    # ── R2 업로드 ─────────────────────────────────────────────────
    async def _on_r2_upload():
        result_path = _dp_state.get("result_path", "")
        if not result_path or not _Path(result_path).is_file():
            ui.notify("먼저 상세페이지 이미지를 생성하세요.", type="warning", timeout=2000)
            return
        dp_upload_btn.props("disabled loading")
        dp_result_url_label.set_text("업로드 중...")
        try:
            from modules.image_uploader import upload_file as _upload_file
            loop = _asyncio.get_running_loop()
            url  = await loop.run_in_executor(None, lambda: _upload_file(result_path))
            if url:
                _dp_state["result_url"] = url
                dp_result_url_label.set_text(f"R2 URL: {url}")
                ui.notify("R2 업로드 완료!", type="positive", timeout=3000)
            else:
                dp_result_url_label.set_text("R2 업로드 실패")
                ui.notify("R2 업로드 실패", type="negative", timeout=3000)
        except Exception as ex:
            dp_result_url_label.set_text(f"오류: {ex}")
            ui.notify(f"업로드 오류: {ex}", type="negative", timeout=4000)
        finally:
            dp_upload_btn.props(remove="disabled loading")

    dp_upload_btn.on_click(_on_r2_upload)


# ── 메인 페이지 ──────────────────────────────────────────────────

@ui.page("/")
def page() -> None:

    _add_common_head()

    # ── 로그아웃 버튼 (우상단 고정) ──────────────────────────────────
    with ui.element('div').style('position:fixed;top:8px;right:12px;z-index:9999'):
        ui.button('로그아웃', on_click=lambda: ui.navigate.to('/logout')) \
          .props('flat dense color=grey-5 size=sm')

    # ── 페이지 상태 (전역 공유 — 페이지 이동 후에도 유지) ─────────
    queue          = _global_queue          # 전역 참조 (초기화 금지)
    _template_path = _global_template_path
    _output_file   = _global_output_file

    # ── 현재 큐가 히스토리에 없으면 즉시 기록 ───────────────────
    # (코드 추가 전 시작된 수집 / 복원된 항목 등 히스토리 누락 방지)
    if _global_queue:
        try:
            import json as _jhq, datetime as _dthq
            _existing = _load_collection_history()
            # 큐의 URL 집합
            _cur_urls = {e.url for e in _global_queue if e.url}
            # 이미 동일한 URL 집합의 회차가 있으면 스킵
            _already = any(
                {ed.get("url") for ed in r.get("entries", [])} == _cur_urls
                for r in _existing
            )
            if not _already:
                _save_collection_history(_global_queue)
        except Exception:
            pass

    with ui.element("div").props("id=page-wrap"):
        _make_nav_header("main")
        ui.separator()

        # ── 2열 레이아웃 ──────────────────────────────────────────
        with ui.row().classes("w-full gap-4 items-start"):

            # ════════════════════════════════════════════════════
            # 왼쪽 패널: 단계별 설정 Wizard
            # ════════════════════════════════════════════════════
            with ui.card().classes("shadow-sm").style("width:440px; flex-shrink:0;"):

                # ── 좌측 패널 탭 ─────────────────────────────────────────
                with ui.tabs().props("dense").classes("w-full") as _mode_tabs:
                    _tab_m      = ui.tab("📦 다중 등록")
                    _tab_detail = ui.tab("🖼 상세페이지 생성").set_visibility(False)
                with ui.tab_panels(_mode_tabs, value=_tab_m).classes("w-full p-0"):
                    with ui.tab_panel(_tab_m).classes("p-0"):

                        # ─────────────────────────────────────────────────
                        # 단계 헬퍼: 완료된 단계의 요약 행 + 수정 버튼
                        # ─────────────────────────────────────────────────
                        def _make_step_header(num: int, title: str):
                            """단계 번호 뱃지 + 제목 행."""
                            with ui.row().classes("items-center gap-2 mb-3"):
                                ui.badge(str(num)).props("color=blue rounded").style(
                                    "font-size:13px; min-width:24px; height:24px;"
                                    "display:flex; align-items:center; justify-content:center;"
                                )
                                ui.label(title).classes("font-bold text-slate-200 text-base")

                        # ══════════════════════════════════════════════════
                        # STEP 1 — 배송 설정
                        # ══════════════════════════════════════════════════
                        with ui.card_section() as _s1_body:
                            _make_step_header(1, "배송 설정")
                            ui.label("출고리드타임 — 엑셀에 자동 적용").classes(
                                "text-xs text-slate-400 mb-2"
                            )
                            shipping_mode = ui.toggle(
                                {"domestic": "🚚 국내배송  2일", "overseas": "✈️ 해외배송  10일"},
                                value="domestic",
                            ).props("dense")
                            shipping_hint = ui.label("출고리드타임: 2일").classes(
                                "text-xs text-slate-500 mt-1 mb-3"
                            )
                            def _update_ship_hint(e=None):
                                t = "10일 (해외배송)" if shipping_mode.value == "overseas" else "2일 (국내배송)"
                                shipping_hint.set_text(f"출고리드타임: {t}")
                            shipping_mode.on_value_change(_update_ship_hint)

                        # 완료 요약 행 (초기 숨김)
                        with ui.card_section().classes("py-1") as _s1_summary:
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.badge("1").props("color=green rounded").style(
                                    "font-size:12px; min-width:20px; height:20px;"
                                )
                                _s1_sum_lbl = ui.label("").classes(
                                    "text-sm font-semibold text-green-400 flex-1"
                                )
                                ui.button("수정", icon="edit", on_click=lambda: (
                                    _s1_summary.set_visibility(False),
                                    _s1_body.set_visibility(True),
                                    _s2_body.set_visibility(False),
                                    _s2_summary.set_visibility(False),
                                    _s3_body.set_visibility(False),
                                    _s3_summary.set_visibility(False),
                                    _s4_body.set_visibility(False),
                                )).props("flat dense size=xs color=grey")
                        _s1_summary.set_visibility(False)

                        # 다음 버튼 (step1)
                        with ui.card_section().classes("pt-0") as _s1_next_row:
                            def _next_step1():
                                label = "🚚 국내배송 2일" if shipping_mode.value == "domestic" else "✈️ 해외배송 10일"
                                _s1_sum_lbl.set_text(label)
                                _s1_body.set_visibility(False)
                                _s1_next_row.set_visibility(False)
                                _s1_summary.set_visibility(True)
                                _s2_body.set_visibility(True)
                                _s2_next_row.set_visibility(True)
                            ui.button("다음 ▶", icon="arrow_forward", on_click=_next_step1).props(
                                "color=blue dense"
                            ).classes("w-full")

                        ui.separator()

                        # ══════════════════════════════════════════════════
                        # STEP 2 — 수량 설정
                        # ══════════════════════════════════════════════════
                        with ui.card_section() as _s2_body:
                            _make_step_header(2, "수량 설정")

                            qty_mode = ui.toggle(
                                {"range": "🔢 최대 수량", "pick": "☑ 개별 선택"},
                                value="range",
                            ).props("dense").classes("mb-2")

                            with ui.column().classes("gap-1 mb-1") as _panel_range:
                                with ui.row().classes("items-start gap-2"):
                                    with ui.column().classes("gap-0 items-center"):
                                        ui.label("최소 등록").classes(
                                            "text-xs font-bold text-yellow-300 tracking-tight"
                                        )
                                        with ui.row().classes("items-center gap-1"):
                                            qty_min_input = ui.number(
                                                value=1, min=1, max=100, step=1, format="%.0f",
                                            ).props("dense outlined color=yellow").style(
                                                "width:68px; font-weight:700;"
                                            ).tooltip("최솟값")
                                            ui.label("개~").classes("text-sm font-bold text-white")
                                    with ui.column().classes("gap-0 items-center"):
                                        ui.label("최대 수량").classes(
                                            "text-xs font-bold text-sky-300 tracking-tight"
                                        )
                                        with ui.row().classes("items-center gap-1"):
                                            qty_max_input = ui.number(
                                                value=12, min=1, max=100, step=1, format="%.0f",
                                            ).props("dense outlined color=light-blue").style(
                                                "width:68px; font-weight:700;"
                                            ).tooltip("최댓값")
                                            ui.label("개").classes("text-sm font-bold text-white")
                                qty_range_preview = ui.label(
                                    "→ 1, 2, 3개 자동 산출"
                                ).classes("text-xs font-semibold text-blue-400 ml-1")

                                def _refresh_preview(e=None):
                                    try:
                                        mn = max(1, min(100, int(qty_min_input.value or 1)))
                                        mx = max(mn, min(100, int(qty_max_input.value or 12)))
                                    except Exception:
                                        mn, mx = 1, 12
                                    qty_range_preview.set_text(
                                        f"→ {', '.join(str(i) for i in range(mn, mx + 1))}개 자동 산출"
                                    )
                                qty_min_input.on_value_change(_refresh_preview)
                                qty_max_input.on_value_change(_refresh_preview)

                            _qty_checks: dict[int, ui.checkbox] = {}
                            with ui.column().classes("gap-0 mb-1") as _panel_pick:
                                with ui.row().classes("items-center gap-2 mb-1"):
                                    ui.label("원하는 수량만 체크:").classes("text-xs text-slate-400")
                                    _all_checked = {"v": False}
                                    _toggle_all_btn = ui.button("전체 선택").props(
                                        "dense flat size=xs color=blue-grey"
                                    ).classes("text-xs")
                                    def _toggle_all():
                                        _all_checked["v"] = not _all_checked["v"]
                                        for _cb in _qty_checks.values():
                                            _cb.set_value(_all_checked["v"])
                                        _toggle_all_btn.set_text(
                                            "전체 해제" if _all_checked["v"] else "전체 선택"
                                        )
                                    _toggle_all_btn.on_click(_toggle_all)
                                with ui.grid(columns=5).classes("gap-x-1 gap-y-0"):
                                    for _q in range(1, 31):
                                        _cb = ui.checkbox(f"{_q}개", value=False).classes("text-xs")
                                        _qty_checks[_q] = _cb
                            _panel_pick.set_visibility(False)

                            def _on_qty_mode(e):
                                _panel_range.set_visibility(e.value == "range")
                                _panel_pick.set_visibility(e.value == "pick")
                            qty_mode.on_value_change(_on_qty_mode)

                            # 기본 용량 UI 제거 — 상품명에서 자동 추출(_parse_volume)하므로 불필요
                            default_vol      = type("_V", (), {"value": 0})()       # 하위 참조 호환용 더미
                            default_vol_unit = type("_U", (), {"value": "L"})()     # 하위 참조 호환용 더미

                            # 목록 전체 적용
                            def _apply_qty_to_all():
                                if qty_mode.value == "range":
                                    try:
                                        _mn = max(1, min(100, int(qty_min_input.value or 1)))
                                        _mx = max(_mn, min(100, int(qty_max_input.value or 12)))
                                    except Exception:
                                        _mn, _mx = 1, 12
                                    new_qtys = list(range(_mn, _mx + 1)) or [_mn]
                                    new_min  = _mn
                                else:
                                    new_qtys = sorted(q for q, cb in _qty_checks.items() if cb.value) or [1]
                                    new_min  = new_qtys[0]
                                updated = 0
                                for _e in queue:
                                    if _e.status == "pending":
                                        _e.qtys = new_qtys[:]
                                        _e.min_qty = new_min
                                        _e.qty_locked = False
                                        updated += 1
                                _render_queue()
                                ui.notify(f"✅ {updated}개 항목에 수량 {new_min}~{max(new_qtys)}개 적용됨",
                                          type="positive", timeout=2500)
                            ui.button("📋 목록 전체 적용", icon="playlist_add_check",
                                      on_click=_apply_qty_to_all).props(
                                "color=teal dense outline").classes("w-full mt-2")
                        _s2_body.set_visibility(False)

                        # 완료 요약 행
                        with ui.card_section().classes("py-1") as _s2_summary:
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.badge("2").props("color=green rounded").style(
                                    "font-size:12px; min-width:20px; height:20px;"
                                )
                                _s2_sum_lbl = ui.label("").classes(
                                    "text-sm font-semibold text-green-400 flex-1"
                                )
                                ui.button("수정", icon="edit", on_click=lambda: (
                                    _s2_summary.set_visibility(False),
                                    _s2_body.set_visibility(True),
                                    _s2_next_row.set_visibility(True),
                                    _s3_body.set_visibility(False),
                                    _s3_summary.set_visibility(False),
                                    _s4_body.set_visibility(False),
                                )).props("flat dense size=xs color=grey")
                        _s2_summary.set_visibility(False)

                        with ui.card_section().classes("pt-0") as _s2_next_row:
                            def _next_step2():
                                if qty_mode.value == "range":
                                    try:
                                        _mn = max(1, min(100, int(qty_min_input.value or 1)))
                                        _mx = max(_mn, min(100, int(qty_max_input.value or 12)))
                                    except Exception:
                                        _mn, _mx = 1, 12
                                    lbl = f"{_mn}~{_mx}개"
                                else:
                                    picked = sorted(q for q, cb in _qty_checks.items() if cb.value)
                                    lbl = f"{picked}개 선택" if picked else "미선택"
                                _s2_sum_lbl.set_text(f"수량 {lbl}")
                                _s2_body.set_visibility(False)
                                _s2_next_row.set_visibility(False)
                                _s2_summary.set_visibility(True)
                                _s3_body.set_visibility(True)
                                _s3_next_row.set_visibility(True)
                            ui.button("다음 ▶", icon="arrow_forward", on_click=_next_step2).props(
                                "color=blue dense"
                            ).classes("w-full")
                        _s2_next_row.set_visibility(False)

                        ui.separator()

                        # ══════════════════════════════════════════════════
                        # STEP 3 — 마진율
                        # ══════════════════════════════════════════════════
                        with ui.card_section() as _s3_body:
                            _make_step_header(3, "마진율")
                            with ui.row().classes("items-center gap-3"):
                                ui.label("판매가 ×").classes("text-xs text-slate-400")
                                margin_rate_input = ui.number(
                                    value=1.35, min=1.0, max=5.0, step=0.01, format="%.2f",
                                ).props("dense outlined").style("width:90px")
                                margin_hint = ui.label("마진 35%").classes("text-xs text-slate-400")
                            def _update_hint(e=None):
                                try:
                                    m = float(margin_rate_input.value or 1.35)
                                    margin_hint.set_text(f"마진 {(m-1)*100:.0f}%")
                                except Exception:
                                    pass
                            margin_rate_input.on_value_change(_update_hint)
                        _s3_body.set_visibility(False)

                        # 완료 요약 행
                        with ui.card_section().classes("py-1") as _s3_summary:
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.badge("3").props("color=green rounded").style(
                                    "font-size:12px; min-width:20px; height:20px;"
                                )
                                _s3_sum_lbl = ui.label("").classes(
                                    "text-sm font-semibold text-green-400 flex-1"
                                )
                                ui.button("수정", icon="edit", on_click=lambda: (
                                    _s3_summary.set_visibility(False),
                                    _s3_body.set_visibility(True),
                                    _s3_next_row.set_visibility(True),
                                    _s_nobg_body.set_visibility(False),
                                    _s_nobg_summary.set_visibility(False),
                                    _s4_body.set_visibility(False),
                                )).props("flat dense size=xs color=grey")
                        _s3_summary.set_visibility(False)

                        with ui.card_section().classes("pt-0") as _s3_next_row:
                            def _next_step3():
                                try:
                                    m = float(margin_rate_input.value or 1.35)
                                    lbl = f"×{m:.2f} (마진 {(m-1)*100:.0f}%)"
                                except Exception:
                                    lbl = "×1.35"
                                _s3_sum_lbl.set_text(lbl)
                                _s3_body.set_visibility(False)
                                _s3_next_row.set_visibility(False)
                                _s3_summary.set_visibility(True)
                                _s_nobg_body.set_visibility(True)
                                _s_nobg_next_row.set_visibility(True)
                            ui.button("다음 ▶", icon="arrow_forward", on_click=_next_step3).props(
                                "color=blue dense"
                            ).classes("w-full")
                        _s3_next_row.set_visibility(False)

                        ui.separator()

                        # ══════════════════════════════════════════════════
                        # STEP 4 — 누끼(배경제거) 설정
                        # ══════════════════════════════════════════════════
                        with ui.card_section() as _s_nobg_body:
                            _make_step_header(4, "누끼(배경제거) 설정")
                            ui.label("상품 이미지의 배경을 자동 제거합니다.").classes(
                                "text-xs text-slate-400 mb-2"
                            )
                            nobg_toggle = ui.toggle(
                                {"on": "✂️ 누끼 ON  (배경 제거)", "off": "🖼️ 누끼 OFF (원본 유지)"},
                                value="off",
                            ).props("dense")
                            nobg_hint = ui.label("배경 자동 제거 후 합성 이미지 생성").classes(
                                "text-xs text-slate-500 mt-1 mb-2"
                            )
                            def _update_nobg_hint(e=None):
                                if nobg_toggle.value == "on":
                                    nobg_hint.set_text("배경 자동 제거 후 합성 이미지 생성")
                                else:
                                    nobg_hint.set_text("원본 이미지 그대로 사용 (배경 제거 안 함)")
                            nobg_toggle.on_value_change(_update_nobg_hint)
                        _s_nobg_body.set_visibility(False)

                        with ui.card_section().classes("py-1") as _s_nobg_summary:
                            with ui.row().classes("items-center gap-2 w-full"):
                                ui.badge("4").props("color=green rounded").style(
                                    "font-size:12px; min-width:20px; height:20px;"
                                )
                                _s_nobg_sum_lbl = ui.label("").classes(
                                    "text-sm font-semibold text-green-400 flex-1"
                                )
                                ui.button("수정", icon="edit", on_click=lambda: (
                                    _s_nobg_summary.set_visibility(False),
                                    _s_nobg_body.set_visibility(True),
                                    _s_nobg_next_row.set_visibility(True),
                                    _s4_body.set_visibility(False),
                                )).props("flat dense size=xs color=grey")
                        _s_nobg_summary.set_visibility(False)

                        with ui.card_section().classes("pt-0") as _s_nobg_next_row:
                            def _next_step_nobg():
                                lbl = "✂️ 누끼 ON" if nobg_toggle.value == "on" else "🖼️ 누끼 OFF"
                                _s_nobg_sum_lbl.set_text(lbl)
                                _s_nobg_body.set_visibility(False)
                                _s_nobg_next_row.set_visibility(False)
                                _s_nobg_summary.set_visibility(True)
                                _s4_body.set_visibility(True)
                            ui.button("다음 ▶", icon="arrow_forward", on_click=_next_step_nobg).props(
                                "color=blue dense"
                            ).classes("w-full")
                        _s_nobg_next_row.set_visibility(False)

                        ui.separator()

                        # ══════════════════════════════════════════════════
                        # STEP 5 — URL 입력 + 메모장 업로드
                        # ══════════════════════════════════════════════════
                        with ui.card_section() as _s4_body:
                            _make_step_header(5, "URL 추가")

                            # ── 스토어 선택 (토글 버튼) ──────────────────
                            _main_store_val = {"v": "샵케이"}

                            class _main_store_sel:
                                """ui.select 호환 인터페이스 (value 프로퍼티)."""
                                @property
                                def value(self): return _main_store_val["v"]

                            _main_store_sel = _main_store_sel()

                            _S_BASE = "border-radius:{r}; font-size:13px; padding:5px 16px; min-width:90px; font-weight:{w}; color:{c}; background:{bg} !important; border:2px solid {bd}; transition:all .15s;"
                            _S_RED  = _S_BASE.format(r="8px 0 0 8px", w="700", c="#fff", bg="#e53935", bd="#e53935")
                            _S_BLUE = _S_BASE.format(r="0 8px 8px 0", w="700", c="#fff", bg="#1e88e5", bd="#1e88e5")
                            _S_OFF1 = _S_BASE.format(r="8px 0 0 8px", w="500", c="#6b7280", bg="#1e1e2e", bd="#374151")
                            _S_OFF2 = _S_BASE.format(r="0 8px 8px 0", w="500", c="#6b7280", bg="#1e1e2e", bd="#374151")

                            with ui.row().classes("gap-0 items-center mb-3"):
                                ui.label("스토어:").classes("text-xs text-slate-400 font-semibold mr-3")
                                _sb1 = ui.button("샵케이").props("unelevated dense no-caps").style(_S_RED)
                                _sb2 = ui.button("제니스 트레이딩").props("unelevated dense no-caps").style(_S_OFF2)

                                def _sel_store(name):
                                    _main_store_val["v"] = name
                                    if name == "샵케이":
                                        _sb1.style(_S_RED)
                                        _sb2.style(_S_OFF2)
                                    else:
                                        _sb1.style(_S_OFF1)
                                        _sb2.style(_S_BLUE)

                                _sb1.on_click(lambda: _sel_store("샵케이"))
                                _sb2.on_click(lambda: _sel_store("제니스 트레이딩"))

                            new_url_input = ui.input(
                                placeholder="https://smartstore.naver.com/.../products/..."
                            ).props("dense outlined clearable").style("width:100%")
                            with ui.row().classes("gap-2 mt-2 items-center w-full"):
                                new_brand_input = ui.input(
                                    placeholder="브랜드 (비워두면 자동추출)",
                                ).props("dense outlined").style("flex:1")
                                add_btn = ui.button("추가", icon="add").props("color=blue dense")

                            ui.separator().classes("my-2")
                            ui.label("URL 일괄 추가 (메모장 파일)").classes(
                                "text-xs font-semibold text-slate-400 mb-1"
                            )
                            ui.label(
                                "한 줄에 URL 하나 • https:// 로 시작하는 줄 자동 인식"
                            ).classes("text-xs text-slate-500 mb-1")
                            bulk_result_lbl = ui.label("").classes("text-xs text-green-500")

                            async def _on_bulk_upload(e):
                                try:
                                    if hasattr(e, "file") and e.file is not None:
                                        content = await e.file.text("utf-8")
                                        _fname  = getattr(e.file, "name", "") or getattr(e, "name", "")
                                    else:
                                        content = e.content.read().decode("utf-8", errors="ignore")
                                        _fname  = getattr(e, "name", "")

                                    _fsize = len(content.encode("utf-8"))
                                    urls = _parse_urls_from_text(content)
                                    naver_urls = [u for u in urls if "smartstore.naver.com" in u]
                                    other_urls  = len(urls) - len(naver_urls)

                                    # ── 이전 배치 복원 확인 ───────────────────────────────
                                    # 파일명 + 크기가 같으면 "이전 작업 파일" 로 판단
                                    _prev   = _last_batch_meta.get("v") or {}
                                    _prev_name = _prev.get("filename", "")
                                    _prev_size = _prev.get("filesize", 0)
                                    _prev_qtys = _prev.get("url_qtys", {})
                                    _restore_qtys: dict = {}

                                    if (
                                        _fname and _prev_name
                                        and _fname == _prev_name
                                        and _fsize == _prev_size
                                        and _prev_qtys
                                    ):
                                        # 다이얼로그로 복원 여부 묻기
                                        _restore_confirmed = {"v": None}   # None=대기, True/False=선택

                                        with ui.dialog() as _restore_dlg, ui.card().classes("p-4"):
                                            ui.label("📂 이전 작업 파일로 확인됩니다").classes(
                                                "text-base font-bold text-sky-400 mb-2"
                                            )
                                            ui.label(
                                                f"파일: {_fname}\n"
                                                f"이전 작업한 수량 설정({len(_prev_qtys)}개 URL)을 복원하시겠습니까?"
                                            ).classes("text-sm text-slate-300 whitespace-pre-line mb-4")
                                            with ui.row().classes("gap-3 justify-end w-full"):
                                                def _yes():
                                                    _restore_confirmed["v"] = True
                                                    _restore_dlg.close()
                                                def _no():
                                                    _restore_confirmed["v"] = False
                                                    _restore_dlg.close()
                                                ui.button("예 — 이전 수량 복원", on_click=_yes).props(
                                                    "color=sky dense"
                                                )
                                                ui.button("아니오 — 패널 설정 사용", on_click=_no).props(
                                                    "flat dense color=grey"
                                                )

                                        _restore_dlg.open()
                                        # 사용자가 선택할 때까지 폴링 대기 (최대 60초)
                                        for _ in range(600):
                                            await asyncio.sleep(0.1)
                                            if _restore_confirmed["v"] is not None:
                                                break

                                        if _restore_confirmed["v"] is True:
                                            _restore_qtys = _prev_qtys

                                    added = skipped = 0
                                    _seen_pids: set[str] = set()
                                    for u in naver_urls:
                                        _pid = _naver_product_id(u)
                                        if _is_duplicate_url(u, queue) or _pid in _seen_pids:
                                            skipped += 1
                                            continue
                                        _seen_pids.add(_pid)
                                        brand_val = new_brand_input.value.strip()

                                        # 복원 데이터 있으면 우선 사용, 없으면 패널 설정값
                                        if _restore_qtys and u in _restore_qtys:
                                            _rq    = _restore_qtys[u]
                                            _mn    = max(1, int(_rq.get("min_qty", 1)))
                                            _mx    = max(_mn, int(_rq.get("max_qty", _mn)))
                                            default_qtys = list(range(_mn, _mx + 1)) or [_mn]
                                            default_min  = _mn
                                            _vol   = float(_rq.get("volume", 0))
                                            _vunit = _rq.get("volume_unit", "L") or "L"
                                            _brand = _rq.get("brand", brand_val) or brand_val
                                        else:
                                            if qty_mode.value == "range":
                                                try:
                                                    _mn = max(1, min(100, int(qty_min_input.value or 1)))
                                                    _mx = max(_mn, min(100, int(qty_max_input.value or 12)))
                                                except Exception:
                                                    _mn, _mx = 1, 12
                                                default_qtys = list(range(_mn, _mx + 1)) or [_mn]
                                                default_min  = _mn
                                            else:
                                                default_qtys = sorted(
                                                    q for q, cb in _qty_checks.items() if cb.value
                                                ) or [1, 2, 3]
                                                default_min  = default_qtys[0] if default_qtys else 1
                                            _vol   = float(default_vol.value or 0)
                                            _vunit = default_vol_unit.value or "L"
                                            _brand = brand_val

                                        queue.append(QueueEntry(
                                            uid          = uuid.uuid4().hex[:8],
                                            url          = u,
                                            brand        = _brand,
                                            brand_locked = bool(_brand),  # 브랜드 입력 시 처리 중 덮어쓰기 방지
                                            qtys         = default_qtys,
                                            min_qty      = default_min,
                                            volume       = _vol,
                                            volume_unit  = _vunit,
                                            gosisi_cat   = "기타 재화",
                                            qty_locked   = bool(_restore_qtys and u in _restore_qtys),
                                            use_nobg     = nobg_toggle.value == "on",
                                            source_file  = _fname or "",
                                            lead_time    = 10 if shipping_mode.value == "overseas" else 2,
                                            watch_store  = _main_store_sel.value or "샵케이",
                                        ))
                                        added += 1

                                    # ── 이번 파일 식별 정보를 메모리에만 보관 ──────────
                                    # filename/filesize/url_qtys 는 [전체 처리 시작] 시 한 번에
                                    # 저장 → 세 값이 항상 같은 파일 기준으로 동기화됨
                                    if _fname and added:
                                        _pending_batch_meta["v"] = {
                                            "filename": _fname,
                                            "filesize": _fsize,
                                        }

                                    _render_queue()
                                    msg_parts = [f"{added}개 추가"]
                                    if _restore_qtys: msg_parts.append("이전 수량 복원됨")
                                    if skipped:       msg_parts.append(f"{skipped}개 중복")
                                    if other_urls:    msg_parts.append(f"{other_urls}개 비네이버 제외")
                                    msg = " / ".join(msg_parts)
                                    bulk_result_lbl.set_text(msg)
                                    if added:
                                        try:
                                            await ui.run_javascript(_DING_JS)
                                        except Exception:
                                            pass
                                    ui.notify(msg, type="positive" if added else "warning")
                                except Exception as ex:
                                    ui.notify(f"파일 읽기 오류: {ex}", type="negative")

                            ui.upload(
                                label="📁  .txt 파일 선택",
                                on_upload=_on_bulk_upload,
                                auto_upload=True,
                                multiple=False,
                            ).props("accept=.txt flat dense color=teal")

                            # ── 엑셀 템플릿 (접힘 형태) ───────────────────────
                            ui.separator().classes("my-2")
                            with ui.expansion("📋 엑셀 템플릿 설정", icon="table_chart").props(
                                "dense flat"
                            ).classes("text-xs text-slate-400 w-full"):
                                ui.label(
                                    "Wing에서 다운받은 양식을 data/templates/ 에 넣으면 자동 인식"
                                ).classes("text-xs text-slate-500 mb-1")

                                def _scan_templates() -> list[Path]:
                                    found: list[Path] = []
                                    for ext in ("*.xlsx", "*.xlsm", "*.xls",
                                                "*.XLSX", "*.XLSM", "*.XLS"):
                                        found.extend(_TMPL_ROOT.glob(ext))
                                    seen: set[str] = set()
                                    unique: list[Path] = []
                                    for p in found:
                                        key = p.name.lower()
                                        if key not in seen:
                                            seen.add(key)
                                            unique.append(p)
                                    return sorted(unique, key=lambda p: p.name)

                                tmpl_files = _scan_templates()
                                if tmpl_files:
                                    if not _template_path["v"]:
                                        _template_path["v"] = str(tmpl_files[0])
                                    tmpl_label = ui.label(
                                        f"✅ {Path(_template_path['v']).name if _template_path['v'] else tmpl_files[0].name}"
                                    ).classes("text-xs text-green-500 font-semibold")
                                else:
                                    tmpl_label = ui.label(
                                        "템플릿 없음 — 표준 포맷 자동 생성"
                                    ).classes("text-xs text-orange-400")

                            def _refresh_template():
                                tmpl_files2 = _scan_templates()
                                if tmpl_files2:
                                    _template_path["v"] = str(tmpl_files2[0])
                                    tmpl_label.set_text(f"✅ {tmpl_files2[0].name}")
                                    tmpl_label.classes(remove="text-orange-400")
                                    tmpl_label.classes(add="text-green-500 font-semibold")
                                else:
                                    _template_path["v"] = ""
                                    tmpl_label.set_text("템플릿 없음 — 표준 포맷 자동 생성")
                                    tmpl_label.classes(remove="text-green-500 font-semibold")
                                    tmpl_label.classes(add="text-orange-400")
                            ui.button("새로고침", icon="refresh", on_click=_refresh_template).props(
                                "flat dense size=sm color=grey"
                            )
                    _s4_body.set_visibility(False)

                    # ── 상세페이지 생성 탭 패널 (비활성) ─────────────
                    with ui.tab_panel(_tab_detail).classes("p-0"):
                        pass

                # ── 최근 수집목록 카드 ─────────────────────────────
                with ui.card().classes("shadow-sm w-full"):
                    with ui.card_section():
                        with ui.row().classes("items-center justify-between mb-2"):
                            ui.label("🕘 최근 수집목록").classes("font-bold text-slate-700")
                            ui.button(
                                "", icon="refresh",
                                on_click=lambda: _render_history(),
                            ).props("flat dense round size=sm color=teal").tooltip("새로고침")
                        history_container = ui.column().classes("w-full gap-2")
                        history_page_row  = ui.row().classes("items-center gap-1 mt-1 w-full justify-center")

            # ════════════════════════════════════════════════════
            # 오른쪽 패널: 큐 목록 + 로그
            # ════════════════════════════════════════════════════
            with ui.column().classes("gap-4 flex-1 min-w-0"):

                # ── 메인 큐 카드 (버튼 + 진행 + 배송비 + 다운로드 + 아이템 전부 포함) ──
                with ui.card().classes("shadow-sm w-full"):

                    # ① 헤더: 목록수 + 목록초기화 + 판매요청시작
                    with ui.card_section():
                        queue_source_lbl = ui.label("").classes(
                            "text-xs text-teal-400 font-medium mb-1"
                        )
                        with ui.row().classes("items-center justify-between mb-2"):
                            queue_count_lbl = ui.label("등록 대기 목록 (0개)").classes(
                                "font-bold text-slate-700"
                            )
                            with ui.row().classes("gap-1 items-center"):
                                async def _on_reset_queue():
                                    # 수집 중 경고
                                    if _global_running["v"]:
                                        with ui.dialog() as _dlg2, ui.card():
                                            ui.label("⚠️ 현재 상품 수집 중입니다").classes("font-bold text-red-500 text-base")
                                            ui.label("중단하고 초기화하겠습니까?\n수집 중인 작업은 즉시 멈추지 않을 수 있습니다.").classes("text-sm text-slate-600 whitespace-pre-line")
                                            with ui.row().classes("gap-2 justify-end mt-2"):
                                                ui.button("취소", on_click=_dlg2.close).props("flat")
                                                def _force_reset_running():
                                                    _dlg2.close()
                                                    _clear_queue()
                                                    _global_log_buffer.clear()
                                                    ui.notify("✅ 목록·로그 초기화 완료", type="positive", timeout=3000)
                                                ui.button("중단 후 초기화", on_click=_force_reset_running).props("color=red")
                                        _dlg2.open()
                                        return
                                    # 백업 미완 경고
                                    if _output_file["v"] and not _backup_done["v"]:
                                        with ui.dialog() as _dlg, ui.card():
                                            ui.label("⚠️ 백업하지 않은 엑셀이 있습니다").classes("font-bold text-orange-600 text-base")
                                            ui.label("목록을 초기화하면 현재 엑셀 결과가 사라집니다.\n정말 초기화하시겠습니까?").classes("text-sm text-slate-600 whitespace-pre-line")
                                            with ui.row().classes("gap-2 justify-end mt-2"):
                                                ui.button("취소", on_click=_dlg.close).props("flat")
                                                def _force_reset():
                                                    _dlg.close()
                                                    _clear_queue()
                                                    _global_log_buffer.clear()
                                                    ui.notify("✅ 목록·로그 초기화 완료 — 새 메모장을 추가하세요", type="positive", timeout=3000)
                                                ui.button("초기화", on_click=_force_reset).props("color=red")
                                        _dlg.open()
                                        return
                                    _clear_queue()
                                    _global_log_buffer.clear()
                                    ui.notify("✅ 목록·로그 초기화 완료 — 새 메모장을 추가하세요", type="positive", timeout=3000)
                                ui.button("🗑 목록 초기화", icon="delete_sweep",
                                    on_click=_on_reset_queue,
                                ).props("color=red-7 outline size=md").classes("font-bold")
                                wing_pub_btn_shopk = ui.button(
                                    "🚀 샵케이", icon="send"
                                ).props("color=deep-orange size=md").classes("font-bold").on_click(
                                    lambda: _on_wing_publish("샵케이")
                                )
                                wing_pub_btn_zenith = ui.button(
                                    "🚀 제니스 트레이딩", icon="send"
                                ).props("color=indigo size=md").classes("font-bold").on_click(
                                    lambda: _on_wing_publish("제니스 트레이딩")
                                )
                        wing_pub_status = ui.label("").classes("text-xs text-slate-500")

                    # ② 실행 컨트롤: 전체처리시작 + 진행 + 오류 네비
                    with ui.card_section().classes("py-2 border-t border-slate-700"):
                        with ui.row().classes("items-center gap-3 flex-wrap"):
                            run_btn = ui.button(
                                "🚀 전체 처리 시작", icon="rocket_launch",
                            ).props("color=blue size=md").classes("font-bold")
                            stop_btn = ui.button(
                                "⏹ 중단", icon="stop",
                            ).props("color=red size=md outline").classes("font-bold")
                            stop_btn.set_visibility(False)
                            def _on_stop():
                                if _global_task and not _global_task.done():
                                    _global_task.cancel()
                                stop_btn.set_enabled(False)
                                ui.notify("⏹ 즉시 중단 중...", type="warning", timeout=3000)
                            stop_btn.on_click(_on_stop)
                            run_spinner = ui.spinner("dots", size="md", color="blue")
                            run_spinner.set_visibility(False)
                            progress_lbl = ui.label("").classes("text-sm text-slate-400 flex-1")
                            error_nav_row = ui.row().classes("gap-1 items-center")
                            error_nav_row.set_visibility(False)
                        progress_bar = ui.linear_progress(value=0).classes("w-full mt-1")
                        progress_bar.set_visibility(False)

                    # ③ 배송비 추천 패널 (완료 항목 있을 때 표시)
                    shipping_summary_card = ui.card_section().classes(
                        "py-2 border-t border-slate-700"
                    ).style("border-left:4px solid #f59e0b; background:#1c1a12;")
                    shipping_summary_card.set_visibility(False)
                    with shipping_summary_card:
                        with ui.row().classes("items-center gap-2 mb-1"):
                            ui.icon("local_shipping", color="amber", size="sm")
                            ui.label("Wing 배송비 설정 가이드").classes(
                                "font-bold text-amber-400 text-sm"
                            )
                        shipping_summary_lbl = ui.label("").classes(
                            "text-sm font-bold text-amber-400"
                        )
                        shipping_summary_sub = ui.label("").classes(
                            "text-xs text-slate-400 mt-1"
                        )

                    # ④ 다운로드 + 백업 (완료 후 표시)
                    _backup_done = {"v": False}  # 페이지 스코프 — 초기화·reset 공유

                    def _set_beforeunload(active: bool):
                        if active:
                            ui.run_javascript(
                                "window._backupNeeded = true;"
                                "window.onbeforeunload = function(e){"
                                "  if(window._backupNeeded){"
                                "    e.preventDefault(); e.returnValue='백업을 완료하지 않았습니다. 정말 나가시겠습니까?';"
                                "    return e.returnValue;"
                                "  }"
                                "};"
                            )
                        else:
                            ui.run_javascript(
                                "window._backupNeeded = false;"
                                "window.onbeforeunload = null;"
                            )

                    dl_card = ui.card_section().classes(
                        "py-2 border-t border-slate-700"
                    ).style("border-left:4px solid #22c55e; background:#0f2318;")
                    dl_card.set_visibility(bool(_output_file["v"]))
                    with dl_card:
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon("check_circle", color="green", size="md")
                            with ui.column().classes("gap-0 flex-1"):
                                dl_title = ui.label("엑셀 생성 완료!").classes(
                                    "font-bold text-green-400 text-base"
                                )
                                dl_subtitle = ui.label(
                                    _output_file["v"] if _output_file["v"] else ""
                                ).classes("text-xs text-slate-400 font-mono")
                        dl_issue_lbl = ui.label("").classes(
                            "text-xs text-red-400 font-bold mb-2"
                        )
                        with ui.row().classes("gap-2 w-full flex-wrap"):

                            async def _on_dl_click():
                                _done_entries = [e for e in _global_queue if e.status == "done"]
                                success_items = [e.result_item for e in _done_entries if e.result_item]
                                if not success_items:
                                    ui.notify("다운로드할 완료 항목이 없습니다.", type="warning")
                                    return
                                _crit_total = sum(
                                    1 for e in _done_entries
                                    for i in _excel_issues(e)
                                    if i["severity"] == "critical"
                                )
                                if _crit_total > 0:
                                    ui.notify(
                                        f"❌ 필수 오류 {_crit_total}개 — 각 카드에서 수정 후 다운로드하세요.",
                                        type="negative", timeout=4000,
                                    )
                                    return
                                dl_btn.set_enabled(False)
                                dl_btn.set_text("⏳ 생성 중...")
                                try:
                                    tmpl = _template_path.get("v", "")
                                    builder = ExcelBuilder(
                                        template_path=tmpl or None,
                                        output_dir=str(_OUTPUT_ROOT),
                                    )
                                    _loop = asyncio.get_running_loop()
                                    out_path = await _loop.run_in_executor(
                                        None, lambda: builder.build(success_items)
                                    )
                                    _output_file["v"] = out_path.name
                                    dl_subtitle.set_text(out_path.name)
                                    ui.notify(f"✅ 엑셀 생성 완료 ({len(success_items)}개)", type="positive", timeout=2000)
                                    ui.download(str(out_path))
                                except Exception as _de:
                                    ui.notify(f"엑셀 생성 오류: {_de}", type="negative", timeout=5000)
                                finally:
                                    dl_btn.set_enabled(True)
                                    dl_btn.set_text("⬇ 엑셀 다운로드")

                            dl_btn = ui.button(
                                "⬇ 엑셀 다운로드", icon="download",
                                on_click=_on_dl_click,
                            ).props("color=green").classes("font-bold flex-1")

                            async def _on_backup_click():
                                if not _output_file["v"]:
                                    ui.notify("저장할 엑셀 파일이 없습니다.", type="warning")
                                    return
                                src = _OUTPUT_ROOT / _output_file["v"]
                                if not src.exists():
                                    ui.notify("엑셀 파일을 찾을 수 없습니다.", type="negative")
                                    return
                                import shutil as _sh, datetime as _dtnow
                                _ts = _dtnow.datetime.now().strftime("%Y%m%d_%H%M%S")
                                done_cnt = sum(1 for e in _global_queue if e.result_item)
                                _bk_name = f"{_ts}_쿠팡_{done_cnt}개_{src.name}"
                                _bk_path = _BACKUP_ROOT / _bk_name
                                try:
                                    _sh.copy2(src, _bk_path)
                                    ui.notify(f"💾 백업 저장 완료: {_bk_name}", type="positive")
                                    _backup_done["v"] = True
                                    _set_beforeunload(False)
                                    backup_btn.props("color=green")
                                    backup_btn.set_text("✅ 백업 완료")
                                    _refresh_backup_list()
                                except Exception as _be:
                                    ui.notify(f"백업 실패: {_be}", type="negative")

                            backup_btn = ui.button(
                                "💾 백업 저장", icon="save",
                                on_click=_on_backup_click,
                            ).props("color=indigo outline").classes("flex-1") \
                             .tooltip("이 엑셀을 백업 폴더에 수동 저장합니다")

                        with ui.expansion("📂 백업 목록 보기", icon="folder_open").props(
                            "dense flat"
                        ).classes("text-xs text-slate-400 w-full mt-2"):
                            with ui.row().classes("items-center justify-between w-full mb-1"):
                                ui.label("수동 저장한 백업 파일").classes("text-xs text-slate-400")
                                def _open_backup_folder():
                                    import subprocess as _sp
                                    _sp.Popen(["explorer", str(_BACKUP_ROOT)])
                                ui.button("폴더 열기", icon="open_in_new",
                                          on_click=_open_backup_folder).props(
                                    "flat dense size=xs color=grey"
                                )
                            backup_list_col = ui.column().classes("w-full gap-0")
                            def _refresh_backup_list():
                                backup_list_col.clear()
                                _bk_files = sorted(
                                    list(_BACKUP_ROOT.glob("*.xlsx")) +
                                    list(_BACKUP_ROOT.glob("*.xlsm")),
                                    key=lambda p: p.stat().st_mtime,
                                    reverse=True,
                                )[:20]
                                import datetime as _dtbk
                                with backup_list_col:
                                    if not _bk_files:
                                        ui.label("아직 백업 없음").classes("text-xs text-slate-500 py-2")
                                    else:
                                        for _bkf in _bk_files:
                                            _sz = _bkf.stat().st_size // 1024
                                            _mt = _dtbk.datetime.fromtimestamp(
                                                _bkf.stat().st_mtime
                                            ).strftime("%m/%d %H:%M")
                                            with ui.row().classes(
                                                "items-center w-full py-1 border-b border-slate-700"
                                            ):
                                                ui.label(
                                                    f"📄 {_bkf.name[:45]}{'…' if len(_bkf.name)>45 else ''}"
                                                ).classes("text-xs text-slate-400 flex-1")
                                                ui.label(f"{_mt} {_sz}KB").classes(
                                                    "text-xs text-slate-500 whitespace-nowrap mx-2"
                                                )
                                                ui.button(
                                                    icon="download",
                                                    on_click=lambda p=_bkf: ui.download(str(p)),
                                                ).props("flat dense round size=xs color=teal") \
                                                 .tooltip("이 백업 파일 다운로드")
                            _refresh_backup_list()

                    # ── 배송방식 일괄전환 (미사용 — 로직 보존) ────
                    ship_conv_card = ui.card_section()
                    ship_conv_card.set_visibility(False)
                    with ship_conv_card:
                        with ui.row().classes("items-center gap-3 flex-wrap"):
                            ship_conv_lbl = ui.label("배송방식 일괄전환").classes("font-bold text-sky-700")
                            _ship_hint_lbl = ui.label("").classes("text-xs text-slate-500")
                            def _toggle_shipping():
                                _OVERSEAS_TAG = (
                                    "<img src='https://pub-52f3ccc0b1874a4dbca6ac2b8b860d49.r2.dev/"
                                    "%EC%9A%B0%EB%A7%88%EC%9D%B4%EB%A7%88%EC%BC%93.png'>"
                                )
                                done_ents = [e for e in queue if e.status in ("done", "error") and e.result_item]
                                if not done_ents:
                                    ui.notify("전환할 완료 항목이 없습니다.", type="warning")
                                    return
                                overseas_n = sum(1 for e in done_ents if e.lead_time == 10)
                                to_overseas = overseas_n <= len(done_ents) // 2
                                new_lt = 10 if to_overseas else 2
                                for e in done_ents:
                                    e.lead_time = new_lt
                                    if e.result_item:
                                        e.result_item.lead_time = new_lt
                                        desc = e.result_item.detail_description or ""
                                        if to_overseas and _OVERSEAS_TAG not in desc:
                                            e.result_item.detail_description = _OVERSEAS_TAG + desc
                                        elif not to_overseas:
                                            e.result_item.detail_description = desc.replace(_OVERSEAS_TAG, "")
                                mode_str = "✈️ 해외배송 10일" if to_overseas else "🚚 국내배송 3일"
                                _ship_hint_lbl.set_text(f"→ {len(done_ents)}개 {mode_str} 전환됨")
                                ui.notify(f"{len(done_ents)}개 항목 {mode_str}로 전환 완료", type="positive", timeout=3000)
                            ui.button("🚚↔✈️ 국내/해외 전환", on_click=_toggle_shipping).props("color=sky dense")
                            async def _regen_excel():
                                success_items = [e.result_item for e in queue if e.result_item]
                                if not success_items:
                                    ui.notify("재생성할 완료 항목이 없습니다.", type="warning")
                                    return
                                tmpl = _template_path.get("v", "")
                                builder = ExcelBuilder(template_path=tmpl or None, output_dir=str(_OUTPUT_ROOT))
                                loop = asyncio.get_running_loop()
                                out_path = await loop.run_in_executor(None, lambda: builder.build(success_items))
                                _output_file["v"] = out_path.name
                                _backup_done["v"] = False
                                _set_beforeunload(True)
                                dl_card.set_visibility(True)
                                ui.notify("✅ 엑셀 재생성 완료!", type="positive")
                            ui.button("📥 엑셀 재생성", on_click=_regen_excel).props("color=indigo dense outline")

                    # ⑤ 큐 아이템 컨테이너 (스크롤 네비 타겟)
                    queue_container = ui.column().classes("w-full gap-2 px-2 pb-2")
                    empty_label = ui.label(
                        "⬆️  왼쪽에서 URL을 입력하고 추가하세요."
                    ).classes("text-slate-400 text-sm text-center py-4 w-full")

                # ── 로그 (탭: 수집 / Wing) ───────────────────────
                _LOG_STYLE = (
                    "width:100% !important;"
                    "height:480px;"
                    "background:#0d1117; color:#cdd6f4;"
                    "font-family:monospace; font-size:13px;"
                    "border-radius:6px; padding:10px;"
                )

                async def _copy_log():
                    if not _global_log_buffer:
                        ui.notify("복사할 로그가 없습니다.", type="warning", timeout=2000)
                        return
                    text = "\n".join(_global_log_buffer)
                    esc  = text.replace("\\", "\\\\").replace("`", "\\`")
                    await ui.run_javascript(
                        f"navigator.clipboard.writeText(`{esc}`)"
                        f".catch(()=>{{const t=document.createElement('textarea');"
                        f"t.value=`{esc}`;document.body.appendChild(t);"
                        f"t.select();document.execCommand('copy');"
                        f"document.body.removeChild(t);}});"
                    )
                    ui.notify(f"수집 로그 복사 완료! ({len(text):,}자)", type="positive", timeout=2500)

                async def _copy_wing_log():
                    if not _global_wing_log_buffer:
                        ui.notify("복사할 Wing 로그가 없습니다.", type="warning", timeout=2000)
                        return
                    text = "\n".join(_global_wing_log_buffer)
                    esc  = text.replace("\\", "\\\\").replace("`", "\\`")
                    await ui.run_javascript(
                        f"navigator.clipboard.writeText(`{esc}`)"
                        f".catch(()=>{{const t=document.createElement('textarea');"
                        f"t.value=`{esc}`;document.body.appendChild(t);"
                        f"t.select();document.execCommand('copy');"
                        f"document.body.removeChild(t);}});"
                    )
                    ui.notify(f"Wing 로그 복사 완료! ({len(text):,}자)", type="positive", timeout=2500)

                with ui.card().classes("shadow-sm w-full"):
                    with ui.tabs().classes("w-full") as _log_tabs:
                        _tab_collect = ui.tab("📦 수집 로그")
                        _tab_wing    = ui.tab("🚀 Wing 판매요청 로그")
                    with ui.tab_panels(_log_tabs, value=_tab_collect).classes("w-full"):
                        with ui.tab_panel(_tab_collect):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label("수집 로그").classes("font-bold text-slate-700")
                                with ui.row().classes("gap-1"):
                                    ui.button("복사", icon="content_copy", on_click=_copy_log).props("flat dense size=sm color=teal")
                                    ui.button("지우기", icon="delete_outline",
                                        on_click=lambda: (log_widget.clear(), _global_log_buffer.clear()),
                                    ).props("flat dense size=sm color=grey")
                            log_widget = ui.log(max_lines=5000).style(_LOG_STYLE).classes("nicegui-log")
                        with ui.tab_panel(_tab_wing):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label("Wing 판매요청 로그").classes("font-bold text-slate-700")
                                with ui.row().classes("gap-1"):
                                    ui.button("복사", icon="content_copy", on_click=_copy_wing_log).props("flat dense size=sm color=teal")
                                    ui.button("지우기", icon="delete_outline",
                                        on_click=lambda: (wing_log_widget.clear(), _global_wing_log_buffer.clear()),
                                    ).props("flat dense size=sm color=grey")
                            wing_log_widget = ui.log(max_lines=2000).style(_LOG_STYLE).classes("nicegui-log")

    _history_page = {"v": 0}   # 현재 페이지 인덱스 (0-based)
    _HISTORY_PAGE_SIZE = 5

    def _delete_history_run(run_id: str):
        """히스토리에서 특정 회차 삭제 후 저장."""
        try:
            import json as _jd
            _hist = _load_collection_history()
            _hist = [r for r in _hist if r.get("id") != run_id]
            _HISTORY_FILE.write_text(
                _jd.dumps(_hist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as _de:
            print(f"[히스토리삭제] 오류: {_de}")
        _render_history()

    def _render_history():
        """최근 수집목록 컨테이너 재렌더링 (페이지네이션 포함)."""
        history_container.clear()
        history_page_row.clear()
        _hist = _load_collection_history()
        if not _hist:
            with history_container:
                ui.label("아직 수집 이력이 없습니다.").classes(
                    "text-xs text-slate-400 text-center py-2 w-full"
                )
            return

        total   = len(_hist)
        pages   = max(1, -(-total // _HISTORY_PAGE_SIZE))   # ceil div
        page    = max(0, min(_history_page["v"], pages - 1))
        _history_page["v"] = page
        start   = page * _HISTORY_PAGE_SIZE
        visible = _hist[start:start + _HISTORY_PAGE_SIZE]

        import datetime as _dthi
        with history_container:
            for _run in visible:
                _ts_raw = _run.get("timestamp", "")
                try:
                    _dt_obj = _dthi.datetime.fromisoformat(_ts_raw)
                    _ts_str = _dt_obj.strftime("%m/%d %H:%M")
                except Exception:
                    _ts_str = _ts_raw[:16]
                _cnt   = _run.get("count", 0)
                _lbl   = _run.get("label", "")
                _blbl  = _run.get("brand_label", "")
                _ents  = _run.get("entries", [])
                _rid   = _run.get("id", "")
                _smode = _run.get("shipping_mode", "domestic")
                _mrate = float(_run.get("margin_rate") or 1.35)

                _summary = f"{_ts_str}  {_cnt}개"
                if _blbl:
                    _summary += f"  [{_blbl}]"
                if _lbl:
                    _summary += f"  📂{_lbl}"

                with ui.row().classes(
                    "items-center justify-between w-full rounded px-2 py-1"
                    " bg-slate-50 border border-slate-200"
                ):
                    ui.label(_summary).classes("text-xs text-slate-600 flex-1 truncate")

                    async def _readd(run_entries=_ents, run_smode=_smode, run_mrate=_mrate):
                        _added = 0
                        for _ed in run_entries:
                            _url = (_ed.get("url") or "").strip()
                            if not _url:
                                continue
                            if any(e.url == _url for e in queue):
                                continue
                            import uuid as _uuid2
                            _new_e = QueueEntry(
                                uid               = _uuid2.uuid4().hex[:8],
                                url               = _url,
                                brand             = _ed.get("brand", ""),
                                qtys              = _ed.get("qtys") or [1],
                                min_qty           = int(_ed.get("min_qty") or 1),
                                qty_locked        = bool(_ed.get("qty_locked", False)),
                                volume            = float(_ed.get("volume") or 0),
                                volume_unit       = _ed.get("volume_unit", "L"),
                                use_nobg          = bool(_ed.get("use_nobg", False)),
                                draft             = bool(_ed.get("draft", False)),
                                gtin              = _ed.get("gtin", ""),
                                source_file       = _ed.get("source_file", ""),
                                gosisi_cat        = _ed.get("gosisi_cat", "기타 재화"),
                                category_id       = _ed.get("category_id", ""),
                                category_is_manual= bool(_ed.get("category_is_manual", False)),
                                manual_options    = _ed.get("manual_options") or [],
                                lead_time         = int(_ed.get("lead_time") or 2),
                                watch_store       = _ed.get("watch_store", "샵케이"),
                                price_extra       = int(_ed.get("price_extra") or 0),
                                extra_detail_images = list(_ed.get("extra_detail_images") or []),
                                extra_detail_text   = _ed.get("extra_detail_text", ""),
                            )
                            queue.append(_new_e)
                            _added += 1
                        if _added:
                            # ── UI 설정 복원 ──────────────────────────
                            try:
                                shipping_mode.set_value(run_smode)
                                _update_ship_hint()
                            except Exception:
                                pass
                            try:
                                margin_rate_input.set_value(round(run_mrate, 2))
                            except Exception:
                                pass
                            _render_queue()
                            _smode_lbl = "해외배송" if run_smode == "overseas" else "국내배송"
                            ui.notify(
                                f"✅ {_added}개 항목 추가 · {_smode_lbl} · 마진×{run_mrate:.2f} 복원됨",
                                type="positive", timeout=4000,
                            )
                        else:
                            ui.notify("추가할 새 항목이 없습니다 (이미 모두 큐에 있음).", type="info", timeout=2500)

                    def _make_delete(rid=_rid):
                        def _do():
                            _delete_history_run(rid)
                        return _do

                    ui.button(
                        "다시 추가", icon="add_circle_outline",
                        on_click=_readd,
                    ).props("flat dense size=xs color=teal")
                    ui.button(
                        "", icon="delete",
                        on_click=_make_delete(),
                    ).props("flat dense size=xs color=red").tooltip("이 이력 삭제")

        # ── 페이지네이션 ──────────────────────────────────────────
        if pages > 1:
            with history_page_row:
                def _go(p):
                    def _do():
                        _history_page["v"] = p
                        _render_history()
                    return _do
                ui.button("", icon="chevron_left",  on_click=_go(page - 1)).props(
                    f"flat dense round size=xs {'disable' if page == 0 else ''}"
                )
                ui.label(f"{page + 1} / {pages}").classes("text-xs text-slate-500")
                ui.button("", icon="chevron_right", on_click=_go(page + 1)).props(
                    f"flat dense round size=xs {'disable' if page >= pages - 1 else ''}"
                )

    _render_history()

    # ══════════════════════════════════════════════════════════════
    # 내부 함수: 단일 항목 재수집
    # ══════════════════════════════════════════════════════════════

    async def _reprocess(e_ref: QueueEntry, new_max_qty: int, new_min_qty: int = 1) -> None:
        """수량 변경 후 단일 항목만 재수집."""
        if _running.get("v"):
            # 전체 처리 중이라도 이미 완료(done/error) 항목은 재수집 허용
            # — 전체 루프가 더 이상 해당 항목을 건드리지 않으므로 충돌 없음
            if e_ref.status in ("processing", "pending"):
                ui.notify(
                    "전체 처리 실행 중 — 처리 중/대기 중인 항목은 완료 후 재수집하세요.",
                    type="warning",
                )
                return
        mn = max(1, min(100, int(new_min_qty or 1)))
        n  = max(mn, min(100, int(new_max_qty or mn)))
        e_ref.min_qty     = mn
        e_ref.qtys        = list(range(mn, n + 1)) or [mn]
        e_ref.qty_locked  = True
        e_ref.status      = "pending"
        e_ref.result_item = None
        e_ref.error       = ""
        _render_queue()
        margin   = float(margin_rate_input.value or 1.35)
        lt       = 10 if shipping_mode.value == "overseas" else 2
        e_ref.status = "processing"
        _render_queue()
        await _process_entry(e_ref, log_widget, margin, lt, e_ref.use_nobg)
        e_ref.status = "done" if e_ref.result_item else "error"
        _render_queue()
        # 재수집 완료 후 Excel 자동 재생성 (다운로드 파일이 최신 데이터 반영하도록)
        if e_ref.result_item:
            try:
                await _regen_excel()
            except Exception as _rex:
                print(f"[재수집] Excel 재생성 실패 (수동으로 재생성 버튼 클릭하세요): {_rex}")

    # ══════════════════════════════════════════════════════════════
    # 내부 함수: 계층형 카테고리 선택 다이얼로그
    # ══════════════════════════════════════════════════════════════

    async def _open_cat_selector(e_ref: QueueEntry) -> None:
        """
        Wing 카테고리 JSON → 대분류>중분류>소분류 계층 선택 다이얼로그.
        선택 완료 후 e_ref.category_id 및 result_item.category_id 갱신.
        """
        tree = _get_wing_cat_tree()
        if not tree:
            ui.notify("카테고리 데이터 없음 — config/wing_categories.json 확인", type="negative")
            return

        _sel: list[str] = []
        _code: dict     = {"v": "", "path": ""}
        flat_list       = _get_wing_cat_flat()


        with ui.dialog() as dlg, ui.card().style(
            "min-width:580px; max-width:780px; padding:20px;"
            "background:#1e293b !important; color:#f1f5f9 !important;"
            "border:1px solid #334155 !important;"
        ):
            # ── 헤더 ─────────────────────────────────────────────
            with ui.row().classes("items-center justify-between w-full mb-3"):
                ui.label("🗂  카테고리 선택").classes("text-lg font-bold").style(
                    "color:#38bdf8"
                )
                ui.button(icon="close", on_click=dlg.close).props(
                    "flat dense size=sm"
                ).style("color:#94a3b8")

            # _apply_btn 을 아직 생성 전이지만 클로저에서 접근 가능하도록 ref 사용
            _apply_ref: dict = {"btn": None}

            # ── 선택 결과 표시 (공통) ────────────────────────────
            _res_row = ui.row().classes(
                "items-center gap-2 px-3 py-2 rounded w-full mb-2"
            ).style("background:#052e16; border:1px solid #166534;")
            _res_row.set_visibility(False)
            with _res_row:
                ui.icon("check_circle").style("color:#4ade80; font-size:18px")
                _res_lbl = ui.label("").classes("text-sm font-bold").style("color:#86efac")

            # ── 탭 ───────────────────────────────────────────────
            with ui.tabs().props("dense").style(
                "background:#0f172a; border-radius:6px; margin-bottom:12px"
            ) as tabs:
                tab_search = ui.tab("🔍 검색").classes("cat-tab")
                tab_tree   = ui.tab("📂 계층 선택").classes("cat-tab")

            with ui.tab_panels(tabs, value=tab_search).style("background:transparent"):

                # ════ 검색 탭 ════════════════════════════════════
                with ui.tab_panel(tab_search):
                    ui.label("카테고리명으로 검색").classes("text-xs").style("color:#94a3b8; margin-bottom:4px")

                    def _on_search_select(e):
                        if not e.value:
                            return
                        item = next((x for x in flat_list if x["label"] == e.value), None)
                        if item:
                            _code["v"]    = item["code"]
                            _code["path"] = item["label"]
                            _res_lbl.set_text(f"ID: {item['code']}  │  {item['name']}")
                            _res_row.set_visibility(True)
                            if _apply_ref["btn"]: _apply_ref["btn"].enable()

                    ui.select(
                        options=[x["label"] for x in flat_list],
                        label="예: 올리브오일, 원두분쇄기, 샴푸...",
                        on_change=_on_search_select,
                        with_input=True,
                    ).props(
                        "dense outlined use-input input-debounce=200 "
                        "hide-selected fill-input dark virtual-scroll-slice-size=20 "
                        "popup-content-class=cat-popup"
                    ).classes("cat-search w-full")

                # ════ 계층 탭 ════════════════════════════════════
                with ui.tab_panel(tab_tree):
                    _crumb = ui.label("").classes(
                        "text-xs font-mono px-2 py-1 rounded w-full mb-2"
                    ).style(
                        "background:#0c4a6e; color:#7dd3fc; border:1px solid #0369a1"
                    )
                    _crumb.set_visibility(False)

                    _body = ui.column().classes("gap-2 w-full")

                    def _mark_leaf(code: str, path_parts: list):
                        _code["v"]    = code
                        _code["path"] = " > ".join(path_parts)
                        _crumb.set_text(" > ".join(path_parts))
                        _crumb.set_visibility(True)
                        _res_lbl.set_text(f"ID: {code}  │  {path_parts[-1] if path_parts else ''}")
                        _res_row.set_visibility(True)
                        if _apply_ref["btn"]: _apply_ref["btn"].enable()

                    def _on_sel(depth: int, value: str):
                        _sel[depth:] = [value]
                        _redraw()

                    def _redraw():
                        _body.clear()
                        _code["v"] = ""
                        _code["path"] = ""
                        _crumb.set_visibility(False)
                        _res_row.set_visibility(False)
                        if _apply_ref["btn"]: _apply_ref["btn"].disable()

                        node = tree
                        lv_labels  = ["대분류", "중분류", "소분류", "세분류", "상세분류"]
                        path_parts: list[str] = []

                        for depth in range(7):
                            if isinstance(node, str):
                                _mark_leaf(node, path_parts)
                                break
                            if not isinstance(node, dict):
                                break
                            opts = sorted(k for k in node if not k.startswith("__"))
                            if not opts:
                                if "__code__" in node:
                                    _mark_leaf(node["__code__"], path_parts)
                                break
                            lbl     = lv_labels[depth] if depth < len(lv_labels) else f"{depth+1}단계"
                            sel_val = _sel[depth] if depth < len(_sel) else None
                            with _body:
                                with ui.row().classes("items-center gap-2 w-full"):
                                    ui.label(lbl).classes("text-xs font-semibold w-14 shrink-0").style(
                                        "color:#7dd3fc"
                                    )
                                    ui.select(
                                        opts, value=sel_val, label=lbl,
                                        on_change=lambda e, d=depth: _on_sel(d, e.value),
                                    ).props(
                                        "dense outlined use-input input-debounce=0 "
                                        "hide-selected fill-input dark "
                                        "popup-content-class=cat-popup"
                                    ).classes("cat-dlg-select").style("flex:1")
                            if sel_val is None or sel_val not in node:
                                break
                            path_parts.append(sel_val)
                            node = node[sel_val]
                            if isinstance(node, dict) and "__code__" in node:
                                sub = [k for k in node if not k.startswith("__")]
                                if not sub:
                                    _mark_leaf(node["__code__"], path_parts)
                                    break

                    _redraw()

            # ── 하단 버튼 ─────────────────────────────────────────
            ui.separator().style("background:#334155; margin:12px 0 8px")
            with ui.row().classes("justify-end gap-2 w-full"):
                ui.button("취소", on_click=dlg.close).props("flat dense").style("color:#94a3b8")
                _apply_ref["btn"] = ui.button(
                    "✅ 적용", on_click=lambda: _do_apply(),
                ).props("dense unelevated color=teal")
                _apply_ref["btn"].disable()

            async def _do_apply():
                code = _code["v"]
                if not code:
                    return
                e_ref.category_id        = code
                e_ref.category_is_manual = True
                _new_gosisi = _guide_gosisi_cat(code)
                if _new_gosisi:
                    e_ref.gosisi_cat = _new_gosisi
                # 수동 선택된 카테고리 이름으로 detected_keyword 갱신 → UI 표시용
                from modules.category_detector import get_detector as _gdet
                _cat_display_name = _gdet().get_name_by_id(code) or code
                e_ref.detected_keyword = f"[수동입력]{_cat_display_name}"
                if e_ref.result_item:
                    e_ref.result_item.category_id = code
                    if _new_gosisi:
                        e_ref.result_item.gosisi_cat = _new_gosisi
                    _new_valid = _valid_option_types(code)
                    if _new_valid and e_ref.result_item.extra_options:
                        e_ref.result_item.extra_options = [
                            (t, v) for t, v in e_ref.result_item.extra_options
                            if t in _new_valid
                        ]
                dlg.close()
                _render_queue()
                ui.notify(
                    f"카테고리 적용 ✅  {_code['path']} (ID: {code})",
                    type="positive", timeout=3000,
                )
                # 카테고리 변경 즉시 엑셀 재생성 — Wing 업로드 파일에 새 카테고리 반영
                try:
                    await _regen_excel()
                    ui.notify("📋 엑셀 재생성 완료 (변경된 카테고리 반영)", type="info", timeout=2500)
                except Exception as _re:
                    ui.notify(f"⚠️ 엑셀 재생성 실패 (수동으로 재생성 버튼 클릭): {_re}", type="warning", timeout=4000)

        dlg.open()

    # ══════════════════════════════════════════════════════════════
    # 내부 함수: 큐 UI 렌더링
    # ══════════════════════════════════════════════════════════════

    def _render_queue():
        """큐 목록 컨테이너 전체 재렌더링."""
        queue_container.clear()
        queue_count_lbl.set_text(f"등록 대기 목록 ({len(queue)}개)")
        empty_label.set_visibility(len(queue) == 0)

        # ── 출처 요약 라벨 ────────────────────────────────────────
        _txt_files   = []
        _seen_txt    = set()
        _manual_cnt  = 0
        for _e in queue:
            if _e.source_file:
                if _e.source_file not in _seen_txt:
                    _txt_files.append(_e.source_file)
                    _seen_txt.add(_e.source_file)
            else:
                _manual_cnt += 1

        if _txt_files or _manual_cnt:
            _MAX_SHOW = 3
            if _txt_files:
                _shown = " + ".join(_txt_files[:_MAX_SHOW])
                if len(_txt_files) > _MAX_SHOW:
                    _shown += f" 외 {len(_txt_files) - _MAX_SHOW}개 파일"
                if _manual_cnt:
                    _shown += f" + 직접 입력 {_manual_cnt}개 URL"
            else:
                _shown = f"직접 입력 {_manual_cnt}개 URL"
            queue_source_lbl.set_text(f"📂 {_shown}")
            queue_source_lbl.set_visibility(True)
        else:
            queue_source_lbl.set_visibility(False)

        # 엑셀 오류 카운트 실시간 업데이트 (수정 후 즉시 반영)
        if _output_file.get("v"):
            try:
                _done_ents = [e for e in queue if e.status == "done"]
                _cc = sum(1 for e in _done_ents for i in _excel_issues(e) if i["severity"] == "critical")
                _wc = sum(1 for e in _done_ents for i in _excel_issues(e) if i["severity"] == "warning")
                _it = ""
                if _cc: _it += f"❌ 필수오류 {_cc}개  "
                if _wc: _it += f"⚠ 경고 {_wc}개"
                dl_issue_lbl.set_text(_it + "  →  각 카드에서 수정 후 다운로드" if _it else "✅ 모든 항목 정상 — 다운로드 가능")
            except Exception:
                pass

        # ── 배송비 추천 패널 업데이트 ───────────────────────────────
        # 완료된 항목 중 최솟값 번들 가격의 최솟값 = Wing 배송비 합계 상한
        _done_items = [
            e for e in queue
            if e.status == "done" and e.result_item and e.result_item.bundles
        ]
        if _done_items:
            _min_prices = []
            for _de in _done_items:
                _mb = min(_de.result_item.bundles, key=lambda b: b.qty)
                _min_prices.append((_mb.original_price, _de))
            _bottleneck_price, _bottleneck_entry = min(_min_prices, key=lambda x: x[0])
            _bn = _bottleneck_entry.product_name or _bottleneck_entry.url[:30]
            _ship_color = "text-amber-400" if _bottleneck_price < 15000 else "text-green-400"
            _ship_text = (
                f"추천 배송비 합계 상한: {_bottleneck_price:,}원  "
                f"(← {_bn[:20]}... 기준)"
            )
            _ship_sub = (
                "Wing 초도배송비 + 반품배송비 합계가 위 금액 미만이어야 전체 등록 가능"
            )
            shipping_summary_lbl.set_text(_ship_text)
            shipping_summary_sub.set_text(_ship_sub)
            shipping_summary_lbl.classes(
                remove="text-amber-400 text-green-400 text-slate-400",
                add=_ship_color,
            )
            shipping_summary_card.set_visibility(True)
        else:
            shipping_summary_card.set_visibility(False)

        with queue_container:
            for _q_idx, entry in enumerate(queue, 1):
                status_color = {
                    "pending":    "grey",
                    "processing": "orange",
                    "done":       "positive",
                    "error":      "negative",
                }[entry.status]
                status_icon = {
                    "pending":    "schedule",
                    "processing": "sync",
                    "done":       "check_circle",
                    "error":      "error",
                }[entry.status]

                css_class = f"queue-card {entry.status}"
                with ui.card().classes(f"w-full shadow-sm {css_class}").props(
                    f'id="qcard-{entry.uid}"'
                ):
                    with ui.card_section().classes("py-2"):
                        # 상단: URL + 상태 + 임시저장 토글 + 삭제
                        with ui.row().classes("items-center gap-2 w-full"):
                            ui.icon(status_icon, color=status_color, size="sm")
                            with ui.column().classes("gap-0 flex-1 min-w-0"):
                                display_name = entry.product_name or entry.url[:70]
                                ui.label(f"{_q_idx}. {display_name}").classes(
                                    "text-sm font-semibold text-slate-700 truncate"
                                )
                                if entry.error:
                                    ui.label(f"❌ {entry.error[:80]}").classes(
                                        "text-xs text-red-500"
                                    )
                                with ui.row().classes("items-center gap-1"):
                                    ui.link(
                                        entry.url[:55] + "...",
                                        entry.url,
                                        new_tab=True,
                                    ).classes("text-xs text-blue-400 font-mono truncate naver-url-link")
                                    ui.button(
                                        icon="open_in_new",
                                        on_click=lambda u=entry.url: ui.run_javascript(
                                            f"window.open('{u}', '_blank')"
                                        ),
                                    ).props("flat dense size=xs color=blue").tooltip("네이버 상품 페이지 열기")
                            ui.badge(
                                {"pending": "대기", "processing": "처리중",
                                 "done": "완료", "error": "오류"}[entry.status]
                            ).props(f"color={status_color}")

                            # 배송 구분 뱃지: 클릭하면 큐 전체 국↔해 동시 전환
                            def _make_ship_toggle(e_ref=entry):
                                _is_overseas = e_ref.lead_time == 10
                                _badge = ui.badge(
                                    "해" if _is_overseas else "국"
                                ).props(
                                    f"color={'orange' if _is_overseas else 'teal'} rounded"
                                ).style(
                                    "font-size:11px; font-weight:700; min-width:22px; height:22px;"
                                    "border-radius:50%; display:inline-flex; align-items:center; justify-content:center;"
                                    "cursor:pointer;"
                                ).tooltip("클릭하면 전체 배송 전환")
                                def _toggle_all_ship(_e=e_ref):
                                    _new_lt = 2 if _e.lead_time == 10 else 10
                                    for _qe in _global_queue:
                                        _qe.lead_time = _new_lt
                                        _qe.lead_time_locked = True   # 뱃지 직접 토글 → 글로벌 패널 무시
                                        if _qe.result_item:
                                            _qe.result_item.lead_time = _new_lt  # 엑셀 재생성 시 반영
                                    _msg = "해외배송 10일" if _new_lt == 10 else "국내배송 2일"
                                    ui.notify(f"전체 → {_msg}로 변경", type="warning" if _new_lt == 10 else "info", timeout=2000)
                                    _render_queue()
                                _badge.on("click", _toggle_all_ship)
                            _make_ship_toggle()

                            # ── 누끼 ON/OFF 토글 ─────────────────────────────
                            # pending 상태면 처리 중에도 변경 가능
                            if entry.status in ("pending", "done", "error"):
                                def _make_nobg_handler(e_ref=entry):
                                    def _h(ev):
                                        e_ref.use_nobg = (ev.value == "on")
                                    return _h
                                ui.toggle(
                                    {"on": "✂️ 누끼 ON", "off": "🖼️ 누끼 OFF"},
                                    value="on" if entry.use_nobg else "off",
                                ).props("dense").classes("text-xs").on_value_change(
                                    _make_nobg_handler()
                                )

                            _uid = entry.uid

                            # ── 추가자료(이미지/텍스트) 버튼 ─────────────────
                            def _make_extra_detail_handler(e_ref=entry):
                                async def _h():
                                    _cur_imgs = list(getattr(e_ref, "extra_detail_images", None) or [])
                                    _cur_txt  = getattr(e_ref, "extra_detail_text", "") or ""
                                    with ui.dialog() as _dlg2, ui.card().classes("p-4").style("min-width:420px; max-width:600px"):
                                        ui.label("📎 추가 이미지 / 텍스트").classes(
                                            "text-base font-bold text-slate-700 mb-1"
                                        )
                                        ui.label(
                                            "기존 합성 이미지 아래에 추가됩니다. 엑셀에도 반영됩니다."
                                        ).classes("text-xs text-slate-500 mb-3")

                                        ui.label("이미지 URL (한 줄에 하나씩)").classes("text-xs font-semibold text-slate-600")
                                        _img_ta = ui.textarea(
                                            value="\n".join(_cur_imgs),
                                            placeholder="https://shop-phinf.pstatic.net/...\nhttps://...",
                                        ).props("dense outlined rows=4").style("width:100%; font-size:11px")

                                        ui.separator().classes("my-2")

                                        async def _on_file_upload(ev, _ta=None):
                                            try:
                                                if hasattr(ev, "file") and ev.file is not None:
                                                    _fb = await ev.file.read()
                                                    _fname_orig = getattr(ev.file, "name", "") or getattr(ev, "name", "img.jpg")
                                                else:
                                                    _fb = ev.content.read()
                                                    _fname_orig = getattr(ev, "name", "img.jpg")
                                                _ext = (_fname_orig.rsplit(".", 1)[-1] or "jpg").lower()
                                                _mime = "image/png" if _ext == "png" else "image/webp" if _ext == "webp" else "image/jpeg"
                                                _fname2 = f"{e_ref.uid}_extra_{len((_img_ta.value or '').splitlines())}.{_ext}"
                                                import asyncio as _aio2
                                                _loop2 = _aio2.get_running_loop()
                                                from modules.image_uploader import _do_upload as _r2up
                                                _up_url = await _loop2.run_in_executor(
                                                    None, lambda: _r2up(_fb, _fname2, _mime)
                                                )
                                                if _up_url:
                                                    _cur = (_img_ta.value or "").strip()
                                                    _img_ta.set_value((_cur + "\n" + _up_url).strip())
                                                    ui.notify("이미지 업로드 완료", type="positive", timeout=2000)
                                                else:
                                                    ui.notify("업로드 실패", type="negative")
                                            except Exception as _ue:
                                                ui.notify(f"업로드 오류: {_ue}", type="negative")

                                        ui.label("이미지 붙여넣기 / 드래그&드롭 / 파일 업로드").classes("text-xs font-semibold text-slate-600 mb-1")
                                        _paste_zone_id = f"pz_{e_ref.uid}"
                                        ui.html(
                                            f'<div id="{_paste_zone_id}" tabindex="0" '
                                            f'style="border:2px dashed #64748b;border-radius:8px;padding:18px;'
                                            f'text-align:center;color:#94a3b8;font-size:13px;cursor:pointer;'
                                            f'background:#1e293b;outline:none;">'
                                            f'📋 이미지 복사 후 여기서 <b>Ctrl+V</b> 붙여넣기<br>'
                                            f'<span style="font-size:11px;color:#64748b">또는 파일을 드래그&드롭</span>'
                                            f'</div>'
                                        )
                                        ui.upload(
                                            label="📁 파일 선택으로도 업로드 가능",
                                            on_upload=_on_file_upload,
                                            auto_upload=True,
                                            multiple=True,
                                        ).props("accept=.jpg,.jpeg,.png,.webp flat dense color=grey-7")

                                        ui.separator().classes("my-2")

                                        ui.label("추가 텍스트 (이미지로 렌더링됨)").classes("text-xs font-semibold text-slate-600")
                                        _txt_ta = ui.textarea(
                                            value=_cur_txt,
                                            placeholder="상품 설명, 사용법, 주의사항 등 자유롭게 입력...",
                                        ).props("dense outlined rows=5").style("width:100%; font-size:12px")

                                        with ui.row().classes("gap-2 justify-end mt-3 w-full"):
                                            ui.button("취소", on_click=_dlg2.close).props("flat dense")
                                            def _save2(e=e_ref, d=_dlg2, it=_img_ta, tt=_txt_ta):
                                                imgs = [u.strip() for u in (it.value or "").splitlines() if u.strip().startswith("http")]
                                                e.extra_detail_images = imgs
                                                e.extra_detail_text   = (tt.value or "").strip()
                                                d.close()
                                                _cnt = len(imgs) + (1 if e.extra_detail_text else 0)
                                                ui.notify(f"추가자료 {_cnt}건 저장됨", type="positive", timeout=2000)
                                                _render_queue()
                                            ui.button("저장", on_click=_save2).props("color=blue dense")
                                    _dlg2.open()
                                    # script 태그는 ui.html 불가 → 다이얼로그 열린 뒤 JS 주입
                                    _pzid = _paste_zone_id
                                    await ui.run_javascript(f"""
(function(){{
  var _pzid="{_pzid}";
  var _uploading=0;
  var _docHandler=null;
  function initPasteZone(){{
    var zone=document.getElementById(_pzid);
    if(!zone){{ setTimeout(initPasteZone,100); return; }}
    if(zone._pzInited) return;
    zone._pzInited=true;
    function resetZone(){{
      zone.style.borderColor="#64748b";
      zone.innerHTML='📋 이미지 복사 후 여기서 <b>Ctrl+V</b> 붙여넣기<br><span style="font-size:11px;color:#64748b">또는 파일을 드래그&드롭</span>';
    }}
    function appendUrl(url){{
      var dlg=zone.closest(".q-dialog");
      var tas=dlg?dlg.querySelectorAll("textarea"):[];
      var ta=null;
      for(var i=0;i<tas.length;i++){{
        if(tas[i].placeholder&&tas[i].placeholder.indexOf("https")!==-1){{ta=tas[i];break;}}
      }}
      if(ta){{
        var cur=(ta.value||"").trim();
        ta.value=cur?cur+"\\n"+url:url;
        ta.dispatchEvent(new Event("input",{{bubbles:true}}));
      }}
    }}
    function uploadBlob(blob,idx,total){{
      _uploading++;
      zone.style.borderColor="#f59e0b";
      zone.innerHTML="⏳ 업로드 중... "+_uploading+"/"+total+"장";
      var fd=new FormData();
      fd.append("file",blob,"clipboard_"+idx+".png");
      fetch("/api/extra-img/upload",{{method:"POST",body:fd}})
        .then(function(r){{return r.json();}})
        .then(function(data){{
          _uploading--;
          if(data.ok){{appendUrl(data.url);}}
          else{{zone.style.borderColor="#ef4444";zone.innerHTML="❌ 실패: "+(data.error||"");}}
          if(_uploading===0){{
            zone.style.borderColor="#22c55e";
            zone.innerHTML="✅ "+total+"장 업로드 완료!";
            setTimeout(resetZone,2500);
          }}
        }}).catch(function(){{
          _uploading--;
          zone.style.borderColor="#ef4444";zone.innerHTML="❌ 오류";
          if(_uploading===0)setTimeout(resetZone,3000);
        }});
    }}
    function uploadUrl(url,idx,total){{
      _uploading++;
      zone.style.borderColor="#f59e0b";
      zone.innerHTML="⏳ 다운로드 중... "+_uploading+"/"+total+"장";
      fetch("/api/extra-img/url-upload",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{url:url}})}})
        .then(function(r){{return r.json();}})
        .then(function(data){{
          _uploading--;
          if(data.ok){{appendUrl(data.url);}}
          else{{zone.style.borderColor="#ef4444";zone.innerHTML="❌ URL실패: "+(data.error||"");}}
          if(_uploading===0){{
            zone.style.borderColor="#22c55e";
            zone.innerHTML="✅ "+total+"장 완료!";
            setTimeout(resetZone,2500);
          }}
        }}).catch(function(){{
          _uploading--;
          if(_uploading===0){{zone.style.borderColor="#ef4444";zone.innerHTML="❌ 오류";setTimeout(resetZone,3000);}}
        }});
    }}
    function handlePaste(e){{
      var cd=e.clipboardData||(e.originalEvent&&e.originalEvent.clipboardData);
      if(!cd)return;
      var items=cd.items||[];
      // blobs를 미리 동기적으로 추출 (이벤트 핸들러 종료 후 items 무효화 대비)
      var blobs=[];
      var hasHtml=false;
      for(var i=0;i<items.length;i++){{
        if(items[i].type==="text/html"){{ hasHtml=true; }}
        if(items[i].type&&items[i].type.startsWith("image/")){{
          var b=items[i].getAsFile();
          if(b)blobs.push(b);
        }}
      }}
      if(hasHtml){{
        // ① HTML 우선 — JS비활성화 후 네이버 다중 이미지 선택 복사
        for(var i=0;i<items.length;i++){{
          if(items[i].type==="text/html"){{
            e.preventDefault();
            (function(capturedBlobs){{
              items[i].getAsString(function(html){{
                var doc=new DOMParser().parseFromString(html,"text/html");
                var imgs=Array.from(doc.querySelectorAll("img"));
                var srcs=imgs.map(function(img){{return img.src||img.getAttribute("src")||"";}})
                             .filter(function(s){{return s.startsWith("http");}});
                if(srcs.length>0){{
                  srcs.forEach(function(src,idx){{uploadUrl(src,idx,srcs.length);}});
                }} else if(capturedBlobs.length>0){{
                  capturedBlobs.forEach(function(bl,idx){{uploadBlob(bl,idx,capturedBlobs.length);}});
                }}
              }});
            }})(blobs);
            return;
          }}
        }}
      }}
      // ② binary blobs — 로컬 파일 복사 또는 단일 이미지
      if(blobs.length>0){{
        e.preventDefault();
        blobs.forEach(function(bl,idx){{uploadBlob(bl,idx,blobs.length);}});
      }}
    }}
    zone.addEventListener("paste",handlePaste);
    zone.addEventListener("dragover",function(e){{e.preventDefault();zone.style.borderColor="#38bdf8";}});
    zone.addEventListener("dragleave",function(){{zone.style.borderColor="#64748b";}});
    zone.addEventListener("drop",function(e){{
      e.preventDefault();
      var files=Array.from(e.dataTransfer.files).filter(function(f){{return f.type.startsWith("image/");}});
      if(files.length>0){{files.forEach(function(f,idx){{uploadBlob(f,idx,files.length);}});}}
      else{{resetZone();}}
    }});
    zone.addEventListener("click",function(){{zone.focus();}});
    zone.focus();
    // 다이얼로그 내 어디서나 Ctrl+V 작동 (존에 포커스 없어도)
    if(_docHandler){{document.removeEventListener("paste",_docHandler,true);}}
    _docHandler=function(ev){{
      var z=document.getElementById(_pzid);
      if(!z||!document.body.contains(z)){{
        document.removeEventListener("paste",_docHandler,true);
        _docHandler=null;
        return;
      }}
      if(document.activeElement===z)return; // zone 자체 이벤트는 중복 방지
      // image/* blob이 있을 때만 가로채기 (순수 텍스트/HTML paste는 무시)
      var cd2=ev.clipboardData; if(!cd2)return;
      var its=cd2.items||[];
      var hasImgBlob=false;
      for(var j=0;j<its.length;j++){{
        if(its[j].type&&its[j].type.startsWith("image/")){{hasImgBlob=true;break;}}
      }}
      if(!hasImgBlob)return;
      handlePaste(ev);
    }};
    document.addEventListener("paste",_docHandler,true);
  }}
  initPasteZone();
}})();
""")
                                return _h

                            _has_extra = bool((entry.extra_detail_images or []) or (entry.extra_detail_text or ""))
                            _extra_btn_color = "teal" if _has_extra else "grey-6"
                            _extra_cnt = len(entry.extra_detail_images or []) + (1 if entry.extra_detail_text else 0)
                            _extra_lbl = f"📎 추가자료 {_extra_cnt}건" if _has_extra else "📎 추가자료"
                            ui.button(
                                _extra_lbl,
                                on_click=_make_extra_detail_handler(),
                            ).props(f"outline size=md color={_extra_btn_color}").style(
                                "font-size:13px; font-weight:600; padding:4px 12px"
                            ).tooltip("상세페이지 하단 추가 이미지/텍스트 설정")

                            # ── 관부가세 추가 버튼 ──────────────────────────
                            def _make_customs_handler(e_ref=entry):
                                async def _h():
                                    _cur = getattr(e_ref, "price_extra", 0) or 0
                                    with ui.dialog() as _dlg, ui.card().classes("p-4 min-w-[280px]"):
                                        ui.label("관부가세 추가금액 입력").classes(
                                            "text-base font-bold text-slate-700 mb-3"
                                        )
                                        ui.label(
                                            "입력한 금액이 모든 옵션/수량 판매가에 더해지며\n"
                                            "상품명 끝에 '관부가세 포함'이 자동으로 붙습니다."
                                        ).classes("text-xs text-slate-500 whitespace-pre-line mb-3")
                                        _inp = ui.number(
                                            label="추가금액 (원)",
                                            value=_cur if _cur else None,
                                            min=0, step=10000,
                                        ).props("dense outlined").style("width:100%")
                                        with ui.row().classes("gap-2 justify-end mt-3 w-full"):
                                            if _cur > 0:
                                                def _clear(e=e_ref, d=_dlg):
                                                    e.price_extra = 0
                                                    d.close()
                                                    ui.notify("관부가세 추가 취소됨", type="info", timeout=1500)
                                                    _render_queue()
                                                ui.button("추가 취소", on_click=_clear).props("flat dense color=grey")
                                            ui.button("취소", on_click=_dlg.close).props("flat dense")
                                            def _confirm(e=e_ref, d=_dlg, i=_inp):
                                                val = int(i.value or 0)
                                                e.price_extra = val
                                                d.close()
                                                if val > 0:
                                                    ui.notify(f"관부가세 +{val:,}원 적용", type="positive", timeout=2000)
                                                _render_queue()
                                            ui.button("확인", on_click=_confirm).props("color=blue dense")
                                    _dlg.open()
                                return _h
                            _customs_label = f"🛃 +{entry.price_extra:,}원" if (entry.price_extra or 0) > 0 else "🛃 관부가세"
                            _customs_color = "orange" if (entry.price_extra or 0) > 0 else "grey-6"
                            ui.button(
                                _customs_label,
                                on_click=_make_customs_handler(),
                            ).props(f"outline size=md color={_customs_color}").style("font-size:13px; font-weight:600; padding:4px 12px").tooltip("관부가세 추가금액 설정")

                            if entry.status != "processing":
                                ui.button(
                                    icon="delete",
                                    on_click=lambda u=_uid: _remove_entry(u),
                                ).props("flat dense size=xs color=red").tooltip("삭제")

                        # done/error: 임시저장 모드 안내
                        # done/error: 카테고리 버튼 (미설정=빨강, 설정됨=회색 수정)
                        # 카테고리 미설정인 경우에만 경고+선택 버튼 표시
                        # (설정 완료 시엔 수동 수정 패널의 카테고리 항목으로 변경 가능)
                        if entry.status in ("done", "error") and not entry.category_id:
                            with ui.row().classes("items-center gap-2 mt-1"):
                                ui.label("❌ 카테고리 미설정").classes(
                                    "text-xs font-bold text-red-600"
                                )
                                def _make_sel_handler(e_ref=entry):
                                    async def _h():
                                        try:
                                            await _open_cat_selector(e_ref)
                                        except Exception as _ex:
                                            import traceback as _tb
                                            print(f"[CatSel] 오류: {_ex}")
                                            _tb.print_exc()
                                            ui.notify(f"카테고리 선택 오류: {_ex}", type="negative")
                                    return _h
                                ui.button(
                                    "🗂 카테고리 선택",
                                    on_click=_make_sel_handler(),
                                ).props("color=red dense size=sm flat").tooltip(
                                    "카테고리 미설정 — 클릭하여 선택"
                                )

                        # ── done: 중복 감지 경고 뱃지 ───────────────────────────
                        if entry.status == "done" and entry.dup_status:
                            _dup_cfg = {
                                "duplicate": ("🔴 중복 상품 주의",  "red",    "error",
                                              "이미 등록한 상품과 동일 구성일 수 있습니다. Wing에서 직접 확인하세요."),
                                "variant":   ("🟡 유사 상품 참고",  "amber",  "warning",
                                              "구성이 다른 변형 상품입니다. 등록해도 무방합니다."),
                                "unknown":   ("🟠 수동 확인 권고",  "orange", "help",
                                              "자동 판별이 불가능했습니다. 직접 확인 후 등록 여부를 결정하세요."),
                            }
                            _dc = _dup_cfg.get(entry.dup_status)
                            if _dc:
                                _dlabel, _dcolor, _dicon, _dtip = _dc
                                with ui.row().classes("items-center gap-2 mt-1 flex-wrap"):
                                    ui.icon(_dicon, color=_dcolor, size="xs")
                                    ui.label(_dlabel).classes(
                                        f"text-xs font-bold text-{_dcolor}-500"
                                    ).tooltip(_dtip)
                                    if entry.dup_reason:
                                        ui.label(f"({entry.dup_reason})").classes(
                                            "text-xs text-slate-400"
                                        )
                                if entry.dup_matched_name:
                                    ui.label(
                                        f"↳ 과거 등록: {entry.dup_matched_name[:40]}"
                                        + (f"  ({entry.dup_matched_date})" if entry.dup_matched_date else "")
                                    ).classes("text-xs text-slate-500 ml-4 font-mono")

                        # done/error: 카테고리 감지 결과 표시
                        if entry.status in ("done", "error") and entry.detected_keyword:
                            if entry.category_is_manual:
                                kw_color = "blue"
                                _manual_name = entry.detected_keyword.removeprefix("[수동입력]")
                                kw_text = (
                                    f"수동입력 카테고리: {_manual_name} | "
                                    f"ID: {entry.category_id} | "
                                    f"{entry.gosisi_cat[:18]}..."
                                )
                            else:
                                kw_color = "teal" if entry.category_id else "orange"
                                kw_text  = (
                                    f"키워드: {entry.detected_keyword} | "
                                    f"ID: {entry.category_id or '미설정'} | "
                                    f"{entry.gosisi_cat[:18]}..."
                                )
                            ui.label(kw_text).classes(
                                f"text-xs text-{kw_color}-600 font-mono mt-1"
                            )

                        # done/error: 세트 상품 차종·제조사 표시
                        _is_set_card = (
                            entry.category_id == "113070"
                            or bool(_re.search(r'교환세트|오일세트|필터세트|점검세트', entry.product_name or ""))
                        )
                        if entry.status in ("done", "error") and _is_set_card and entry.result_item:
                            _set_opts = entry.result_item.extra_options
                            _car_m  = next((v for t, v in _set_opts if t == "차종"), "")
                            _car_mk = next((v for t, v in _set_opts if t == "자동차제조사"), "")
                            _sae_g  = next((v for t, v in _set_opts if t == "엔진오일 SAE점도"), "")
                            with ui.row().classes("items-center gap-2 mt-1 flex-wrap"):
                                ui.icon("directions_car", color="teal", size="xs")
                                _set_info = []
                                if _car_mk: _set_info.append(f"제조사: {_car_mk}")
                                if _car_m:  _set_info.append(f"차종: {_car_m}")
                                if _sae_g:  _set_info.append(f"점도: {_sae_g}")
                                ui.label(
                                    "  |  ".join(_set_info) if _set_info else "⚠ 차종·제조사 미감지"
                                ).classes("text-xs text-teal-600 font-mono")
                                ui.label("(수량: 1세트)").classes("text-xs text-slate-400")

                        # done/error: GTIN 표시 (자동조회 결과만 — 수동입력 제거)
                        if entry.status in ("done", "error"):
                            with ui.row().classes("items-center gap-2 mt-1"):
                                gtin_display = (
                                    f"🔖 GTIN: {entry.gtin}"
                                    if entry.gtin else "🔖 GTIN: 미조회"
                                )
                                gtin_color = "text-teal-600" if entry.gtin else "text-slate-400"
                                ui.label(gtin_display).classes(f"text-xs font-mono {gtin_color}")

                        # done: 최소 등록 수량 기준 최저 판매가 표시
                        # Wing 배송비 설정 시 (초도+반품) ≤ 이 금액 이어야 등록 가능
                        if entry.status == "done" and entry.result_item and entry.result_item.bundles:
                            _min_bundle = min(entry.result_item.bundles, key=lambda b: b.qty)
                            _min_price  = _min_bundle.original_price
                            _min_qty    = _min_bundle.qty
                            with ui.row().classes("items-center gap-1 mt-1"):
                                ui.icon("local_shipping", size="xs", color="amber")
                                ui.label(
                                    f"최저 판매가 ({_min_qty}개): {_min_price:,}원"
                                ).classes("text-xs font-bold text-amber-400")
                                ui.label(
                                    "← Wing 초도+반품 배송비 합계가 이 금액 미만이어야 등록 가능"
                                ).classes("text-xs text-slate-500")

                        # ── done/error: 엑셀 등록 필수 항목 검증 + 수기 수정 ──
                        if entry.status in ("done", "error"):
                            _issues = _excel_issues(entry)
                            if _issues:
                                _crit = [i for i in _issues if i["severity"] == "critical"]
                                _warn = [i for i in _issues if i["severity"] == "warning"]
                                _hdr_color = "red" if _crit else "amber"
                                _hdr_icon  = "error" if _crit else "warning"
                                _hdr_text  = (
                                    f"❌ 엑셀 오류 {len(_crit)}개"
                                    + (f"  ⚠ 경고 {len(_warn)}개" if _warn else "")
                                    if _crit else
                                    f"⚠ 경고 {len(_warn)}개"
                                )
                                with ui.expansion(_hdr_text, icon=_hdr_icon).props(
                                    f"dense header-class='text-{_hdr_color}-600 font-bold text-xs'"
                                ).classes("w-full mt-1 border border-red-200 rounded"):
                                    with ui.column().classes("gap-2 py-1 w-full"):
                                        for _iss in _issues:
                                            _sev_color = "red" if _iss["severity"] == "critical" else "amber"
                                            _sev_icon  = "cancel" if _iss["severity"] == "critical" else "warning_amber"
                                            with ui.row().classes("items-start gap-2 w-full flex-wrap"):
                                                ui.icon(_sev_icon, color=_sev_color, size="xs")
                                                ui.label(_iss["msg"]).classes(
                                                    f"text-xs text-{_sev_color}-700 flex-1"
                                                )

                                            # ── 필드별 수정 입력란 ──────────────
                                            if _iss["field"] == "brand" and _iss["fixable"]:
                                                def _make_brand_fix(e_ref=entry):
                                                    _b_inp = ui.input(
                                                        value=e_ref.brand,
                                                        placeholder="브랜드명 입력",
                                                    ).props("dense outlined").style("width:160px")
                                                    def _h(ev=None):
                                                        v = (_b_inp.value or "").strip()
                                                        if v:
                                                            e_ref.brand = v
                                                            e_ref.brand_locked = True
                                                            if e_ref.result_item:
                                                                e_ref.result_item.brand = v
                                                                e_ref.result_item.manufacturer = v
                                                            ui.notify(f"브랜드 저장: {v}", type="positive", timeout=1500)
                                                            _render_queue()
                                                    _b_inp.on("blur", _h)
                                                _make_brand_fix()

                                            elif _iss["field"] == "product_name" and _iss["fixable"]:
                                                def _make_name_fix(e_ref=entry):
                                                    _n_inp = ui.input(
                                                        value=e_ref.product_name,
                                                        placeholder="상품명 입력 (5자 이상)",
                                                    ).props("dense outlined").style("width:260px")
                                                    def _h(ev=None):
                                                        v = (_n_inp.value or "").strip()
                                                        if len(v) >= 5:
                                                            e_ref.product_name = v
                                                            if e_ref.result_item:
                                                                e_ref.result_item.product_name = v
                                                            ui.notify(f"상품명 저장완료", type="positive", timeout=1500)
                                                            _render_queue()
                                                        else:
                                                            ui.notify("5자 이상 입력하세요.", type="warning", timeout=2000)
                                                    _n_inp.on("blur", _h)
                                                _make_name_fix()

                                            elif _iss["field"] == "image" and _iss["fixable"]:
                                                def _make_img_fix(e_ref=entry):
                                                    _img_inp = ui.input(
                                                        placeholder="이미지 URL 직접 입력",
                                                    ).props("dense outlined").style("width:260px")
                                                    def _h(ev=None):
                                                        v = (_img_inp.value or "").strip()
                                                        if v.startswith("http"):
                                                            if e_ref.result_item:
                                                                e_ref.result_item.main_image_url = v
                                                                if e_ref.result_item.bundles:
                                                                    e_ref.result_item.bundles[0].image_url = v
                                                            ui.notify("이미지 URL 저장", type="positive", timeout=1500)
                                                            _render_queue()
                                                        else:
                                                            ui.notify("http로 시작하는 URL을 입력하세요.", type="warning", timeout=2000)
                                                    _img_inp.on("blur", _h)
                                                _make_img_fix()

                        # ── done/error: 범용 수동 수정 패널 ─────────────────────
                        # error 항목은 result_item이 없어서 _excel_issues 입력란이 뜨지 않음.
                        # done 항목도 issue가 없으면 편집 수단이 없으므로 항상 노출.
                        if entry.status in ("done", "error"):
                            with ui.expansion(
                                "✏️ 수동 수정",
                                icon="edit_note",
                            ).props(
                                "dense header-class='text-slate-400 text-xs font-semibold'"
                            ).classes("w-full mt-1 border border-slate-600 rounded"):
                                with ui.column().classes("gap-3 py-2 w-full"):

                                    # ① 상품명
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label("상품명").classes(
                                            "text-xs text-slate-400 w-14 shrink-0"
                                        )
                                        def _make_pname_edit(e_ref=entry):
                                            _pn_inp = ui.input(
                                                value=e_ref.product_name or "",
                                                placeholder="상품명 (5자 이상)",
                                            ).props("dense outlined").style("width:260px")
                                            def _h(ev=None):
                                                v = (_pn_inp.value or "").strip()
                                                if len(v) >= 5:
                                                    e_ref.product_name = v
                                                    if e_ref.result_item:
                                                        e_ref.result_item.product_name = v
                                                    ui.notify("상품명 저장", type="positive", timeout=1500)
                                                    _render_queue()
                                                elif v:
                                                    ui.notify("5자 이상 입력하세요.", type="warning", timeout=2000)
                                            _pn_inp.on("blur", _h)
                                            _pn_inp.on("change", _h)
                                        _make_pname_edit()

                                    # ② 브랜드
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label("브랜드").classes(
                                            "text-xs text-slate-400 w-14 shrink-0"
                                        )
                                        def _make_brand_edit(e_ref=entry):
                                            _br_inp = ui.input(
                                                value=e_ref.brand or "",
                                                placeholder="브랜드명",
                                            ).props("dense outlined").style("width:180px")
                                            def _h(ev=None):
                                                v = (_br_inp.value or "").strip()
                                                if v:
                                                    e_ref.brand = v
                                                    e_ref.brand_locked = True
                                                    if e_ref.result_item:
                                                        e_ref.result_item.brand = v
                                                        e_ref.result_item.manufacturer = v
                                                    ui.notify(f"브랜드 저장: {v}", type="positive", timeout=1500)
                                                    _render_queue()
                                            _br_inp.on("blur", _h)
                                            _br_inp.on("change", _h)
                                        _make_brand_edit()

                                    # ③ GTIN(바코드)
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label("GTIN").classes(
                                            "text-xs text-slate-400 w-14 shrink-0"
                                        )
                                        def _make_gtin_edit(e_ref=entry):
                                            _gt_inp = ui.input(
                                                value=e_ref.gtin or "",
                                                placeholder="바코드 (8~13자리 숫자, 없으면 공란)",
                                            ).props("dense outlined").style("width:200px")
                                            def _h(ev=None):
                                                v = (_gt_inp.value or "").strip().replace("-", "").replace(" ", "")
                                                if v and not v.isdigit():
                                                    ui.notify("숫자만 입력하세요.", type="warning", timeout=2000)
                                                    return
                                                e_ref.gtin = v
                                                if e_ref.result_item:
                                                    e_ref.result_item.gtin = v
                                                ui.notify(f"GTIN {'저장: ' + v if v else '삭제'}", type="positive", timeout=1500)
                                                _render_queue()
                                            _gt_inp.on("blur", _h)
                                        _make_gtin_edit()

                                    # ④ 카테고리 — 직접 ID 입력 또는 계층 선택 다이얼로그
                                    with ui.row().classes("items-center gap-2 flex-wrap"):
                                        ui.label("카테고리").classes(
                                            "text-xs text-slate-400 w-14 shrink-0"
                                        )
                                        def _make_cat_manual_edit(e_ref=entry):
                                            _cur_id = e_ref.category_id or ""
                                            _cat_id_inp = ui.input(
                                                value=_cur_id,
                                                placeholder="카테고리 ID (예: 58832)",
                                            ).props("dense outlined").style("width:140px").tooltip(
                                                f"현재: {_cur_id or '미설정'}"
                                            )
                                            async def _h(ev=None):
                                                v = (_cat_id_inp.value or "").strip()
                                                e_ref.category_id = v
                                                e_ref.category_is_manual = bool(v)
                                                if e_ref.result_item:
                                                    e_ref.result_item.category_id = v
                                                if v:
                                                    ui.notify(f"카테고리 ID 저장: {v}", type="positive", timeout=1500)
                                                _render_queue()
                                                if v:
                                                    try:
                                                        await _regen_excel()
                                                    except Exception:
                                                        pass
                                            _cat_id_inp.on("blur", _h)
                                            _cat_id_inp.on("change", _h)

                                            # 계층 선택 다이얼로그 버튼
                                            def _make_cat_dlg_handler(e_ref2=e_ref):
                                                async def _dlg():
                                                    try:
                                                        await _open_cat_selector(e_ref2)
                                                    except Exception as _ex:
                                                        ui.notify(f"카테고리 선택 오류: {_ex}", type="negative")
                                                return _dlg
                                            ui.button(
                                                "🗂", on_click=_make_cat_dlg_handler(),
                                            ).props("flat dense size=sm color=teal").tooltip("계층 카테고리 선택")
                                        _make_cat_manual_edit()

                                    # ⑤ error 항목 안내: 재수집 유도
                                    if entry.status == "error":
                                        ui.label(
                                            "💡 위 정보 수정 후 아래 🔄 재수집 버튼을 누르면 해당 항목만 다시 처리됩니다."
                                        ).classes("text-xs text-sky-400 mt-1")

                        # ── done/error: 용량 미감지 시 직접 입력 → 수량 자동 재계산 (엔진오일 카테고리만)
                        _OIL_CATS_UI = {"78889", "78897", "78893", "78903", "78894"}
                        _no_volume = (
                            entry.status in ("done", "error")
                            and entry.volume == 0
                            and entry.qtys == [1]
                            and entry.category_id in _OIL_CATS_UI
                            and entry.category_id not in {"113070"}
                            and not bool(_re.search(r'교환세트|오일세트|필터세트|점검세트', entry.product_name or ""))
                        )
                        if _no_volume:
                            with ui.row().classes("items-center gap-2 mt-1 flex-wrap"):
                                ui.label("⚠️ 용량/중량 미감지").classes("text-xs font-bold text-amber-500")
                                _vinj_inp = ui.number(
                                    value=None,
                                    min=0.01, max=99999, step=1, format="%.2f",
                                    placeholder="숫자 입력",
                                ).props("dense outlined").style("width:100px").tooltip(
                                    "숫자 입력 후 단위 선택 → 묶음 수량 자동 계산"
                                )
                                # 단위 선택 (용량·중량 전체)
                                _UNIT_OPTIONS = {
                                    "ml": "ml (밀리리터)",
                                    "L":  "L (리터)",
                                    "cc": "cc",
                                    "g":  "g (그램)",
                                    "kg": "kg (킬로그램)",
                                    "mg": "mg (밀리그램)",
                                    "oz": "oz (온스)",
                                    "개": "개 (낱개)",
                                    "매": "매 (장)",
                                    "팩": "팩",
                                    "box": "box (박스)",
                                    "정": "정 (알약 등)",
                                    "캡슐": "캡슐",
                                    "포": "포",
                                    "m":  "m (미터)",
                                    "cm": "cm (센티미터)",
                                }
                                _vinj_unit = ui.select(
                                    options=_UNIT_OPTIONS,
                                    value="L",
                                ).props("dense outlined").style("width:130px")

                                def _make_vol_inject_handler(e_ref=entry, inp=_vinj_inp, unit_sel=_vinj_unit):
                                    def _h():
                                        try:
                                            val = float(inp.value or 0)
                                        except Exception:
                                            val = 0
                                        if val <= 0:
                                            ui.notify("수치를 입력해주세요.", type="warning", timeout=2000)
                                            return
                                        unit = unit_sel.value or "L"
                                        # L 환산 (수량 계산용 — L 계열만 적용)
                                        if unit in ("ml", "cc"):
                                            vol_l = val / 1000
                                        elif unit == "L":
                                            vol_l = val
                                        else:
                                            vol_l = 0   # 중량·개수류는 수량 자동계산 불가 → 단품 고정
                                        e_ref.volume      = val
                                        e_ref.volume_unit = unit
                                        if vol_l > 0:
                                            new_max = _volume_to_max_qty(vol_l)
                                            e_ref.qtys = list(range(1, new_max + 1))
                                            msg = f"✅ {val}{unit} ({vol_l:.3g}L) → 수량 1~{new_max}개"
                                        else:
                                            e_ref.qtys = [1]
                                            msg = f"✅ {val}{unit} 저장 → 단품(1개) 고정"
                                        e_ref.qty_locked = True

                                        # ── result_item 동기화 ─────────────────────
                                        if e_ref.result_item:
                                            item = e_ref.result_item
                                            vol_str = f"{val:g}{unit}"

                                            # extra_options에서 기존 용량/중량 제거 후 추가
                                            item.extra_options = [
                                                (t, v) for t, v in item.extra_options
                                                if t not in ("개당 용량", "중량")
                                            ]
                                            item.extra_options.append(("개당 용량", vol_str))

                                            # 기존 1개 번들의 단가 기반으로 전체 번들 재생성
                                            if item.bundles:
                                                base = min(item.bundles, key=lambda b: b.qty)
                                                unit_price_s = base.sale_price     // base.qty
                                                unit_price_o = base.original_price // base.qty
                                                base_img = base.image_url or item.main_image_url
                                                item.bundles = [
                                                    Bundle(
                                                        qty=q,
                                                        sale_price=unit_price_s * q,
                                                        original_price=unit_price_o * q,
                                                        image_url=base_img,
                                                    )
                                                    for q in e_ref.qtys
                                                ]

                                        ui.notify(msg, type="positive", timeout=2500)
                                        _render_queue()
                                    return _h

                                ui.button(
                                    "📐 적용",
                                    on_click=_make_vol_inject_handler(),
                                ).props("color=amber dense size=sm").tooltip(
                                    "단위 포함 저장 → L/ml/cc는 묶음 수량 자동계산, 나머지는 단품"
                                )

                        # done/error: 수량 변경 후 재수집
                        if entry.status in ("done", "error"):
                            _orig_min = entry.min_qty or 1
                            _orig_qty = max(entry.qtys) if entry.qtys else 1
                            with ui.row().classes("items-center gap-1 mt-1 flex-wrap"):
                                ui.label("📦").classes("text-sm mt-4")
                                with ui.column().classes("gap-0 items-center"):
                                    ui.label("최소 등록").classes(
                                        "text-xs font-bold text-yellow-500 tracking-tight"
                                    )
                                    with ui.row().classes("items-center gap-1"):
                                        _r_min_inp = ui.number(
                                            value=_orig_min,
                                            min=1, max=100, step=1, format="%.0f",
                                        ).props("dense outlined color=yellow").style(
                                            "width:70px; font-weight:700;"
                                        ).tooltip("최솟값 (시작 묶음 수량)")
                                        ui.label("개~").classes("text-sm font-bold text-white")
                                with ui.column().classes("gap-0 items-center"):
                                    ui.label("최대 수량").classes(
                                        "text-xs font-bold text-sky-500 tracking-tight"
                                    )
                                    with ui.row().classes("items-center gap-1"):
                                        _r_qty_inp = ui.number(
                                            value=_orig_qty,
                                            min=1, max=100, step=1, format="%.0f",
                                        ).props("dense outlined color=light-blue").style(
                                            "width:70px; font-weight:700;"
                                        ).tooltip("최댓값 (최대 묶음 수량)")
                                        ui.label("개").classes("text-sm font-bold text-white")

                                def _make_reprocess_handler(e_ref=entry, mn_inp=_r_min_inp, mx_inp=_r_qty_inp):
                                    async def _h():
                                        try:
                                            mn = max(1, min(100, int(mn_inp.value or 1)))
                                            mx = max(mn, min(100, int(mx_inp.value or mn)))
                                        except Exception:
                                            mn, mx = 1, 1
                                        await _reprocess(e_ref, mx, mn)
                                    return _h

                                _reprocess_btn = ui.button(
                                    "🔄 재수집",
                                    on_click=_make_reprocess_handler(),
                                ).props("color=teal dense size=sm")
                                if entry.status != "error":
                                    _reprocess_btn.set_visibility(False)

                                def _make_qty_change_handler(omn=_orig_min, omx=_orig_qty, btn=_reprocess_btn, mn_i=_r_min_inp, mx_i=_r_qty_inp, is_err=entry.status == "error"):
                                    def _h(ev=None):
                                        try:
                                            cur_mn = int(mn_i.value or 1)
                                            cur_mx = int(mx_i.value or 1)
                                        except Exception:
                                            cur_mn, cur_mx = omn, omx
                                        if is_err:
                                            btn.set_visibility(True)
                                        else:
                                            btn.set_visibility(cur_mn != omn or cur_mx != omx)
                                    return _h
                                _hdl = _make_qty_change_handler()
                                _r_min_inp.on_value_change(_hdl)
                                _r_qty_inp.on_value_change(_hdl)

                        # 하단: 브랜드 / 용량 (pending 상태에서만 편집 가능)
                        if entry.status == "pending":
                            with ui.row().classes("gap-2 mt-1 flex-wrap items-center"):
                                _uid = entry.uid

                                _brand_inp = ui.input(
                                    value=entry.brand,
                                    placeholder="브랜드",
                                ).props("dense outlined").style("width:120px")

                                def _make_brand_handler(e_ref=entry, inp_ref=_brand_inp):
                                    def _h(ev=None):
                                        v = (inp_ref.value or "").strip()
                                        if v:
                                            e_ref.brand = v
                                            e_ref.brand_locked = True  # 처리 중 덮어쓰기 방지
                                        else:
                                            e_ref.brand_locked = False
                                    return _h
                                _brand_inp.on("blur", _make_brand_handler())

                                # ── 수량 설정 (다이얼로그 방식) ──────────────────
                                _qtys_sorted = sorted(entry.qtys) if entry.qtys else [1]
                                _is_pick_now = _qtys_sorted != list(range(_qtys_sorted[0], _qtys_sorted[-1] + 1))
                                if _is_pick_now:
                                    _qty_summary_txt = (
                                        ", ".join(str(q) for q in _qtys_sorted[:6])
                                        + ("…" if len(_qtys_sorted) > 6 else "")
                                        + "개"
                                    )
                                else:
                                    _qty_summary_txt = f"{_qtys_sorted[0]}~{_qtys_sorted[-1]}개"
                                _qty_lbl = ui.label(f"📦 {_qty_summary_txt}").classes(
                                    "text-sm font-bold text-cyan-300"
                                )

                                def _make_qty_dialog_opener(e_ref=entry, lbl=_qty_lbl):
                                    def _open():
                                        _q_sorted = sorted(e_ref.qtys) if e_ref.qtys else [1]
                                        _init_pick = _q_sorted != list(range(_q_sorted[0], _q_sorted[-1] + 1))
                                        with ui.dialog() as _dlg, ui.card().classes("w-80 gap-2"):
                                            ui.label("📦 수량 설정").classes("text-base font-bold mb-1")
                                            _dlg_mode = ui.toggle(
                                                {"range": "① 최소~최대 수량", "pick": "② 수량 개별선택"},
                                                value="pick" if _init_pick else "range",
                                            ).props("dense").classes("w-full")
                                            # ── 범위 모드 패널 ─────────────────────
                                            with ui.column().classes("gap-2") as _dlg_range_col:
                                                with ui.row().classes("items-center gap-2"):
                                                    _dlg_min = ui.number(
                                                        value=e_ref.min_qty or _q_sorted[0],
                                                        min=1, max=100, step=1, format="%.0f",
                                                    ).props("dense outlined").style("width:72px").tooltip("최솟값")
                                                    ui.label("개 ~").classes("font-bold text-sm")
                                                    _dlg_max = ui.number(
                                                        value=max(_q_sorted),
                                                        min=1, max=100, step=1, format="%.0f",
                                                    ).props("dense outlined").style("width:72px").tooltip("최댓값")
                                                    ui.label("개").classes("font-bold text-sm")
                                                _dlg_preview = ui.label("").classes("text-xs text-blue-400")
                                                def _upd_prev(ev=None, mn_i=_dlg_min, mx_i=_dlg_max, prev=_dlg_preview):
                                                    try:
                                                        _mn = max(1, min(100, int(mn_i.value or 1)))
                                                        _mx = max(_mn, min(100, int(mx_i.value or _mn)))
                                                    except Exception:
                                                        _mn, _mx = 1, 1
                                                    prev.set_text(f"→ {_mn}~{_mx}개  총 {_mx - _mn + 1}가지")
                                                _upd_prev()
                                                _dlg_min.on_value_change(_upd_prev)
                                                _dlg_max.on_value_change(_upd_prev)
                                            # ── 개별 선택 패널 ─────────────────────
                                            _dlg_checks: dict = {}
                                            _cur_picks_d = set(_q_sorted)
                                            with ui.column().classes("gap-1") as _dlg_pick_col:
                                                with ui.row().classes("items-center gap-2 mb-1"):
                                                    ui.label("원하는 수량 선택:").classes("text-xs text-slate-400")
                                                    def _sel_all_d(cks=_dlg_checks):
                                                        for cb in cks.values(): cb.set_value(True)
                                                    def _clr_all_d(cks=_dlg_checks):
                                                        for cb in cks.values(): cb.set_value(False)
                                                    ui.button("전체", on_click=_sel_all_d).props("flat dense size=xs color=teal")
                                                    ui.button("해제", on_click=_clr_all_d).props("flat dense size=xs color=grey")
                                                with ui.grid(columns=5).classes("gap-x-1 gap-y-0"):
                                                    for _qn in range(1, 31):
                                                        _ck = ui.checkbox(
                                                            f"{_qn}개",
                                                            value=(_qn in _cur_picks_d),
                                                        ).classes("text-xs")
                                                        _dlg_checks[_qn] = _ck
                                            # ── 모드 가시성 전환 ───────────────────
                                            def _on_dlg_mode_chg(ev=None, rc=_dlg_range_col, pc=_dlg_pick_col, tog=_dlg_mode):
                                                rc.set_visibility(tog.value == "range")
                                                pc.set_visibility(tog.value == "pick")
                                            _dlg_mode.on_value_change(_on_dlg_mode_chg)
                                            _on_dlg_mode_chg()
                                            # ── 적용 / 취소 ────────────────────────
                                            with ui.row().classes("gap-2 mt-2 justify-end w-full"):
                                                ui.button("취소", on_click=_dlg.close).props("flat dense color=grey")
                                                def _apply_qty(
                                                    tog=_dlg_mode, mn_i=_dlg_min, mx_i=_dlg_max,
                                                    cks=_dlg_checks, er=e_ref, lbl_ref=lbl, dlg=_dlg,
                                                ):
                                                    if tog.value == "range":
                                                        try:
                                                            _mn = max(1, min(100, int(mn_i.value or 1)))
                                                            _mx = max(_mn, min(100, int(mx_i.value or _mn)))
                                                        except Exception:
                                                            _mn, _mx = 1, 1
                                                        er.qtys = list(range(_mn, _mx + 1)) or [_mn]
                                                        er.min_qty = _mn
                                                        lbl_ref.set_text(f"📦 {_mn}~{_mx}개")
                                                    else:
                                                        _picked = sorted(q for q, cb in cks.items() if cb.value)
                                                        if not _picked:
                                                            ui.notify("최소 1개 이상 선택하세요.", type="warning")
                                                            return
                                                        er.qtys = _picked
                                                        er.min_qty = _picked[0]
                                                        _txt = (
                                                            ", ".join(str(q) for q in _picked[:6])
                                                            + ("…" if len(_picked) > 6 else "")
                                                            + "개"
                                                        )
                                                        lbl_ref.set_text(f"📦 {_txt}")
                                                    er.qty_locked = True
                                                    dlg.close()
                                                ui.button("✅ 적용", on_click=_apply_qty).props("color=teal dense")
                                        _dlg.open()
                                    return _open

                                ui.button("📦 수량 설정", on_click=_make_qty_dialog_opener()).props(
                                    "flat dense size=sm color=cyan"
                                ).classes("text-xs font-bold")

                                # 용량 — 엔진오일 카테고리일 때만 표시
                                _OIL_CATS = {"78889", "78903", "78894"}
                                _show_vol = entry.category_id in _OIL_CATS or entry.volume > 0
                                _vol_inp = ui.number(
                                    value=entry.volume or None,
                                    placeholder="용량",
                                    min=0, step=0.5,
                                ).props("dense outlined").style("width:70px")
                                _VUNIT_NORM = {"리터": "L", "밀리리터": "ml", "킬로그램": "kg", "그램": "g", "l": "L", "ML": "ml", "KG": "kg", "G": "g", "CC": "cc"}
                                _vunit_safe = _VUNIT_NORM.get(entry.volume_unit or "", entry.volume_unit or "L")
                                if _vunit_safe not in ["L", "ml", "cc", "kg", "g"]:
                                    _vunit_safe = "L"
                                _vunit_inp = ui.select(
                                    ["L", "ml", "cc", "kg", "g"],
                                    value=_vunit_safe,
                                ).props("dense outlined").style("width:60px")
                                if not _show_vol:
                                    _vol_inp.set_visibility(False)
                                    _vunit_inp.set_visibility(False)

                                def _make_vol_handler(e_ref=entry, vi=_vol_inp, vu=_vunit_inp):
                                    def _h(ev=None):
                                        e_ref.volume      = float(vi.value or 0)
                                        e_ref.volume_unit = vu.value or "L"
                                    return _h
                                _vol_inp.on("blur",  _make_vol_handler())
                                _vunit_inp.on_value_change(_make_vol_handler())


                            # ── 카테고리 자동감지 표시 + ID 입력 ─────────────
                            with ui.expansion(
                                "카테고리 설정", icon="category",
                            ).classes("w-full mt-1").props("dense"):
                                # 자동감지 결과 표시
                                if entry.detected_keyword:
                                    ui.label(
                                        f"감지 키워드: {entry.detected_keyword}  |  "
                                        f"고시: {entry.gosisi_cat[:30]}..."
                                    ).classes("text-xs text-teal-600 mb-1")
                                else:
                                    ui.label(
                                        "키워드 미감지 (처리 후 자동분류 시도)"
                                    ).classes("text-xs text-slate-400 mb-1")

                                with ui.row().classes("items-center gap-2 mt-1 flex-wrap"):
                                    _cat_inp = ui.input(
                                        value=entry.category_id,
                                        placeholder="카테고리 ID (예: 78889)",
                                    ).props("dense outlined").style("width:140px")

                                    def _make_cat_handler(
                                        e_ref=entry, inp=_cat_inp
                                    ):
                                        def _h(ev=None):
                                            e_ref.category_id = (inp.value or "").strip()
                                        return _h
                                    _cat_inp.on("blur", _make_cat_handler())

                                    # 계층 선택 버튼
                                    def _make_pending_sel(e_ref=entry):
                                        async def _h():
                                            try:
                                                await _open_cat_selector(e_ref)
                                            except Exception as _ex:
                                                import traceback as _tb
                                                print(f"[CatSel] 오류: {_ex}")
                                                _tb.print_exc()
                                                ui.notify(f"카테고리 선택 오류: {_ex}", type="negative")
                                        return _h
                                    ui.button(
                                        "🗂", on_click=_make_pending_sel(),
                                    ).props("flat dense size=sm color=teal").tooltip(
                                        "대분류 → 소분류 계층 선택"
                                    )

                                    # 저장 버튼: 키워드 감지됐을 때 → map에 영구저장
                                    if entry.detected_keyword:
                                        def _save_to_map(
                                            er=entry, ci=_cat_inp
                                        ):
                                            cat_id = (ci.value or "").strip()
                                            if not cat_id:
                                                ui.notify("카테고리 ID를 입력하세요", type="warning")
                                                return
                                            er.category_id = cat_id
                                            er.category_is_manual = True  # 수동 저장
                                            _sg = _guide_gosisi_cat(cat_id)
                                            if _sg:
                                                er.gosisi_cat = _sg
                                            if er.result_item:
                                                er.result_item.category_id = cat_id
                                                if _sg:
                                                    er.result_item.gosisi_cat = _sg
                                                _sv = _valid_option_types(cat_id)
                                                if _sv and er.result_item.extra_options:
                                                    er.result_item.extra_options = [
                                                        (t, v) for t, v in er.result_item.extra_options
                                                        if t in _sv
                                                    ]
                                            _get_detector().update(
                                                keyword=er.detected_keyword,
                                                category_id=cat_id,
                                                gosisi_cat=er.gosisi_cat,
                                            )
                                            ui.notify(
                                                f"'{er.detected_keyword}' → ID {cat_id} 영구저장!",
                                                type="positive", timeout=3000,
                                            )
                                        ui.button(
                                            "ID 저장", icon="save",
                                            on_click=_save_to_map,
                                        ).props("flat dense size=sm color=teal").tooltip(
                                            "이 키워드의 카테고리 ID를 영구저장 (다음엔 자동입력)"
                                        )

                            # ── 수동 옵션 추가 섹션 (단계 3) ────────────────
                            with ui.expansion(
                                "추가 옵션 직접 지정", icon="tune",
                            ).classes("w-full mt-1").props("dense"):
                                ui.label(
                                    "자동추출 외 옵션을 직접 입력 (색상·사이즈 등)"
                                ).classes("text-xs text-slate-400 mb-1")

                                # 현재 수동 옵션 목록 표시
                                _opt_list_col = ui.column().classes("gap-1 w-full mb-2")

                                def _render_opt_list(e_ref=entry, col=_opt_list_col):
                                    col.clear()
                                    with col:
                                        if not e_ref.manual_options:
                                            ui.label("(없음)").classes(
                                                "text-xs text-slate-300 italic"
                                            )
                                        for _i, (_ot, _ov) in enumerate(e_ref.manual_options):
                                            with ui.row().classes("items-center gap-1"):
                                                ui.chip(
                                                    f"{_ot}: {_ov}",
                                                    icon="label",
                                                ).props("dense color=blue-1 text-color=blue-8")
                                                def _del_opt(idx=_i, er=e_ref):
                                                    er.manual_options.pop(idx)
                                                    _render_queue()
                                                ui.button(
                                                    icon="close", on_click=_del_opt,
                                                ).props("flat dense size=xs color=red")

                                _render_opt_list()

                                # 옵션 입력 행
                                _OPT_TYPES = [
                                    "색상", "사이즈", "중량", "개당 용량",
                                    "향", "옵션명", "엔진오일 SAE점도",
                                    "용량", "두께", "재질",
                                ]
                                with ui.row().classes("items-center gap-1 flex-wrap"):
                                    _otype_sel = ui.select(
                                        _OPT_TYPES, label="옵션 타입",
                                        new_value_mode="add",
                                    ).props("dense outlined").style("width:160px")
                                    _oval_inp = ui.input(
                                        placeholder="값 (예: 블랙, M, 500g)",
                                    ).props("dense outlined").style("width:130px")

                                    def _add_manual_opt(
                                        er=entry, ts=_otype_sel, vi=_oval_inp
                                    ):
                                        t = (ts.value or "").strip()
                                        v = (vi.value or "").strip()
                                        if t and v:
                                            er.manual_options.append((t, v))
                                            _render_queue()
                                        else:
                                            ui.notify(
                                                "옵션 타입과 값을 모두 입력하세요.",
                                                type="warning", timeout=2000,
                                            )

                                    ui.button(
                                        icon="add", on_click=_add_manual_opt,
                                    ).props("flat dense color=blue").tooltip("옵션 추가")

    # ══════════════════════════════════════════════════════════════
    # 내부 함수: 큐 조작
    # ══════════════════════════════════════════════════════════════

    def _add_entry(url: str, brand: str = ""):
        url = url.strip()
        if not url:
            ui.notify("URL을 입력하세요.", type="warning")
            return
        if not (url.startswith("http://") or url.startswith("https://")):
            ui.notify(f"올바른 URL이 아닙니다 (https://... 형식으로 입력)", type="warning")
            return
        if "smartstore.naver.com" not in url:
            ui.notify("네이버 SmartStore URL만 지원합니다 (smartstore.naver.com)", type="warning")
            return
        if _is_duplicate_url(url, queue):
            ui.notify("이미 추가된 URL입니다 (같은 상품 ID).", type="warning")
            return

        # 기본 설정에서 수량 읽기 (모드에 따라 분기)
        if qty_mode.value == "range":
            try:
                _mn = max(1, min(100, int(qty_min_input.value or 1)))
                _mx = max(_mn, min(100, int(qty_max_input.value or 12)))
            except Exception:
                _mn, _mx = 1, 12
            default_qtys = list(range(_mn, _mx + 1)) or [_mn]
            default_min  = _mn
        else:
            default_qtys = sorted(q for q, cb in _qty_checks.items() if cb.value)
            if not default_qtys:
                default_qtys = [1, 2, 3]
            default_min = default_qtys[0] if default_qtys else 1

        _brand_str = brand.strip()
        entry = QueueEntry(
            uid          = uuid.uuid4().hex[:8],
            url          = url,
            brand        = _brand_str,
            brand_locked = bool(_brand_str),  # URL 추가 시 브랜드 입력했으면 잠금
            qtys         = default_qtys,
            min_qty      = default_min,
            volume       = float(default_vol.value or 0),
            volume_unit  = default_vol_unit.value or "L",
            gosisi_cat   = "기타 재화",
            use_nobg     = nobg_toggle.value == "on",
            lead_time    = 10 if shipping_mode.value == "overseas" else 2,
            watch_store  = _main_store_sel.value or "샵케이",
        )
        queue.append(entry)
        new_url_input.set_value("")
        new_brand_input.set_value("")
        _render_queue()
        asyncio.ensure_future(ui.run_javascript(_DING_JS))
        ui.notify(f"추가됨: {url[:50]}...", type="positive", timeout=2000)

    def _remove_entry(uid: str):
        idx = next((i for i, e in enumerate(queue) if e.uid == uid), None)
        if idx is not None:
            queue.pop(idx)
            _render_queue()

    def _clear_queue():
        queue.clear()
        _render_queue()
        dl_card.set_visibility(False)
        _output_file["v"] = ""
        _global_output_file["v"] = ""   # 큐 초기화 시 글로벌 파일 참조도 제거
        _backup_done["v"] = False
        _set_beforeunload(False)
        # 큐 초기화 시 저장된 상태파일도 삭제 (재시작해도 복원 안 되도록)
        try:
            if _QUEUE_STATE_FILE.exists():
                _QUEUE_STATE_FILE.unlink()
        except Exception:
            pass

    add_btn.on_click(lambda: _add_entry(new_url_input.value, new_brand_input.value))
    new_url_input.on("keydown.enter", lambda: _add_entry(new_url_input.value, new_brand_input.value))

    # ══════════════════════════════════════════════════════════════
    # 내부 함수: 전체 처리 실행
    # ══════════════════════════════════════════════════════════════

    _running = _global_running   # 전역 참조

    # ── last_batch.json 로드 (앱 시작 시 1회) ────────────────────────
    if not _last_batch_meta.get("v"):
        try:
            _lb_path = Path(__file__).parent / "data" / "last_batch.json"
            if _lb_path.exists():
                import json as _json_lb
                _last_batch_meta["v"] = _json_lb.loads(_lb_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    async def _on_run():
        global _global_task
        if _global_running["v"]:
            ui.notify("이미 실행 중입니다.", type="warning")
            return
        pending = [e for e in queue if e.status == "pending"]
        if not pending:
            ui.notify("처리할 항목이 없습니다. URL을 추가하거나 상태를 확인하세요.", type="warning")
            return

        margin_rate = float(margin_rate_input.value or 1.35)

        # ── 세션 세팅 즉시 저장 (에러 여부와 무관하게 수집 직전 확정) ────
        # queue 전체 + UI 설정값 포함 저장 → 재수집 시 완벽 복원.
        _save_collection_history(
            list(queue),
            shipping_mode=shipping_mode.value,
            margin_rate=margin_rate,
        )
        _render_history()  # 히스토리 UI 즉시 갱신
        lead_time   = 10 if shipping_mode.value == "overseas" else 2
        use_nobg    = nobg_toggle.value == "on"

        # ── 배치 수량 정보 저장 (재작업 복원용) ──────────────────────
        # 현재 큐의 URL별 수량을 last_batch.json에 저장.
        # 다음에 같은 메모장 파일을 업로드하면 수량을 자동 복원 제안.
        try:
            _batch_file = Path(__file__).parent / "data" / "last_batch.json"
            _batch_data = _last_batch_meta.get("v") or {}
            # 이번 업로드 파일의 식별 정보를 url_qtys와 함께 저장 (항상 동기화)
            _pend = _pending_batch_meta.get("v") or {}
            if _pend.get("filename"):
                _batch_data["filename"] = _pend["filename"]
                _batch_data["filesize"] = _pend["filesize"]
            _url_qtys: dict = {}
            for _e in queue:
                _url_qtys[_e.url] = {
                    "min_qty":     _e.min_qty,
                    "max_qty":     max(_e.qtys) if _e.qtys else 1,
                    "volume":      _e.volume,
                    "volume_unit": _e.volume_unit,
                    "brand":       _e.brand,
                }
            _batch_data["url_qtys"] = _url_qtys
            _last_batch_meta["v"] = _batch_data  # 메모리도 갱신
            import json as _json2
            _batch_file.write_text(
                _json2.dumps(_batch_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as _be:
            print(f"[배치저장] 오류 (무시): {_be}")

        # ── 큐 상태 즉시 저장 (재시작 복원용) ───────────────────────
        _persist_queue()

        _global_log_buffer.clear()
        _global_task = asyncio.create_task(
            _run_global_processing(margin_rate, lead_time, use_nobg)
        )
        ui.notify(
            "🚀 처리 시작 — 다른 페이지로 이동해도 계속 실행됩니다",
            type="info", timeout=4000,
        )

    run_btn.on_click(_on_run)

    # ── UI 폴링 타이머 (1초마다 전역 상태 반영) ───────────────────
    _log_last_idx    = {"v": 0}
    _prev_snapshot   = {"v": ""}
    _prev_done       = {"v": bool(_global_output_file["v"])}  # 이미 완료된 경우 포함
    _persist_tick    = {"v": 0}   # 큐 상태 저장 카운터 (15초마다)

    def _tick():
        is_running = _global_running["v"]

        # ── 새 로그 라인 → log_widget 추가 (처리 중일 때만 실시간 반영) ──
        new_lines = _global_log_buffer[_log_last_idx["v"]:]
        if new_lines:
            # 한 번에 최대 20줄만 push (탭 복귀 후 수백 줄 한꺼번에 터지는 것 방지)
            for line in new_lines[:20]:
                try:
                    log_widget.push(line)
                except Exception:
                    pass
            _log_last_idx["v"] = _log_last_idx["v"] + min(len(new_lines), 20)

        # ── 큐 상태 변경 시만 재렌더링 ──────────────────────────
        snapshot = ",".join(f"{e.uid}:{e.status}" for e in _global_queue)
        if snapshot != _prev_snapshot["v"]:
            _render_queue()
            _prev_snapshot["v"] = snapshot

        # ── 큐 상태 자동저장 (15초마다) ──────────────────────────
        _persist_tick["v"] += 1
        if _persist_tick["v"] >= 15 and _global_queue:
            _persist_tick["v"] = 0
            _persist_queue()

        # ── 실행 중 여부 UI ──────────────────────────────────────
        is_running = _global_running["v"]
        try:
            run_btn.set_enabled(not is_running)
            stop_btn.set_visibility(is_running)
            stop_btn.set_enabled(is_running and not _global_stop_req["v"])
            run_spinner.set_visibility(is_running)
            progress_bar.set_visibility(is_running)
            if is_running:
                done_n    = sum(1 for e in _global_queue if e.status == "done")
                total_n   = len(_global_queue)
                pending_n = sum(1 for e in _global_queue if e.status == "pending")
                proc_name = next(
                    (e.product_name or e.url[:40]
                     for e in _global_queue if e.status == "processing"),
                    ""
                )
                # ETA 계산 (완료 2개 이상부터 표시)
                _eta_txt = ""
                _itimes = _global_timing["item_times"]
                if len(_itimes) >= 2 and pending_n > 0:
                    _avg = sum(_itimes) / len(_itimes)
                    # 현재 처리 중인 항목의 경과시간 반영
                    _cur_elapsed = 0.0
                    if _global_timing["item_start"] > 0:
                        import time as _tm
                        _cur_elapsed = _tm.perf_counter() - _global_timing["item_start"]
                    _cur_remain = max(0.0, _avg - _cur_elapsed)
                    _eta_secs = int(_cur_remain + _avg * pending_n)
                    if _eta_secs >= 3600:
                        _eta_txt = f"  ⏱ 약 {_eta_secs // 3600}시간 {(_eta_secs % 3600) // 60}분 남음"
                    elif _eta_secs >= 60:
                        _eta_txt = f"  ⏱ 약 {_eta_secs // 60}분 {_eta_secs % 60}초 남음"
                    else:
                        _eta_txt = f"  ⏱ 약 {_eta_secs}초 남음"
                elif pending_n > 0 and _global_timing["item_start"] > 0:
                    _eta_txt = "  ⏱ 계산 중..."
                progress_lbl.set_text(
                    f"처리 중: {done_n}/{total_n - pending_n}  {proc_name[:35]}...{_eta_txt}"
                )
                progress_bar.set_value(done_n / max(total_n, 1))

            # ── 오류 네비게이션 버튼 실시간 업데이트 ──────────────
            _err_entries = [e for e in _global_queue if e.status == "error"]
            _crit_entries = [
                e for e in _global_queue
                if e.status == "done" and any(
                    i["severity"] == "critical" for i in _excel_issues(e)
                )
            ]
            _nav_targets = _err_entries + _crit_entries
            error_nav_row.set_visibility(bool(_nav_targets))
            error_nav_row.clear()
            with error_nav_row:
                if _nav_targets:
                    ui.label(f"⚠ 오류 {len(_nav_targets)}개:").classes(
                        "text-xs text-red-400 font-bold"
                    )
                    for _ni, _ne in enumerate(_nav_targets[:5], 1):
                        _nav_label = f"오류{_ni}"
                        _nav_uid = _ne.uid
                        ui.button(
                            _nav_label,
                            on_click=lambda uid=_nav_uid: ui.run_javascript(
                                f"document.getElementById('qcard-{uid}')"
                                f"?.scrollIntoView({{behavior:'smooth',block:'center'}})"
                            )
                        ).props("flat dense size=xs color=red")
                    # ── 오류 전체 삭제 버튼 ─────────────────────────────
                    _err_uids_snap = [e.uid for e in _nav_targets]
                    def _delete_all_errors(uids=_err_uids_snap):
                        removed = 0
                        for uid in uids:
                            idx = next((i for i, e in enumerate(queue) if e.uid == uid), None)
                            if idx is not None:
                                queue.pop(idx)
                                removed += 1
                        if removed:
                            _render_queue()
                            ui.notify(f"오류 {removed}개 삭제됨", type="warning", timeout=2000)
                    ui.button(
                        "🗑 전체 삭제",
                        on_click=_delete_all_errors,
                    ).props("flat dense size=xs color=orange").tooltip("오류 항목 전체 삭제 후 엑셀 다운로드 가능")
        except Exception:
            pass

        # ── 처리 완료 → 다운로드 카드 표시 + 브라우저 알림 ─────
        # _should_show_dl: 실행 중이 아니고 출력 파일이 있으면 항상 표시
        # (기존 just_done 방식은 _global_running이 True인 채로 tick이 두 번 이상 실행되면
        #  _prev_done["v"]가 먼저 True로 굳어져서 dl_card가 영원히 안 뜨는 race condition 존재)
        _should_show_dl = not is_running and bool(_global_output_file["v"])

        try:
            # dl_card 가시성을 항상 정확히 동기화 (race condition 무관)
            dl_card.set_visibility(_should_show_dl)
            # 로컬 _output_file도 글로벌 값과 동기화 (다운로드 버튼이 올바른 파일명 사용)
            if _should_show_dl and not _output_file["v"]:
                _output_file["v"] = _global_output_file["v"]
        except Exception:
            pass

        # just_done: 완료 첫 감지 시에만 자막·알림 업데이트 (1회성)
        just_done = _should_show_dl and not _prev_done["v"]
        if just_done:
            try:
                _render_history()
            except Exception:
                pass
            success_items = [e.result_item for e in _global_queue if e.result_item]
            try:
                _done_entries = [e for e in _global_queue if e.status == "done"]
                _crit_cnt = sum(
                    1 for e in _done_entries
                    for i in _excel_issues(e)
                    if i["severity"] == "critical"
                )
                _warn_cnt = sum(
                    1 for e in _done_entries
                    for i in _excel_issues(e)
                    if i["severity"] == "warning"
                )
                _issue_txt = ""
                if _crit_cnt:
                    _issue_txt += f"  ❌ 필수오류 {_crit_cnt}개"
                if _warn_cnt:
                    _issue_txt += f"  ⚠ 경고 {_warn_cnt}개"
                dl_subtitle.set_text(
                    f"{len(success_items)}개 상품 / "
                    f"{sum(len(item.bundles) for item in success_items)}개 묶음 행"
                    f" → {_global_output_file['v']}"
                )
                dl_issue_lbl.set_text(
                    _issue_txt + "  →  각 카드에서 수정 후 다운로드하세요" if _issue_txt else ""
                )
                _backup_done["v"] = False
                _set_beforeunload(True)
                progress_lbl.set_text(f"✅ 완료: {len(success_items)}개 처리 성공")
            except Exception:
                pass
            try:
                ui.notify(f"엑셀 생성 완료! {len(success_items)}개 상품", type="positive", timeout=5000)
                ui.run_javascript(
                    f"window._notifyDone('엑셀 생성 완료 — {len(success_items)}개 상품')"
                )
            except Exception:
                pass

        _prev_done["v"] = _should_show_dl

    # ── Wing 자동 판매요청 핸들러 ─────────────────────────────────

    async def _on_wing_publish(store: str | None = None):
        """
        Wing 임시저장 목록에서 draft=False 상품들에 판매요청 클릭.
        store 지정 시 해당 스토어만 실행. None이면 큐 기반 자동 결정.
        """
        # 스토어별 계정 매핑
        _STORE_CREDS = {
            "샵케이": (
                getattr(_settings, "WING_USERNAME", ""),
                getattr(_settings, "WING_PASSWORD", ""),
            ),
            "제니스 트레이딩": (
                getattr(_settings, "WING_USERNAME_ZENITH", ""),
                getattr(_settings, "WING_PASSWORD_ZENITH", ""),
            ),
        }

        if store is not None:
            # 버튼으로 직접 지정된 스토어만 실행
            _stores_in_queue = [store]
        else:
            # 큐에 있는 done 항목의 스토어 목록 수집
            _stores_in_queue = list(dict.fromkeys(
                getattr(e, "watch_store", "샵케이")
                for e in queue if e.status == "done" and e.result_item
            ))
            if not _stores_in_queue:
                _stores_in_queue = [
                    _st for _st, (_u, _p) in _STORE_CREDS.items() if _u and _p
                ]

        if not _stores_in_queue:
            ui.notify("Wing 계정 정보가 없습니다 — .env에서 WING_USERNAME/WING_PASSWORD 확인", type="negative", timeout=5000)
            return

        # 계정 누락 스토어 제거
        _stores_in_queue = [
            _st for _st in _stores_in_queue
            if _STORE_CREDS.get(_st, ("", ""))[0] and _STORE_CREDS.get(_st, ("", ""))[1]
        ]
        if not _stores_in_queue:
            ui.notify("Wing 계정 정보 누락 — .env 확인", type="negative", timeout=5000)
            return

        # draft=True 상품 목록
        draft_names: list[str] = [
            e.result_item.product_name
            for e in queue
            if e.result_item and e.result_item.draft
        ]

        # 상품별 데이터 수집 (GTIN, 번들 정보 → Wing 속성 자동 입력용)
        _product_data: dict = {}
        for _e in queue:
            if _e.result_item and _e.status == "done":
                _pname = _e.result_item.product_name or _e.product_name or ""
                if not _pname:
                    continue
                _bundles_info = {}
                for _b in (_e.result_item.bundles or []):
                    _bundles_info[_b.qty] = {"weight": ""}
                _weight_str = ""
                if _e.volume and _e.volume > 0:
                    _weight_str = f"{_e.volume:g}{_e.volume_unit}"
                _product_data[_pname] = {
                    "gtin": _e.gtin or _e.result_item.gtin or "",
                    "bundles": _bundles_info,
                    "weight": _weight_str,
                }

        if _global_wing_running["v"]:
            ui.notify("Wing 자동화가 이미 실행 중입니다.", type="warning", timeout=4000)
            return

        _global_wing_running["v"] = True
        wing_pub_btn_shopk.set_enabled(False)
        wing_pub_btn_zenith.set_enabled(False)
        _store_label = " + ".join(_stores_in_queue)
        wing_pub_status.set_text(f"⏳ Wing 실행 중 ({_store_label})...")
        ui.notify(f"Wing 판매요청 시작 — {_store_label}", type="info", timeout=4000)

        _progress_state = {"current": 0, "total": 0}

        def _log_cb(msg: str):
            _global_wing_log_buffer.append(msg)
            try:
                wing_log_widget.push(msg)
                cur, tot = _progress_state["current"], _progress_state["total"]
                if tot > 0:
                    wing_pub_status.set_text(f"[{cur}/{tot}] {msg[-50:] if len(msg) > 50 else msg}")
                else:
                    wing_pub_status.set_text(msg[-60:] if len(msg) > 60 else msg)
            except Exception:
                pass  # 페이지 이동으로 UI 삭제된 경우 무시

        def _progress_cb(current: int, total: int):
            _progress_state["current"] = current
            _progress_state["total"] = total
            try:
                if total > 0:
                    wing_pub_status.set_text(f"[{current}/{total}] 처리 중...")
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        _total_pub = 0; _total_skip = 0; _total_errs: list = []; _all_pub_names: list = []
        try:
            for _store in _stores_in_queue:
                _u, _p = _STORE_CREDS[_store]
                _log_cb(f"── [{_store}] Wing 판매요청 시작 ──")
                wing_pub_status.set_text(f"⏳ [{_store}] Wing 실행 중...")
                _result = await loop.run_in_executor(
                    None,
                    lambda u=_u, p=_p: _run_bulk_publish(
                        username=u,
                        password=p,
                        skip_names=draft_names,
                        log_cb=_log_cb,
                        progress_cb=_progress_cb,
                        product_data=_product_data,
                        headless=True,
                        gemini_api_key=getattr(_settings, "GEMINI_API_KEY", ""),
                    ),
                )
                _total_pub  += _result.get("published", 0)
                _total_skip += _result.get("skipped", 0)
                _total_errs += _result.get("errors", [])
                _all_pub_names += _result.get("published_names", [])
                _store_errs = _result.get("errors", [])
                _err_suffix = f" / ⚠️ 오류: {', '.join(str(e) for e in _store_errs[:2])}" if _store_errs else ""
                _log_cb(f"── [{_store}] 완료: {_result.get('published',0)}개 요청 / {_result.get('skipped',0)}개 건너뜀{_err_suffix} ──")

            msg = f"✅ 판매요청 완료: {_total_pub}개 요청 / {_total_skip}개 건너뜀"
            if _total_errs:
                msg += f" / ⚠️ {len(_total_errs)}건 오류"
            wing_pub_status.set_text(msg)
            ui.notify(msg, type="positive" if not _total_errs else "warning", timeout=6000)
            ui.run_javascript(f"window._notifyDone('Wing 판매요청 완료 — {_total_pub}개')")
            tg_msg = f"✅ <b>[자동등록 완료]</b>\n판매요청: {_total_pub}개 / 건너뜀: {_total_skip}개"
            if _total_errs:
                tg_msg += f"\n⚠️ 오류 {len(_total_errs)}건: {', '.join(str(e) for e in _total_errs[:3])}"
            _send_notification(tg_msg)

            # ── 판매요청 성공 상품을 이력 파일에 저장 ────────────────────
            if _all_pub_names:
                _saved_cnt = 0
                for _e in queue:
                    _ename = (_e.result_item.product_name if _e.result_item else _e.product_name) or ""
                    if any(_pn and (_pn[:30] in _ename or _ename[:30] in _pn) for _pn in _all_pub_names):
                        _vol_str = f"{_e.volume:g}{_e.volume_unit}" if _e.volume else ""
                        try:
                            _add_to_history(
                                brand        = _e.brand or "",
                                product_name = _ename[:50],
                                volume       = _vol_str,
                                category_id  = _e.category_id or "",
                                naver_url    = _e.url,
                                gtin         = _e.gtin or "",
                            )
                            _saved_cnt += 1
                        except Exception as _he:
                            print(f"[이력저장] 오류: {_he}")
                if _saved_cnt:
                    ui.notify(f"📋 등록 이력 {_saved_cnt}개 저장됨", type="info", timeout=3000)
        except Exception as exc:
            wing_pub_status.set_text(f"오류: {exc}")
            ui.notify(f"Wing 판매요청 오류: {exc}", type="negative", timeout=6000)
        finally:
            _global_wing_running["v"] = False
            wing_pub_btn_shopk.set_enabled(True)
            wing_pub_btn_zenith.set_enabled(True)

    # 초기 렌더링 + 기존 로그 복원
    _render_queue()
    for line in _global_log_buffer:
        try:
            log_widget.push(line)
        except Exception:
            pass
    _log_last_idx["v"] = len(_global_log_buffer)
    for line in _global_wing_log_buffer:
        try:
            wing_log_widget.push(line)
        except Exception:
            pass

    # 처리 중: 1초마다 폴링 / 유휴: 3초마다 (WebSocket 부하 감소)
    _tick_timer = ui.timer(1.0, _tick)

    def _adjust_tick_interval():
        """처리 중이면 1초, 유휴이면 3초로 자동 전환"""
        is_running = _global_running["v"]
        has_pending = any(e.status == "pending" for e in _global_queue)
        target = 1.0 if (is_running or has_pending) else 3.0
        if abs(_tick_timer.interval - target) > 0.1:
            _tick_timer.interval = target

    ui.timer(5.0, _adjust_tick_interval)


# ── 앱 생애주기 ───────────────────────────────────────────────────

def _cleanup_output(max_age_hours: int = 24) -> None:
    """output 폴더에서 max_age_hours 시간 이상 된 엑셀 파일 자동 삭제."""
    import time
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    for f in _OUTPUT_ROOT.glob("*.xls*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception:
            pass
    if deleted:
        print(f"[Cleanup] output 폴더 정리: {deleted}개 삭제 ({max_age_hours}시간 초과)")


@_app.on_startup
async def _on_startup():
    def _check():
        if getattr(_settings, "USE_SOCKS5", False) and _ensure_ssh_tunnel():
            print("[Startup] SSH 터널 연결 성공")
        print("[Startup] 쿠팡 일괄등록 시스템 준비 완료")
        _cleanup_output(24)   # 시작 시 1회 즉시 청소
    asyncio.get_running_loop().run_in_executor(None, _check)

    # ── 가격 모니터링 스케줄러 시작 ─────────────────────────────
    asyncio.create_task(_pc.run_scheduler())

    # ── output 폴더 주기적 청소 (24시간마다) ────────────────────
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(24 * 3600)
            await asyncio.get_running_loop().run_in_executor(None, lambda: _cleanup_output(24))
    asyncio.create_task(_periodic_cleanup())

    # ── 텔레그램 [▶ 자동등록 시작] 버튼 폴링 ────────────────────
    async def _telegram_register_poll():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(10)
            try:
                triggered = await loop.run_in_executor(None, _poll_register_callback)
                if not triggered:
                    continue

                # 크롤링 or Wing 자동화가 이미 실행 중이면 무시
                if _global_running["v"] or _global_wing_running["v"]:
                    _send_notification("⚠️ 이미 처리 중입니다. 완료 후 다시 시도하세요.")
                    continue

                done_entries = [e for e in _global_queue if e.status == "done"]
                if not done_entries:
                    _send_notification("⚠️ 등록할 완료 상품이 없습니다. 먼저 수집을 실행하세요.")
                    continue

                username = getattr(_settings, "WING_USERNAME", "")
                password = getattr(_settings, "WING_PASSWORD", "")
                if not username or not password:
                    _send_notification("❌ Wing 계정 정보 없음 — .env에 WING_USERNAME / WING_PASSWORD 확인")
                    continue

                # 세션 파일 없으면 headless 로그인 불가 → 안내 후 중단
                _session_file = Path(__file__).parent / "data" / "wing_session.json"
                if not _session_file.exists():
                    _send_notification(
                        "⚠️ <b>Wing 로그인 세션 없음</b>\n\n"
                        "텔레그램 자동등록은 저장된 로그인 세션이 필요합니다.\n"
                        "PC 앱에서 🚀 판매요청 시작 버튼을 한 번 눌러 로그인하면\n"
                        "이후부터 텔레그램 버튼으로 자동 실행됩니다."
                    )
                    continue

                _global_wing_running["v"] = True
                _send_notification("⏳ 자동등록 시작 중... 완료 시 결과를 알려드립니다.")
                print("[TG-폴링] 텔레그램 버튼으로 자동등록 시작")

                # draft=True 상품은 skip (result_item.draft 기준 — UI와 동일 로직)
                draft_names = []
                for e in _global_queue:
                    if e.result_item and e.result_item.draft:
                        name = e.result_item.product_name or ""
                        if name:
                            draft_names.append(name)

                # product_data: gtin + bundles + weight 포함 (UI _on_wing_publish와 동일)
                product_data = {}
                for e in _global_queue:
                    if e.status != "done" or not e.result_item:
                        continue
                    pname = e.result_item.product_name or e.product_name or ""
                    if not pname:
                        continue
                    bundles_info = {}
                    for b in (e.result_item.bundles or []):
                        bundles_info[b.qty] = {"weight": ""}
                    weight_str = ""
                    if e.volume and e.volume > 0:
                        weight_str = f"{e.volume:g}{e.volume_unit}"
                    product_data[pname] = {
                        "gtin": e.gtin or e.result_item.gtin or "",
                        "bundles": bundles_info,
                        "weight": weight_str,
                    }

                try:
                    result = await loop.run_in_executor(
                        None,
                        lambda: _run_bulk_publish(
                            username=username,
                            password=password,
                            skip_names=draft_names,
                            log_cb=lambda msg: print(f"[Wing-TG] {msg}"),
                            progress_cb=None,
                            product_data=product_data,
                            headless=True,
                            gemini_api_key=getattr(_settings, "GEMINI_API_KEY", ""),
                        ),
                    )
                    pub  = result.get("published", 0)
                    skip = result.get("skipped", 0)
                    errs = result.get("errors", [])
                    msg  = f"✅ <b>[자동등록 완료]</b>\n판매요청: {pub}개 / 건너뜀: {skip}개"
                    if errs:
                        msg += f"\n⚠️ 오류 {len(errs)}건: {', '.join(str(e) for e in errs[:3])}"
                    _send_notification(msg)
                    print(f"[TG-폴링] 자동등록 완료: {pub}개")

                    # 이력 저장
                    pub_names = result.get("published_names", [])
                    for e in _global_queue:
                        ename = (e.result_item.product_name if e.result_item else e.product_name) or ""
                        if any(_pn and (_pn[:30] in ename or ename[:30] in _pn) for _pn in pub_names):
                            vol_str = f"{e.volume:g}{e.volume_unit}" if e.volume else ""
                            try:
                                _add_to_history(
                                    brand=e.brand or "",
                                    product_name=ename[:50],
                                    volume=vol_str,
                                    category_id=e.category_id or "",
                                    naver_url=e.url,
                                    gtin=e.gtin or "",
                                )
                            except Exception:
                                pass

                except Exception as exc:
                    _send_notification(f"❌ 자동등록 오류: {exc}")
                    print(f"[TG-폴링] 자동등록 오류: {exc}")
                finally:
                    _global_wing_running["v"] = False

            except Exception as ex:
                print(f"[TG-폴링] 루프 오류 (무시): {ex}")

    asyncio.create_task(_telegram_register_poll())


@_app.on_shutdown
async def _on_shutdown():
    global _ssh_proc
    if _ssh_proc and _ssh_proc.poll() is None:
        _ssh_proc.terminate()
        _ssh_proc = None


# ── 앱 실행 ──────────────────────────────────────────────────────
ui.run(
    title="네이버 → 쿠팡 일괄등록",
    host="0.0.0.0",
    port=8080,
    reload=False,
    show=False,
    favicon="🛒",
    dark=False,
    uvicorn_logging_level="warning",
    reconnect_timeout=300,     # 30초 → 300초 (5분): 수집 중 일시 끊겨도 세션 복원 보장
    ws_ping_interval=60,       # 기본 20초 → 60초: CPU 스파이크 중 ping 실패 방지
    ws_ping_timeout=120,       # 기본 30초 → 120초: nodriver Chrome 구동 중 ping 지연 허용
)
