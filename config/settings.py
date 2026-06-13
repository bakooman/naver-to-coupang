import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)   # .env 값이 OS 환경변수보다 항상 우선 (부트스크립트 충돌 방지)

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    # ── 디렉토리 ─────────────────────────────────────────
    DATA_DIR            = str(BASE_DIR / "data")
    IMAGE_ORIGINAL_DIR  = str(BASE_DIR / "data" / "images" / "original")
    IMAGE_NOBG_DIR      = str(BASE_DIR / "data" / "images" / "nobg")
    IMAGE_COMPOSED_DIR  = str(BASE_DIR / "data" / "images" / "composed")
    OUTPUT_DIR          = str(BASE_DIR / "data" / "output")

    # ── 이미지 캔버스 ─────────────────────────────────────
    CANVAS_WIDTH  = 800
    CANVAS_HEIGHT = 800
    FONT_SIZE     = 52   # 수량 텍스트 pt

    # ── 마진 ─────────────────────────────────────────────
    # 환경변수 MARGIN_RATE 미설정 시 기본 30% 마진
    MARGIN_RATE = float(os.getenv("MARGIN_RATE", "1.3"))

    # ── 쿠팡 API ─────────────────────────────────────────
    COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY", "")
    COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY", "")
    COUPANG_VENDOR_ID  = os.getenv("COUPANG_VENDOR_ID", "")

    # ── 쿠팡 판매자 계정 정보 ────────────────────────────
    VENDOR_USER_ID               = os.getenv("VENDOR_USER_ID", "")
    OUTBOUND_SHIPPING_PLACE_CODE = int(os.getenv("OUTBOUND_SHIPPING_PLACE_CODE", "0") or 0)
    RETURN_CENTER_CODE           = os.getenv("RETURN_CENTER_CODE", "")
    RETURN_CHARGE_NAME           = os.getenv("RETURN_CHARGE_NAME", "반품")
    RETURN_CHARGE                = int(os.getenv("RETURN_CHARGE", "7000") or 7000)
    RETURN_ZIP_CODE              = os.getenv("RETURN_ZIP_CODE", "")
    RETURN_ADDRESS               = os.getenv("RETURN_ADDRESS", "")
    RETURN_ADDRESS_DETAIL        = os.getenv("RETURN_ADDRESS_DETAIL", "")
    COMPANY_CONTACT_NUMBER       = os.getenv("COMPANY_CONTACT_NUMBER", "")
    DELIVERY_COMPANY_CODE        = os.getenv("DELIVERY_COMPANY_CODE", "CJGLS")

    # ── Wing 브라우저 자동화 계정 ─────────────────────────
    WING_USERNAME = os.getenv("WING_USERNAME", "")
    WING_PASSWORD = os.getenv("WING_PASSWORD", "")
    WING_USERNAME_ZENITH = os.getenv("WING_USERNAME_ZENITH", "")
    WING_PASSWORD_ZENITH = os.getenv("WING_PASSWORD_ZENITH", "")

    # ── Google Gemini API ─────────────────────────────────
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # ── Cloudflare R2 이미지 스토리지 ────────────────────────
    R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")          # Cloudflare Account ID
    R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")       # R2 API 토큰 Access Key ID
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")   # R2 API 토큰 Secret Key
    R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "coupang-images")
    R2_PUBLIC_URL        = os.getenv("R2_PUBLIC_URL", "")          # pub-xxxx.r2.dev URL

    # ── 네이버 크롤링 전용 Residential Proxy (구 방식, 미사용 시 빈값) ──
    RESIDENTIAL_PROXY_URL = os.getenv("RESIDENTIAL_PROXY_URL", "")

    # ── Bright Data Web Unlocker API (신규, 권장) ─────────────────
    # https://api.brightdata.com/request  POST 방식으로 WAF 우회
    BRIGHTDATA_API_KEY = os.getenv("BRIGHTDATA_API_KEY", "")

    # ── VPS SOCKS5 프록시 (쿠팡 API 전용) ────────────────
    # SSH 터널: ssh -N -D 1080 -i <key> ubuntu@<VPS_HOST>
    VPS_HOST         = os.getenv("VPS_HOST", "1.201.123.110")
    VPS_SSH_KEY_PATH = os.getenv("VPS_SSH_KEY_PATH", "")   # .pem 절대경로
    SOCKS5_PORT      = int(os.getenv("SOCKS5_PORT", "1080"))
    # USE_SOCKS5=true 이면 Coupang API 요청을 터널로 라우팅
    USE_SOCKS5       = os.getenv("USE_SOCKS5", "true").lower() == "true"

    @classmethod
    def socks5_proxies(cls) -> dict | None:
        """requests 의 proxies 인자용 dict. USE_SOCKS5=false 면 None 반환."""
        if not cls.USE_SOCKS5:
            return None
        proxy_url = f"socks5h://127.0.0.1:{cls.SOCKS5_PORT}"
        return {"http": proxy_url, "https": proxy_url}
