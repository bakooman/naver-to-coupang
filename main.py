"""
네이버 스마트스토어 → 쿠팡 마켓플레이스 자동 등록 파이프라인
실행: python main.py  또는  python main.py <URL>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from config.settings import Settings
from modules.crawler       import NaverStoreCrawler
from modules.image_processor import ImageProcessor
from modules.price_calculator import PriceCalculator
from modules.coupang_api   import CoupangAPI


async def run_pipeline(url: str, settings: Settings) -> None:
    print("=" * 60)
    print("  네이버 → 쿠팡 파이프라인 시작")
    print("=" * 60)

    # ── Module 1: 크롤링 ──────────────────────────────────────────
    print("\n[1/4] 상품 데이터 크롤링 중...")
    product = await NaverStoreCrawler(settings).crawl(url)

    # ── Module 2: 이미지 가공 ─────────────────────────────────────
    composed_paths: dict[int, str] = {}
    if product.local_image_path:
        print("\n[2/4] 이미지 자동 가공 중...")
        composed_paths = ImageProcessor(settings).process(
            product.local_image_path, product.product_id
        )
    else:
        print("\n[2/4] 이미지 없음 – 가공 건너뜀")

    # ── Module 3: 판매가 계산 ─────────────────────────────────────
    print("\n[3/4] 묶음별 판매가 계산 중...")
    bundle_prices = PriceCalculator(settings).calculate(product)

    # ── 결과 요약 출력 ────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  계산 결과 요약")
    print("=" * 60)
    print(f"  상품명    : {product.name}")
    print(f"  네이버 가격: {product.price:,}원")
    print(f"  기본배송비 : {product.delivery.base_fee:,}원 ({product.delivery.fee_type})")
    if product.delivery.bundle_unit:
        print(
            f"  묶음배송  : {product.delivery.bundle_unit}개마다 "
            f"+{product.delivery.bundle_fee:,}원"
        )
    print()
    for bp in bundle_prices:
        img = composed_paths.get(bp.qty, "이미지 없음")
        print(
            f"  {bp.qty}개 묶음 | "
            f"원가 {bp.source_cost:,}원 → "
            f"시작가격 {bp.sale_price:,}원 / 판매가 {bp.original_price:,}원 / 최종가격 {bp.original_price:,}원"
            f"  ({img})"
        )

    # 중간 결과 JSON 저장
    output_dir = Path(settings.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_data = {
        "product_id": product.product_id,
        "name":       product.name,
        "source_url": product.url,
        "bundle_prices": [
            {
                "qty":            bp.qty,
                "source_cost":    bp.source_cost,
                "sale_price":     bp.sale_price,
                "original_price": bp.original_price,
                "image_path":     composed_paths.get(bp.qty, ""),
            }
            for bp in bundle_prices
        ],
    }
    out_path = output_dir / f"{product.product_id}_result.json"
    out_path.write_text(
        json.dumps(result_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n  중간 결과 저장: {out_path}")

    # ── Module 4: 쿠팡 API 상품 등록 ─────────────────────────────
    print("\n[4/4] 쿠팡 API 상품 등록...")

    if not (settings.COUPANG_ACCESS_KEY
            and settings.COUPANG_SECRET_KEY
            and settings.COUPANG_VENDOR_ID):
        print(
            "  ⚠️  쿠팡 API 키 미설정 → 등록 건너뜀\n"
            "  .env 에 COUPANG_ACCESS_KEY / COUPANG_SECRET_KEY / COUPANG_VENDOR_ID 입력 후 재실행"
        )
        return

    api = CoupangAPI(settings)

    # 4-A: 이미지 업로드 (로컬 → 쿠팡 CDN)
    coupang_img_urls: dict[int, str] = {}
    for qty, local_path in composed_paths.items():
        cdn = api.upload_image(local_path)
        if cdn:
            coupang_img_urls[qty] = cdn
        else:
            print(f"  ⚠️  {qty}개 이미지 업로드 실패 – 해당 옵션 이미지 없이 등록")

    # 4-B: 페이로드 구성
    bundles_data = [
        {
            "qty":            bp.qty,
            "sale_price":     bp.sale_price,
            "original_price": bp.original_price,
            "image_url":      coupang_img_urls.get(bp.qty, ""),
        }
        for bp in bundle_prices
    ]
    payload = CoupangAPI.build_payload(
        vendor_id=settings.COUPANG_VENDOR_ID,
        product_name=product.name,
        bundles=bundles_data,
    )

    # 4-C: 상품 등록 요청
    try:
        reg_result = api.register_product(payload)
        print(f"  ✅ 쿠팡 상품 등록 완료!")
        print(f"     응답: {json.dumps(reg_result, ensure_ascii=False, indent=4)}")

        # 등록 결과도 JSON으로 저장
        reg_path = output_dir / f"{product.product_id}_coupang_result.json"
        reg_path.write_text(
            json.dumps(reg_result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"     등록 결과 저장: {reg_path}")

    except Exception as exc:
        print(f"  ❌ 쿠팡 상품 등록 실패: {exc}")


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("네이버 스마트스토어 상품 URL을 입력하세요: ").strip()

    if not url:
        print("URL이 입력되지 않았습니다.")
        sys.exit(1)

    asyncio.run(run_pipeline(url, Settings()))


if __name__ == "__main__":
    main()
