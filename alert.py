import os
import re
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
SHEET_ID        = os.environ.get('SHEET_ID', '1s73BDNtCPe5mOs9EjBE5npEfcaNtYRyWJxRBUmJI-WA')
TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT   = os.environ.get('TELEGRAM_CHAT_ID', '')
DART_BASE       = "https://opendart.fss.or.kr/api"

NOW   = datetime.utcnow() + timedelta(hours=9)   # KST
TODAY = NOW.strftime('%Y-%m-%d')
KST_H = NOW.hour

# PUT/CALL 임박 알람 기준 (일)
ALERT_DAYS = [30, 7, 1]

# 주가 변동 알람 기준 (%)
PRICE_THRESHOLD = 5.0

# 중복 방지용 파일
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
# [보유 종목 로드 - 구글 스프레드시트]
# ============================================================
def load_holdings():
    """메인시트(시트1) + 주식코드매칭 시트를 함께 로드.
    
    - 메인시트: PUT/CALL 일정 (알람2용), 회차/종류 등 표시용
    - 주식코드매칭 시트: 공시 검색용 corp_code, 공시대상 종목명 (알람1용)
    """
    print("📋 보유 종목 로드 중...")
    creds_json = json.loads(os.environ.get('GCP_SERVICE_ACCOUNT_KEY'))
    scopes = [
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ]
    creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SHEET_ID)

    # === 1. 메인시트(시트1) 로드 ===
    ws    = sh.get_worksheet(0)
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

    # === 2. 주식코드매칭 시트 로드 (NEW) ===
    match_map = {}  # ISIN → {corp_code, target_name, target_code, status}
    try:
        ws_match = sh.worksheet('주식코드매칭')
        match_rows = ws_match.get_all_values()
        # 컬럼: A=ISIN, B=채권명, C=종류, D=콜상태, E=발행사주식코드,
        #       F=공시대상종목명, G=공시대상주식코드, H=DARTcorp_code,
        #       I=매칭상태, J=매칭방법, K=최초등록일, L=최근검증일, M=메모
        for row in match_rows[1:]:
            if len(row) >= 8 and row[0].strip().startswith('KR'):
                isin = row[0].strip()
                match_map[isin] = {
                    'corp_code':    row[7].strip(),   # H열
                    'target_name':  row[5].strip(),   # F열
                    'target_code':  row[6].strip(),   # G열
                    'status':       row[8].strip() if len(row) > 8 else '',
                }
        print(f"  ✅ 주식코드매칭 시트: {len(match_map)}개 종목 로드")
    except Exception as e:
        print(f"  ⚠ 주식코드매칭 시트 로드 실패: {e}")

    # === 3. 주식코드 시트 로드 (주가 알람용 - 기존 유지) ===
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

    # === 4. 매칭 정보 + 주식코드 정보를 holdings에 합치기 ===
    for h in holdings:
        match = match_map.get(h['isin'], {})
        h['corp_code']    = match.get('corp_code', '')
        h['target_name']  = match.get('target_name', '')
        h['target_code']  = match.get('target_code', '')
        h['match_status'] = match.get('status', '')

        # 기존 주식코드 시트는 주가 알람용으로 유지
        codes = stock_map.get(h['isin'], {})
        h['issuer_code'] = codes.get('issuer', '')
        # target_code는 매칭시트의 G열 우선, 없으면 기존 주식코드 시트 사용
        if not h['target_code']:
            h['target_code'] = codes.get('target', '')

    print(f"  ✅ {len(holdings)}개 종목 로드")
    
    # 매칭 상태 통계
    matched = sum(1 for h in holdings if h['corp_code'])
    unmatched = len(holdings) - matched
    print(f"  📊 매칭됨: {matched}개 / 매칭 안 됨: {unmatched}개")
    if unmatched > 0:
        print(f"     ⚠️ 매칭 안 된 종목은 공시 알람에서 제외됩니다.")
    
    return holdings

# ============================================================
# [알람1] 보유 종목 신규 공시 (매 10분, 중복 방지)
# ★ 변경됨: 주식코드매칭 시트의 corp_code 직접 사용
# ============================================================
def check_new_disclosures(holdings, sent_nos: set):
    print(f"\n📢 [알람1] 보유 종목 신규 공시 체크...")
    new_count = 0
    skipped = 0

    for h in holdings:
        # ★ 매칭 시트의 corp_code 직접 사용 (DART API 추가 호출 불필요)
        corp_code = h.get('corp_code', '')
        if not corp_code:
            skipped += 1
            continue

        bond_name   = h['name']  # 예: "어보브반도체 3EB"
        target_name = h.get('target_name', '') or bond_name  # 공시 회사명

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

                # ★ 새 알람 포맷: 채권명 + 공시 회사명 분리 표시
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
# [알람2] PUT/CALL 행사 임박 (KST 오전 9시에만 실행)
# ※ 회차 오류 이슈는 풋콜스케줄 시트 도입 시 별도 수정 예정
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
# [네이버 금융 주가 조회]
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
# [알람4] 주가 5% 이상 변동 (KST 9시~15시, 하루 1번)
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

    holdings = load_holdings()

    if holdings:
        check_new_disclosures(holdings, sent_nos)
        check_put_call_deadline(holdings)
        check_new_issuance(sent_nos)
        check_stock_price(holdings, sent_nos)

    save_sent_nos(sent_nos)
    print(f"\n🏁 완료!")
