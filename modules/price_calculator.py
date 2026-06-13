"""
Module 3 – 묶음별 쿠팡 판매가 산출

공식 (고정 배율):
  원가       = 네이버 상품가 × 수량  +  실효배송비
  시작가격   = floor10(원가 × 1.27)   ← autoPricingInfoView.minimumPrice (자동가격조정 하한)
  판매가     = floor10(원가 × 1.37)   ← salePrice / wishPrice (실제 판매가격 / 자동가격조정 목표)

floor10: 10원 단위 내림(절사)
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from modules.crawler import ProductData
from config.settings import Settings

# ── 고정 배율 ─────────────────────────────────────────────────────
SALE_RATE     = 1.27   # 시작가격 배율 (자동가격조정 하한)
ORIGINAL_RATE = 1.37   # 판매가 배율  (실제 판매가 / 자동가격조정 목표)


def floor10(value: float) -> int:
    """10원 단위 내림(절사)."""
    return math.floor(value / 10) * 10


@dataclass
class BundlePrice:
    qty:            int
    source_cost:    int   # 네이버 원가  (상품가 × qty + 실효배송비)
    sale_price:     int   # 시작가격 (×1.27) = 자동가격조정 하한 → minimumPrice
    original_price: int   # 판매가   (×1.37) = 실제 판매가 / 자동가격조정 목표 → salePrice / wishPrice


class PriceCalculator:

    def __init__(self, settings: Settings):
        pass   # Settings 객체는 인터페이스 일관성을 위해 유지

    def calculate(
        self,
        product: ProductData,
        quantities: list[int] | None = None,
        sale_rate: float | None = None,
        original_rate: float | None = None,
    ) -> list[BundlePrice]:
        if quantities is None:
            quantities = [1, 2, 3]
        _sale_rate     = sale_rate     if sale_rate     is not None else SALE_RATE
        _original_rate = original_rate if original_rate is not None else ORIGINAL_RATE

        results: list[BundlePrice] = []
        for qty in quantities:
            delivery = product.delivery.effective_fee(qty)
            cost     = product.price * qty + delivery
            sale     = floor10(cost * _sale_rate)
            original = floor10(cost * _original_rate)

            results.append(BundlePrice(
                qty=qty,
                source_cost=cost,
                sale_price=sale,
                original_price=original,
            ))
            print(
                f"[Calculator] {qty}개 묶음 | "
                f"원가 {cost:,}원 → 시작가격 {sale:,}원 (×{_sale_rate}) "
                f"/ 판매가 {original:,}원 (×{_original_rate})"
            )

        return results
