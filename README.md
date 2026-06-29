# susung-alert

📢 메자닌 모니터링 시스템 - 텔레그램 알림 봇

## 🎯 무엇을 하나요?

113개+ 보유 메자닌 종목에 대해 **5가지 알림**을 실시간으로 텔레그램으로 발송합니다.

## 📨 알림 종류 5가지

| 알림 | 빈도 | 내용 |
|---|---|---|
| 1. 보유종목 신규 공시 | 평일 매 10분 | 발행사/교환대상 회사 DART 공시 |
| 2. 6종 이벤트 D-day | 매일 KST 09시 | 전환청구/만기/리픽싱/PUT/CALL 사전 알림 |
| 3. 신규 CB/EB/BW 발행 | 평일 매 10분 | 시장 전체 신규 메자닌 발행 감지 |
| 4. 자본변동 | 평일 매 10분 + 09시 요약 | 주식병합/액면병합/증자 등 |
| 5. 주가 5% 변동 | 장중 | 발행사 또는 교환대상 주식 |

## 🤖 봇 채팅 기능

봇한테 채팅으로 종목명 입력하면 즉시 상세 정보 응답!

> ⚡ **실시간 응답은 Cloudflare Worker가 담당** (이 레포의 telegram_commands.py는 백업용)

### 명령어

```
종목명 입력     → 종목 상세 정보 (예: 천보)
ISIN 입력      → ISIN으로 직접 검색
/help          → 도움말
/list          → 보유 종목 목록
/total         → 보유 통계
/upcoming      → 7일 내 이벤트
/upcoming30    → 30일 내 이벤트
/capital       → 자본변동 내역
/match         → 매칭 수동 등록
/status        → 매칭 정보 조회
```

## 📦 구성

```
.
├── alert.py                # 메인 알림 스크립트
├── telegram_commands.py    # 봇 명령어 처리 (백업)
├── sent_rcept_nos.json     # 중복 방지 (자동 갱신)
├── last_update_id.json     # Telegram 처리 ID (자동 갱신)
├── requirements.txt
└── .github/workflows/
    └── run.yml             # 시간대별 차등 실행
```

## ⏰ 실행 주기

```
평일 09:00~15:30 → 10분마다 (장중)
평일 07:00~08:59 → 1시간마다 (장 전)
평일 15:30~19:59 → 1시간마다 (장 마감 후)
평일 20:00~익일 06:59 → 작동 안 함
주말 → 작동 안 함
```

## 🚀 수동 실행

```
1. https://github.com/shb3891/susung-alert/actions
2. [Susung Alert Bot] 선택
3. [Run workflow] 클릭
```

## 🔧 환경설정

### Secrets

```
DART_API_KEY
GCP_SERVICE_ACCOUNT_KEY
SHEET_ID
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

## 🔗 관련 시스템

- [dart-monitor](https://github.com/shb3891/dart-monitor) - 데이터 수집
- Cloudflare Worker (실시간 봇) - 별도 배포

## 📖 자세한 문서

전체 시스템 매뉴얼은 별도 문서 참조.

---

**Tech Stack:** Python 3.11 + gspread + DART OpenAPI + Telegram Bot API + GitHub Actions
