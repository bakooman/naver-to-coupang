"""
Module 4 – 쿠팡 OPEN API 연동

공식 문서: https://developers.coupang.com/
인증: HMAC-SHA256  (CEA algorithm=HmacSHA256)

구현 완료:
  ✅ HMAC 서명
  ✅ 이미지 업로드 (multipart/form-data)
  ✅ 상품 등록 JSON 페이로드 (묶음별 할인가/정상가)
  ✅ 상품 등록 POST 요청

미완료:
  ⬜ 카테고리 코드 매핑 테이블 (displayCategoryCode 현재 0)
"""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from config.settings import Settings


class CoupangAPI:
    BASE_URL = "https://api-gateway.coupang.com"

    def __init__(self, settings: Settings):
        self.access_key = settings.COUPANG_ACCESS_KEY
        self.secret_key = settings.COUPANG_SECRET_KEY
        self.vendor_id  = settings.COUPANG_VENDOR_ID
        self._proxies   = settings.socks5_proxies()   # None or {"https": "socks5h://..."}

        if self._proxies:
            print(f"[CoupangAPI] SOCKS5 터널 사용: {list(self._proxies.values())[0]}")
        else:
            print("[CoupangAPI] 직접 연결 (SOCKS5 비활성화)")

    # ── HMAC 서명 ─────────────────────────────────────────────────

    def _sign(self, method: str, path: str, query: str = "") -> dict:
        """
        쿠팡 OPEN API HMAC-SHA256 인증 헤더.
        Authorization: CEA algorithm=HmacSHA256, access-key=...,
                       signed-date=..., signature=...
        """
        dt  = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
        msg = f"{dt}{method}{path}{query}"
        sig = hmac.new(
            self.secret_key.encode("utf-8"),
            msg.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": (
                f"CEA algorithm=HmacSHA256, "
                f"access-key={self.access_key}, "
                f"signed-date={dt}, "
                f"signature={sig}"
            ),
            "Content-Type":            "application/json;charset=UTF-8",
            "X-Coupang-Target-Market": "KR",
        }

    # ── 이미지 업로드 ─────────────────────────────────────────────

    def upload_image(self, image_path: str) -> Optional[str]:
        """
        로컬 이미지를 쿠팡 서버에 업로드하고 CDN URL을 반환.

        엔드포인트:
          POST /v2/providers/seller_api/apis/api/v1/vendor/items/images/upload

        반환값:
          str  : 업로드 성공 시 쿠팡 CDN URL
          None : 실패 시
        """
        if not os.path.isfile(image_path):
            print(f"[CoupangAPI] 업로드 대상 파일 없음: {image_path}")
            return None

        path = "/v2/providers/seller_api/apis/api/v1/vendor/items/images/upload"
        url  = self.BASE_URL + path

        # HMAC 헤더 생성 후 Content-Type 제거
        # (multipart 시 requests가 boundary 포함 Content-Type을 자동 설정)
        headers = self._sign("POST", path)
        headers.pop("Content-Type", None)

        filename  = os.path.basename(image_path)
        mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"

        try:
            with open(image_path, "rb") as f:
                files = {"image": (filename, f, mime_type)}
                resp  = requests.post(
                    url, headers=headers, files=files,
                    proxies=self._proxies, timeout=60
                )

            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == "SUCCESS":
                cdn_url = data.get("data", "")
                print(f"[CoupangAPI] 이미지 업로드 성공: {cdn_url}")
                return cdn_url

            print(f"[CoupangAPI] 이미지 업로드 실패 응답: {data}")
            return None

        except Exception as exc:
            print(f"[CoupangAPI] 이미지 업로드 오류 ({filename}): {exc}")
            return None

    # ── 상품 등록 ─────────────────────────────────────────────────

    def register_product(self, payload: dict) -> dict:
        """
        쿠팡 마켓플레이스 상품 등록 POST.
        payload: build_payload() 로 생성한 dict
        """
        path    = "/v2/providers/seller_api/apis/api/v1/vendor/items"
        url     = self.BASE_URL + path
        headers = self._sign("POST", path)

        resp = requests.post(
            url, headers=headers, json=payload,
            proxies=self._proxies, timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    # ── 상품 등록 JSON 조립 ───────────────────────────────────────

    @staticmethod
    def build_payload(
        vendor_id:    str,
        product_name: str,
        bundles:      list[dict],
        delivery_fee: int = 0,
        return_fee:   int = 5000,
        category_code: int = 0,   # TODO: 실제 카테고리 코드
        brand:        str = "",
    ) -> dict:
        """
        쿠팡 상품 등록 JSON 페이로드 생성.

        bundles 형식 (price_calculator.BundlePrice 기준):
          [
            {
              "qty":            1,
              "sale_price":     15230,    # salePrice   (할인가)
              "original_price": 16430,    # originalPrice (정상가)
              "image_url":      "https://cdn.coupang.com/...",
            },
            ...
          ]
        """
        units = []
        for b in bundles:
            qty       = b["qty"]
            sale      = b["sale_price"]
            original  = b["original_price"]
            image_url = b.get("image_url", "")

            units.append({
                # 옵션명
                "vendorItemName": (
                    f"{product_name} {qty}개 묶음" if qty > 1 else product_name
                ),
                # 판매자 SKU (중복 불가)
                "sellerProductItemCode": f"{vendor_id}_{qty}EA",

                # 가격
                "salePrice":     sale,      # 시작가 (할인가)
                "originalPrice": original,  # 최고가 (정상가)

                # 구매 제한
                "maximumBuyCount":         999,
                "maximumBuyForPeriodType": "NONE",

                # 재고
                "unitCount": 1,

                # 이미지 (쿠팡 CDN URL)
                "images": (
                    [{
                        "imageOrder": 0,
                        "imageType":  "REPRESENTATION",
                        "cdnPath":    image_url,
                    }]
                    if image_url else []
                ),
            })

        return {
            # 카테고리
            "displayCategoryCode": category_code,

            # 기본 정보
            "sellerProductName":  product_name,
            "displayProductName": product_name,
            "vendorId":           vendor_id,
            "brand":              brand,

            # 판매 기간 (무기한)
            "saleStartedAt": "2000-01-01T00:00:00",
            "saleEndedAt":   "2099-12-31T23:59:59",

            # 배송
            "deliveryChargeType":    "FREE" if delivery_fee == 0 else "PAYMENT",
            "deliveryCharge":        delivery_fee,
            "freeShipOverAmount":    0,
            "returnChargeVendor":    return_fee,
            "outboundShippingTimeDay": 2,

            # 묶음별 유닛
            "items": units,
        }
