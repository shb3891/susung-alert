import os
import re
import json
import time
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

# 텔레그램 명령어 처리 모듈 (같은 폴더에 telegram_commands.py 필요)
from telegram_commands import process_telegram_updates

# ============================================================
# [설정]
# ============================================================
DART_KEY = (
    os.environ.get('DART_API_KEY') or
    os.environ.get('DART_KEY') or
    'bfc4e4e445de4727ae0bcc27e80ba5cf0e3818e6'
)
SHEET_ID        = os.environ.get('SHEET_ID', '1s73BDNtCPe5mOs9EjBE5npEfcaNtYRyWJxRBUmJI-WA')
TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT   = os.environ.get('TELEGRAM_CHAT_ID', '')
DART_BASE       = "https://opendart.fss.or.kr/api"

NOW   = datetime.utcnow() + timedelta(hours=9)   # KST
TODAY = NOW.strftime('%Y-%m-%d')
KST_H = NOW.hour

ALERT_DAYS = [30, 7, 1]
PRICE_THRESHOLD = 5.0
SENT_FILE = 'sent_rcept_nos.json'


# ============================================================
# [중복 방지: 보낸 공시번호 로드/저장]
# ============================================================
def load_sent_nos() -> set:
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
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
# [보유 종목 로드]
# ============================================================
def load_holdings(sh):
    print("📋 보유 종목 로드 중...")

    # === 1. 메인시트(시트1) 로드 ===
    ws = sh.get_worksheet(0)
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

    # === 2. 주식코드매칭 시트 로드 ===
    match_map = {}
    try:
        ws_match = sh.worksheet('주식코드매칭')
        match_rows = ws_match.get_all_values()
        for row in match_rows[1:]:
            if len(row) >= 8 and row[0].strip().startswith('KR'):
                isin = row[0].strip()
                match_map[isin] = {
                    'corp_code':    row[7].strip(),
                    'target_name':  row[5].strip(),
                    'target_code':  row[6].strip(),
                    'status':       row[8].strip() if len(row) > 8 else '',
                }
        print(f"  ✅ 주식코드매칭 시트: {len(match_map)}개 종목 로드")
    except Exception as e:
        print(f"  ⚠ 주식코드매칭 시트 로드 실패: {e}")

    # === 3. 주식코드 시트 로드 (주가 알람용) ===
    try:
        ws_stock = sh.worksheet('주식코드')
        stock_rows = ws_stock.get_all_values()
        stock_map = {}
        for row in stock_rows[1:]:
            if len(row) >= 5 and row[1].strip():
                stock_map[row[1].strip()] = {
                    'issuer': row[3].strip(),
                    'target': row[4].strip(),
                }
    except Exception as e:
        print(f"  ⚠ 주식코드 시트 로드 실패: {e}")
        stock_map = {}

    # === 4. 합치기 ===
    for h in holdings:
        match = match_map.get(h['isin'], {})
        h['corp_code']    = match.get('corp_code', '')
        h['target_name']  = match.get('target_name', '')
        h['target_code']  = match.get('target_code', '')
        h['match_status'] = match.get('status', '')

        codes = stock_map.get(h['isin'], {})
        h['issuer_code'] = codes.get('issuer', '')
        if not h['target_code']:
            h['target_code'] = codes.get('target', '')

    print(f"  ✅ {len(holdings)}개 종목 로드")

    matched = sum(1 for h in holdings if h['corp_code'])
    unmatched = len(holdings) - matched
    print(f"  📊 매칭됨: {matched}개 / 매칭 안 됨: {unmatched}개")

    return holdings

# ============================================================
# [알람1] 보유 종목 신규 공시
# ============================================================
def check_new_disclosures(holdings, sent_nos: set):
    print(f"\n📢 [알람1] 보유 종목 신규 공시 체크...")
    new_count = 0
    skipped = 0

    for h in holdings:
        corp_code = h.get('corp_code', '')
        if not corp_code:
            skipped += 1
            continue

        bond_name   = h['name']
        target_name = h.get('target_name', '') or bond_name

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
                    continue

                rpt  = item.get('report_nm', '')
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

                msg = (
                    f"📢 <b>보유종목 신규 공시</b>\n\n"
                    f"🔹 <b>{bond_name}</b>\n"
                    f"🏢 공시: {target_name}\n"
                    f"📄 {rpt}\n"
                    f"📅 {TODAY}\n"
                    f"🔗 <a href='{link}'>DART 바로가기</a>"
                )
                send_telegram(msg)
                sent_nos.add(rcept_no)
                new_count += 1
                print(f"  📄 {bond_name} → {target_name}: {rpt}")
                time.sleep(0.5)

        except Exception as e:
            print(f"  ⚠ {bond_name} 조회 실패: {e}")
        time.sleep(0.3)

    print(f"  → 신규 공시 {new_count}건 전송 (매칭 안 된 {skipped}개 종목 스킵)")

