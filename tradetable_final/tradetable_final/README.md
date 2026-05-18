# TradeTable — KSA 시간표 트레이드 시스템

## 로컬 실행

```bash
# 1. 패키지 설치 (최초 1회)
pip install -r requirements.txt

# 2. 서버 실행
python app.py

# 3. 브라우저에서 접속
http://localhost:5000
```

### Python 3.7 환경 오류 시
```bash
pip install urllib3==1.26.18
pip install flask requests beautifulsoup4 gunicorn
python app.py
```

---

## Render.com 무료 배포 (사이트로 운영)

### 1단계 — GitHub에 올리기
```bash
git init
git add .
git commit -m "TradeTable 초기 배포"
```
GitHub에서 새 저장소 생성 후:
```bash
git remote add origin https://github.com/본인계정/tradetable.git
git push -u origin main
```

### 2단계 — Render 설정
1. [render.com](https://render.com) 회원가입 (GitHub 계정으로 가능)
2. **New → Web Service** 클릭
3. GitHub 저장소 연결
4. 아래 설정 입력:

| 항목 | 값 |
|------|-----|
| Build Command | `pip install -r requirements.txt` |
| Start Command | `gunicorn app:app --bind 0.0.0.0:$PORT` |
| Environment | Python 3 |

5. **Environment Variables** 추가:
   - `ADMIN_PW` = 원하는 관리자 비밀번호
   - `SECRET_KEY` = 임의의 긴 문자열

6. **Create Web Service** 클릭 → 자동 배포 완료

배포 완료 후 `https://tradetable.onrender.com` 같은 주소로 접속 가능합니다.

---

## 사용법

### 일반 사용자
- 가온누리(ksain.net)와 동일한 학번·비밀번호로 로그인
- 학번 형식: `25-006` 또는 `25006` 모두 허용
- 시간표에서 과목 클릭 → 분반 선택 → 트레이드 실행

### 관리자
- 학번: 아무 학번 (예: `25-006`)
- 비밀번호: `tradetable-admin-2026`
- 알고리즘 테스트 케이스 6개 실행 가능

---

## 파일 구조

```
tradetable_final/
├── app.py              ← Flask 서버 + 알고리즘
├── requirements.txt    ← 패키지 목록
├── Procfile            ← Render/Heroku 배포 설정
├── render.yaml         ← Render 자동 배포 설정
├── static/
│   └── data.json       ← 2026-1학기 전체 시간표·수강 데이터
└── templates/
    └── index.html      ← 웹 UI
```

---

*KSA 정보과학 프로젝트 — 25-006 구정준 · 25-096 임서연*
