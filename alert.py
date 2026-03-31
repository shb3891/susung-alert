import os
import json
import time
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

# ============================================================
# [설정]
# ============================================================
DART_KEY = (
    os.environ.get('DART_API_KEY') or
    os.environ.get('DART_KEY') or
    'bfc4e4e445de4727ae0bcc27e80ba5cf0e3818e6'
)
SHEET_ID       = os.environ.get('SHEET_ID', '1s73BDNtCPe5mOs9EjBE5npEfcaNtYRyWJxRBUmJI-WA')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
DART_BASE      = "https://opendart.fss.or.kr/api"

NOW   = datetime.utcnow() + timedelta(hours=9)   # KST
TODAY = NOW.strftime('%Y-%m-%d')
KST_H = NOW.hour

# PUT/CALL 임박 알람 기준 (일)
ALERT_DAYS = [30, 7, 1]

# 중복 방지용 파일
SENT_FILE = 'sent_rcept_nos.json'

# ============================================================
# [중복 방지: 보낸 공시번호 로드/저장]
# ============================================================
def load_sent_nos() -> set:
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
            # 오늘 날짜 것만 유지 (매일 자동 초기화)
            return set(data.get(TODAY, []))
    except Exception:
        return set()

def save_sent_nos(sent_nos: set):
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[TODAY] = list(sent_nos)
    # 오늘 것만 유지 (오래된 날짜 제거)
    data = {k: v for k, v in data.items() if k == TODAY}
    with open(SENT_FILE, 'w') as f:
        json.dump(data, f)

# ============================================================
# [텔레그램 전송]
# ============================================================
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"⚠ 텔레그램 설정 없음")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                'chat_id': TELEGRAM_CHAT,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }, timeout=10
        )
        if r.status_code == 200:
            print(f"  ✅ 전송 완료")
        else:
            print(f"  ⚠ 전송 실패: {r.status_code}")
    except Exception as e:
        print(f"  ⚠ 전송 오류: {e}")

# ============================================================
# [보유 종목 로드 - 구글 스프레드시트]
# ============================================================
def load_holdings():
    print("📋 보유 종목 로드 중...")
    creds_json = json.loads(os.environ.get('GCP_SERVICE_ACCOUNT_KEY'))
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(SHEET_ID).get_worksheet(0)
    all_values = ws.get_all_values()

    holdings = []
    for row in all_values[1:]:
        if len(row) < 2 or not row[1].strip().startswith('KR'):
            continue
        if not row[0].strip() or row[0].strip() == '-':
            continue
        def g(i): return row[i].strip() if len(row) > i else ''
        holdings.append({
            'name':       g(0),
            'isin':       g(1),
            'hosu':       g(2),
            'bond_type':  g(3),
            'put_begin':  g(12),
            'put_end':    g(13),
            'put_date':   g(14),
            'call_ratio': g(16),
            'call_begin': g(17),
            'call_end':   g(18),
        })
    print(f"  ✅ {len(holdings)}개 종목 로드")
    return holdings

# ============================================================
# [corp_code 조회]
# ============================================================
def get_corp_code(corp_name: str) -> str:
    try:
        r = requests.get(
            f"{DART_BASE}/company.json",
            params={'crtfc_key': DART_KEY, 'corp_name': corp_name},
            timeout=10,
        )
        data = r.json()
        if data.get('status') != '000':
            return ''
        items = data.get('list', [])
        listed = [x for x in items if x.get('stock_code', '').strip()]
        for item in (listed or items):
            return item.get('corp_code', '')
    except Exception:
        pass
    return ''

# ============================================================
# [알람1] 보유 종목 신규 공시 (매 5분, 중복 방지)
# ============================================================
def check_new_disclosures(holdings, sent_nos: set):
    print(f"\n📢 [알람1] 보유 종목 신규 공시 체크...")
    new_count = 0

    for h in holdings:
        name = h['name']
        corp_code = get_corp_code(name)
        if not corp_code:
            continue

        try:
            r = requests.get(
                f"{DART_BASE}/list.json",
                params={
                    'crtfc_key': DART_KEY,
                    'corp_code': corp_code,
                    'bgn_de': TODAY.replace('-', ''),
                    'end_de': TODAY.replace('-', ''),
                    'page_count': 10,
                }, timeout=10
            )
            data = r.json()
            if data.get('status') not in ('000', '013'):
                continue

            for item in (data.get('list') or []):
                rcept_no = item.get('rcept_no', '')
                if not rcept_no or rcept_no in sent_nos:
                    continue   # 이미 보낸 공시 스킵

                rpt  = item.get('report_nm', '')
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

                msg = (
                    f"📢 <b>보유종목 신규 공시</b>\n\n"
                    f"🔹 <b>{name}</b> ({h['bond_type']} {h['hosu']}회)\n"
                    f"   📄 {rpt}\n"
                    f"   📅 {TODAY}\n"
                    f"   🔗 <a href='{link}'>DART 바로가기</a>"
                )
                send_telegram(msg)
                sent_nos.add(rcept_no)
                new_count += 1
                print(f"  📄 {name}: {rpt}")
                time.sleep(0.5)

        except Exception as e:
            print(f"  ⚠ {name} 조회 실패: {e}")
        time.sleep(0.3)

    print(f"  → 신규 공시 {new_count}건 전송")

