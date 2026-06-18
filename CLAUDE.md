# naver-to-coupang 프로젝트 지침

## ⚠️ 서버/터널 — 절대 건드리지 말 것

### 현재 구조 (이미 완성된 상태)
- **앱 서버**: root@115.68.223.177 (iwinv KR1-Z01, 4vCPU/4GB, Ubuntu 22.04)
- **앱 경로**: /root/naver-to-coupang/
- **SSH 키**: `C:/Users/pp/Desktop/iwinv_key` (ED25519, 패스프레이즈 없음)
- **서비스**: `naver-coupang` (Python 앱, port 8080) + `cloudflared` — 둘 다 systemd 등록, 서버 재부팅해도 자동 시작
- **도메인**: bakoo-mm.com → Cloudflare tunnel → 서버:8080
- **VPS/네이버클라우드/외부 서비스 없음** — Cloudflare tunnel만 사용

### 구 서버 (사용 중단 — 해지 예정)
- ubuntu@1.201.123.110 — SSH 키: `C:/Users/pp/Desktop/naver to coupang/ssh_keys/SSH_KeyPair-260527213658.pem`
- 이전 완료됨 (2026-06-19). 요금 절약 위해 해지 가능.

### 이 PC에서 절대 하면 안 되는 것
- `cloudflared tunnel create/delete/update` 실행 금지
- `~/.cloudflared/config.yml` 수정 금지
- `cloudflared service install/uninstall` 실행 금지
- `run.bat` 실행 금지 — 로컬에서 앱 따로 띄울 필요 없음
- Cloudflare 대시보드에서 tunnel 라우팅 변경 금지
- **"VPS 어디서 샀냐", "네이버클라우드 확인해봐라", "이메일 확인해봐라" 같은 질문 하지 말 것** — VPS 없음, Cloudflare tunnel뿐

### bakoo-mm.com 안 열릴 때 확인 순서
```bash
# 1. 서버 상태 확인 (이것만 하면 됨)
ssh -i "C:/Users/pp/Desktop/iwinv_key" root@115.68.223.177 "systemctl is-active naver-coupang cloudflared"
```
→ 둘 다 `active` 이면 서버 OK. Cloudflare 대시보드 tunnel 라우팅 문제.
→ 하나라도 `inactive` 이면: `systemctl restart naver-coupang` 또는 `systemctl restart cloudflared`

---

## 배포 절차 (코드 수정 후 반드시)

코드를 로컬에서 수정해도 bakoo-mm.com에는 반영 안 됨. 반드시 아래 순서로 배포.

```bash
# 파일 업로드
scp -i "C:/Users/pp/Desktop/iwinv_key" \
  "C:/Users/pp/Desktop/naver to coupang/수정파일.py" \
  root@115.68.223.177:/root/naver-to-coupang/수정파일.py

# 서비스 재시작 (반드시!)
ssh -i "C:/Users/pp/Desktop/iwinv_key" \
  root@115.68.223.177 "systemctl restart naver-coupang"
```

---

## 작업 원칙

1. 기존 코드 수정 시 → 영향받는 기능 먼저 브리핑 후 진행
2. 단순 신규 추가는 바로 진행
3. 배포 없이 끝내지 말 것 — 로컬 수정은 의미 없음
4. 서버 관련 문제는 SSH로 직접 확인, 외부 서비스 추측 금지

---

## 핵심 기능 구조 (2026-06-19 기준)

### 수집 파이프라인
- **진입점**: `app_gui.py` → `_process_entry()` (단일/배치 공용)
- **크롤러**: `modules/crawler.py` → `Crawler.fetch()` — 네이버 스마트스토어 상품 정보 수집
- **가격 계산**: `modules/price_calculator.py` → `PriceCalculator.calculate()`
  - 공식: `cost = price × qty + delivery.effective_fee(qty)`
  - `effective_fee`: FREE=0, PAID=base_fee, UNIT_QUANTITY_PAID=ceil(qty/N)×fee
- **이미지**: `modules/image_processor.py` → `ImageProcessor.process()` — 누끼+수량배지 합성
- **엑셀 출력**: `modules/excel_builder.py` → Wing 업로드용 xlsm 생성

### N개마다 배송비 (수동 입력 방식)
- 크롤러 자동추출 제거됨 (Playwright 봇 감지 실패로 → 수동 입력으로 전환)
- **UI**: 수집 목록 각 아이템에 "N개마다 배송비" 숫자 입력 (orange 색상 레이블)
  - pending 상태: 📦 수량 설정 버튼 오른쪽
  - done/error 상태: 최소/최대 수량 행 오른쪽
- **동작**: `QueueEntry.bundle_unit > 0` 이면 가격계산 직전 `product.delivery`에 주입
  - `product.delivery.bundle_unit = entry.bundle_unit`
  - `product.delivery.bundle_fee = product.delivery.base_fee`
- **필드**: `QueueEntry.bundle_unit: int = 0` — 직렬화/복원 포함

### 배송비 관련 DeliveryInfo
```python
# modules/crawler.py
class DeliveryInfo:
    base_fee:    int    # 기본 배송비
    fee_type:    str    # FREE / PAID / UNIT_QUANTITY_PAID / CONDITIONAL_FREE
    bundle_unit: int    # N개마다 (크롤러가 못 잡으면 QueueEntry.bundle_unit에서 주입)
    bundle_fee:  int    # 묶음당 배송비 (= base_fee)

def effective_fee(qty):
    if FREE: return 0
    if bundle_unit and bundle_fee:
        return ceil(qty/bundle_unit) * bundle_fee
    return base_fee  # 단일 배송비
```

### 이미지 관련
- **한글 폰트**: 서버에 `fonts-nanum` 설치됨 → `/usr/share/fonts/truetype/nanum/NanumGothic.ttf`
- **해외배송.png**: `/root/naver-to-coupang/data/해외배송.png` — lead_time=10 이면 상세페이지 최상단 합성
  - 사용자 직접 만든 파일 (덮어쓰기로 관리, 클로드가 임의 변경 금지)
- **수량 배지**: 좌하단 원형 배지, 폰트 `NanumGothic` 사용

### Wing 엑셀 업로드
- **형식**: `.xlsm` (매크로 포함) — ZIP XML 패칭 방식 (`_patch_xlsx_prices`)
- **가격 컬럼**: P열 (변경/수정요청 판매가격), 셀 형식 `t="n"` 숫자
- **현재 이슈**: "일괄변경 할 데이터가 없습니다" 오류 미해결 (t="n" 변환 후에도 재현)

### 가격수정 탭 (/price-fix)
- Wing에서 다운받은 xlsm 업로드 → 네이버 현재가로 일괄 가격 재산출
- URL 매핑: Wing 상품명 → 네이버 URL (저장된 이력 또는 직접 입력)

### 세션 저장/복원
- **경로**: `data/queue_state.json` (자동저장)
- **세션 파일**: `data/sessions/` — 배치 처리 완료 후 저장
- **복원**: 앱 재시작 시 자동 복원 (done 항목도 pending으로 리셋)
