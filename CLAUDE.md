# naver-to-coupang 프로젝트 지침

## ⚠️ 서버/터널 — 절대 건드리지 말 것

### 현재 구조 (이미 완성된 상태)
- **앱 서버**: ubuntu@1.201.123.110 (리눅스 서버)
- **앱 경로**: /home/ubuntu/naver-to-coupang/
- **SSH 키**: `C:/Users/pp/Desktop/naver to coupang/ssh_keys/SSH_KeyPair-260527213658.pem`
- **서비스**: `naver-coupang` (Python 앱, port 8080) + `cloudflared` — 둘 다 systemd 등록, 서버 재부팅해도 자동 시작
- **도메인**: bakoo-mm.com → Cloudflare tunnel → 서버:8080
- **VPS/네이버클라우드/외부 서비스 없음** — Cloudflare tunnel만 사용

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
ssh -i "C:/Users/pp/Desktop/naver to coupang/ssh_keys/SSH_KeyPair-260527213658.pem" ubuntu@1.201.123.110 "sudo systemctl is-active naver-coupang cloudflared"
```
→ 둘 다 `active` 이면 서버 OK. Cloudflare 대시보드 tunnel 라우팅 문제.
→ 하나라도 `inactive` 이면: `sudo systemctl restart naver-coupang` 또는 `sudo systemctl restart cloudflared`

---

## 배포 절차 (코드 수정 후 반드시)

코드를 로컬에서 수정해도 bakoo-mm.com에는 반영 안 됨. 반드시 아래 순서로 배포.

```bash
# 파일 업로드
scp -i "C:/Users/pp/Desktop/naver to coupang/ssh_keys/SSH_KeyPair-260527213658.pem" \
  "C:/Users/pp/Desktop/naver to coupang/수정파일.py" \
  ubuntu@1.201.123.110:/home/ubuntu/naver-to-coupang/수정파일.py

# 서비스 재시작 (반드시!)
ssh -i "C:/Users/pp/Desktop/naver to coupang/ssh_keys/SSH_KeyPair-260527213658.pem" \
  ubuntu@1.201.123.110 "sudo systemctl restart naver-coupang"
```

---

## 작업 원칙

1. 기존 코드 수정 시 → 영향받는 기능 먼저 브리핑 후 진행
2. 단순 신규 추가는 바로 진행
3. 배포 없이 끝내지 말 것 — 로컬 수정은 의미 없음
4. 서버 관련 문제는 SSH로 직접 확인, 외부 서비스 추측 금지