# ============================================================
# [알람2] PUT/CALL 행사 임박 (KST 오전 9시에만 실행)
# ============================================================
def check_put_call_deadline(holdings):
    if KST_H != 9:
        print(f"\n⏰ [알람2] PUT/CALL 임박 스킵 (KST {KST_H}시)")
        return

    print(f"\n⏰ [알람2] PUT/CALL 행사 임박 체크...")
    today_dt = datetime.strptime(TODAY, '%Y-%m-%d')

    check_fields = [
        ('put_begin', 'PUT 청구 시작', 'PUT',  '🔵'),
        ('put_end',   'PUT 청구 종료', 'PUT',  '🔵'),
        ('put_date',  'PUT 상환지급',  'PUT',  '🔵'),
        ('call_begin','CALL 청구 시작','CALL', '🔴'),
        ('call_end',  'CALL 청구 종료','CALL', '🔴'),
    ]

    for h in holdings:
        for field, label, dtype, emoji in check_fields:
            dt_str = h.get(field, '')
            if not dt_str or dt_str in ('-', ''):
                continue
            try:
                diff = (datetime.strptime(dt_str, '%Y-%m-%d') - today_dt).days
                if diff not in ALERT_DAYS:
                    continue
                msg = (
                    f"{emoji} <b>{dtype} 행사 임박 D-{diff}</b>\n\n"
                    f"🔹 <b>{h['name']}</b> ({h['bond_type']} {h['hosu']}회)\n"
                    f"   📅 {label}: {dt_str}\n"
                    f"   🆔 {h['isin']}"
                )
                send_telegram(msg)
                print(f"  ⏰ {h['name']} {label}: D-{diff}")
                time.sleep(0.3)
            except Exception:
                continue

# ============================================================
# [알람3] 신규 CB/EB/BW 발행결정 (시장 전체 스캔, 중복 방지)
# ============================================================
def check_new_issuance(sent_nos: set):
    print(f"\n🔍 [알람3] 신규 CB/EB/BW 발행결정 체크...")
    issue_kws = [
        '전환사채권발행결정',
        '교환사채권발행결정',
        '신주인수권부사채권발행결정',
    ]
    new_count = 0

    try:
        r = requests.get(
            f"{DART_BASE}/list.json",
            params={
                'crtfc_key': DART_KEY,
                'pblntf_ty': 'B',
                'bgn_de': TODAY.replace('-', ''),
                'end_de': TODAY.replace('-', ''),
                'page_count': 40,
            }, timeout=10
        )
        data = r.json()
        if data.get('status') not in ('000', '013'):
            print(f"  → DART 응답: {data.get('status')}")
            return

        for item in (data.get('list') or []):
            rpt      = item.get('report_nm', '')
            rcept_no = item.get('rcept_no', '')

            if '[첨부정정]' in rpt or '[첨부추가]' in rpt:
                continue
            if not any(kw in rpt for kw in issue_kws):
                continue
            if not rcept_no or rcept_no in sent_nos:
                continue   # 이미 보낸 공시 스킵

            corp_name = item.get('corp_name', '')
            link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

            if '전환사채' in rpt:   btype = 'CB'
            elif '교환사채' in rpt: btype = 'EB'
            else:                   btype = 'BW'

            msg = (
                f"🔍 <b>신규 {btype} 발행결정</b>\n\n"
                f"🔹 <b>{corp_name}</b> [{btype}]\n"
                f"   📄 {rpt}\n"
                f"   📅 {TODAY}\n"
                f"   🔗 <a href='{link}'>DART 바로가기</a>"
            )
            send_telegram(msg)
            sent_nos.add(rcept_no)
            new_count += 1
            print(f"  📄 {corp_name}: {rpt}")
            time.sleep(0.5)

    except Exception as e:
        print(f"  ⚠ 발행결정 조회 실패: {e}")

    print(f"  → 신규 발행결정 {new_count}건 전송")

# ============================================================
# [실행]
# ============================================================
if __name__ == '__main__':
    print(f"🤖 수성 공시 알람 시작 (KST {TODAY} {KST_H}시)")

    # 중복 방지: 오늘 보낸 공시번호 로드
    sent_nos = load_sent_nos()
    print(f"  📋 기존 전송 기록: {len(sent_nos)}건")

    # 보유 종목 로드
    holdings = load_holdings()

    if holdings:
        check_new_disclosures(holdings, sent_nos)   # 알람1: 보유종목 공시
        check_put_call_deadline(holdings)            # 알람2: PUT/CALL 임박 (9시만)
        check_new_issuance(sent_nos)                 # 알람3: 신규 발행결정

    # 보낸 공시번호 저장
    save_sent_nos(sent_nos)
    print(f"\n🏁 완료!")