# ============================================================
# [알람2] PUT/CALL 행사 임박 (KST 9시만)
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
# [알람3] 신규 CB/EB/BW 발행결정
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
                continue

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
# [네이버 주가 조회]
# ============================================================
def get_naver_stock_price(stock_code: str) -> dict:
    try:
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'euc-kr'
        html = r.text

        cur_m  = re.search(r'<p class="no_today">.*?<span class="blind">([0-9,]+)</span>', html, re.DOTALL)
        prev_m = re.search(r'전일\s*<em[^>]*>([0-9,]+)</em>', html)

        if cur_m and prev_m:
            current    = int(cur_m.group(1).replace(',', ''))
            prev_close = int(prev_m.group(1).replace(',', ''))
            if prev_close == 0:
                return {}
            change_pct = (current - prev_close) / prev_close * 100
            return {'current': current, 'prev_close': prev_close, 'change_pct': round(change_pct, 2)}
    except Exception as e:
        print(f"    ⚠ 주가 조회 실패 ({stock_code}): {e}")
    return {}

# ============================================================
# [알람4] 주가 5% 이상 변동
# ============================================================
def check_stock_price(holdings, sent_nos: set):
    if not (9 <= KST_H <= 15):
        print(f"\n📈 [알람4] 주가 변동 스킵 (KST {KST_H}시, 장시간 외)")
        return

    print(f"\n📈 [알람4] 주가 5% 변동 체크 (기준: {PRICE_THRESHOLD}%)...")

    alerted_today = set(s for s in sent_nos if s.startswith('PRICE_'))
    checked_codes = set()

    for h in holdings:
        for code_type, code in [('발행사', h.get('issuer_code', '')), ('교환대상', h.get('target_code', ''))]:
            if not code or len(code) != 6:
                continue
            alert_key = f"PRICE_{code}_{TODAY}"
            if alert_key in alerted_today or code in checked_codes:
                continue
            checked_codes.add(code)

            price = get_naver_stock_price(code)
            if not price:
                continue

            chg = price['change_pct']
            print(f"  📊 {h['name']} ({code}): {price['current']:,}원 ({chg:+.2f}%)")

            if abs(chg) >= PRICE_THRESHOLD:
                direction = '📈' if chg > 0 else '📉'
                msg = (
                    f"{direction} <b>주가 급변동 알람</b>\n\n"
                    f"🔹 <b>{h['name']}</b> ({h['bond_type']} {h['hosu']}회)\n"
                    f"   🏷 {code_type} 주식 [{code}]\n"
                    f"   💰 현재가: {price['current']:,}원\n"
                    f"   📊 전일 대비: <b>{chg:+.2f}%</b>\n"
                    f"   (전일 종가: {price['prev_close']:,}원)"
                )
                send_telegram(msg)
                sent_nos.add(alert_key)
                print(f"  {direction} {h['name']} ({code}): {chg:+.2f}% → 알람 전송")
            time.sleep(0.5)

# ============================================================
# [실행]
# ============================================================
if __name__ == '__main__':
    print(f"🤖 수성 공시 알람 시작 (KST {TODAY} {KST_H}시)")

    sent_nos = load_sent_nos()
    print(f"  📋 기존 전송 기록: {len(sent_nos)}건")

    # === Google Sheets 연결 ===
    creds_json = json.loads(os.environ.get('GCP_SERVICE_ACCOUNT_KEY'))
    creds = Credentials.from_service_account_info(creds_json, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    # === 알람 처리 ===
    holdings = load_holdings(sh)

    if holdings:
        check_new_disclosures(holdings, sent_nos)
        check_put_call_deadline(holdings)
        check_new_issuance(sent_nos)
        check_stock_price(holdings, sent_nos)

    save_sent_nos(sent_nos)

    # === 텔레그램 명령어 처리 (NEW) ===
    if TELEGRAM_TOKEN:
        process_telegram_updates(sh, TELEGRAM_TOKEN)

    print(f"\n🏁 완료!")
