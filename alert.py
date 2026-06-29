"""
susung-alert alert.py (v2.0)
- 6종 이벤트 알림 (전환청구시작/종료, 만기, 리픽싱, 풋옵션, 콜옵션)
- 자본변동 즉시 알림 + 일일 요약
- 보유금액 포함
- 기존: 보유종목 공시, 신규 발행, 주가 변동
"""

import os
import re
import json
import time
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

# 텔레그램 명령어 처리 모듈
from telegram_commands import process_telegram_updates


# ============================================================
# [설정]
# ============================================================
DART_KEY = (
    os.environ.get('DART_API_KEY') or
    os.environ.get('DART_KEY') or
    ''
)
SHEET_ID = os.environ.get('SHEET_ID', '1s73BDNtCPe5mOs9EjBE5npEfcaNtYRyWJxRBUmJI-WA')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID', '')

# 보안: 환경변수 누락 시 즉시 실패
if not DART_KEY:
    raise RuntimeError("DART_API_KEY 환경변수가 설정되지 않았습니다")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 설정되지 않았습니다")
if not TELEGRAM_CHAT:
    raise RuntimeError("TELEGRAM_CHAT_ID 환경변수가 설정되지 않았습니다")

DART_BASE = "https://opendart.fss.or.kr/api"
NOW = datetime.utcnow() + timedelta(hours=9)  # KST
TODAY = NOW.strftime('%Y-%m-%d')
KST_H = NOW.hour
IS_WEEKDAY = NOW.weekday() < 5  # 월~금 = True

# ============================================================
# [알림 시점 설정]
# ============================================================
ALERT_DAYS_BY_EVENT = {
    '전환청구시작': [7, 1, 0],
    '전환청구종료': [30, 7, 1],
    '만기': [90, 30, 7, 1],       # D-90 추가됨
    '리픽싱': [7, 1, 0],
    '풋옵션': [7, 1, 0],
    '콜옵션': [7, 1, 0],
}

# 이벤트별 이모지
EVENT_EMOJI = {
    '전환청구시작': '🟦',
    '전환청구종료': '🟦',
    '만기': '🟥',
    '리픽싱': '🟨',
    '풋옵션': '🟢',
    '콜옵션': '🟣',
}

PRICE_THRESHOLD = 5.0
SENT_FILE = 'sent_rcept_nos.json'

# ============================================================
# [시트 이름]
# ============================================================
SHEET_PORTFOLIO = 0
SHEET_SCHEDULE = '풋콜스케줄'
SHEET_CAPITAL_ACTION = '자본변동이력'
SHEET_STOCK_MATCH = '주식코드매칭'
SHEET_STOCK_CODE = '주식코드'


# ============================================================
# [중복 방지: sent_rcept_nos.json]
# - 오늘 날짜만 유지 (어제 이전 자동 정리)
# ============================================================
def load_sent_nos() -> set:
    """오늘 보낸 ID 로드 (날짜별 관리)"""
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
        return set(data.get(TODAY, []))
    except Exception:
        return set()


def save_sent_nos(sent_nos: set):
    """오늘 보낸 ID 저장 (오늘 날짜만 유지)"""
    try:
        with open(SENT_FILE, 'r') as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[TODAY] = list(sent_nos)
    # 오늘 것만 남기기
    data = {k: v for k, v in data.items() if k == TODAY}
    with open(SENT_FILE, 'w') as f:
        json.dump(data, f)
    print(f"  💾 sent_rcept_nos.json 저장: {len(sent_nos)}건")


# ============================================================
# [텔레그램 전송]
# ============================================================
def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print(f"⚠ 텔레그램 설정 없음")
        return False
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
            print(f"   ✅ 전송 완료")
            return True
        else:
            print(f"   ⚠ 전송 실패: {r.status_code}, {r.text[:200]}")
            return False
    except Exception as e:
        print(f"   ⚠ 전송 오류: {e}")
        return False


# ============================================================
# [데이터 로드: 보유 종목, 풋콜스케줄, 자본변동]
# ============================================================
def load_holdings(sh):
    """포트폴리오 시트에서 보유 종목 로드 + 보유금액 포함"""
    print("📋 보유 종목 로드 중...")
    
    ws = sh.get_worksheet(SHEET_PORTFOLIO)
    all_values = ws.get_all_values()
    
    # 헤더에서 보유금액 컬럼 자동 인식
    headers = all_values[0] if all_values else []
    holding_amount_col = None
    for i, h in enumerate(headers):
        if '보유금액' in h or '보유수량' in h:
            holding_amount_col = i
            break
    
    holdings = []
    for row in all_values[1:]:
        if len(row) < 2 or not row[1].strip().startswith('KR'):
            continue
        if not row[0].strip() or row[0].strip() == '-':
            continue
        
        def g(i): return row[i].strip() if len(row) > i else ''
        
        # 보유금액 추출 (컬럼 자동 인식)
        amount = ''
        if holding_amount_col is not None:
            amount = g(holding_amount_col)
        
        holdings.append({
            'name': g(0),
            'isin': g(1),
            'hosu': g(2),
            'bond_type': g(3),
            'holding_amount': amount,
            # 기존 PUT/CALL은 새 시트 기반으로 대체되니까 사용 안 함
        })
    
    # 주식코드매칭 시트
    match_map = {}
    try:
        ws_match = sh.worksheet(SHEET_STOCK_MATCH)
        match_rows = ws_match.get_all_values()
        for row in match_rows[1:]:
            if len(row) >= 8 and row[0].strip().startswith('KR'):
                isin = row[0].strip()
                match_map[isin] = {
                    'corp_code': row[7].strip(),
                    'target_name': row[5].strip(),
                    'target_code': row[6].strip(),
                    'status': row[8].strip() if len(row) > 8 else '',
                }
        print(f"  ✅ 주식코드매칭: {len(match_map)}개")
    except Exception as e:
        print(f"  ⚠ 주식코드매칭 시트 로드 실패: {e}")
    
    # 주식코드 시트
    stock_map = {}
    try:
        ws_stock = sh.worksheet(SHEET_STOCK_CODE)
        stock_rows = ws_stock.get_all_values()
        for row in stock_rows[1:]:
            if len(row) >= 5 and row[1].strip():
                stock_map[row[1].strip()] = {
                    'issuer': row[3].strip(),
                    'target': row[4].strip(),
                }
    except Exception as e:
        print(f"  ⚠ 주식코드 시트 로드 실패: {e}")
    
    # 머지
    for h in holdings:
        match = match_map.get(h['isin'], {})
        h['corp_code'] = match.get('corp_code', '')
        h['target_name'] = match.get('target_name', '')
        h['target_code'] = match.get('target_code', '')
        h['match_status'] = match.get('status', '')
        
        codes = stock_map.get(h['isin'], {})
        h['issuer_code'] = codes.get('issuer', '')
        if not h['target_code']:
            h['target_code'] = codes.get('target', '')
    
    print(f"  ✅ 보유종목 {len(holdings)}개 로드 완료")
    matched = sum(1 for h in holdings if h['corp_code'])
    print(f"     매칭됨 {matched}개 / 미매칭 {len(holdings)-matched}개")
    
    return holdings


def load_schedule(sh):
    """풋콜스케줄 시트 전체 로드"""
    print("📅 풋콜스케줄 로드 중...")
    try:
        ws = sh.worksheet(SHEET_SCHEDULE)
        rows = ws.get_all_values()
    except Exception as e:
        print(f"  ⚠ 풋콜스케줄 시트 로드 실패: {e}")
        return []
    
    # 헤더: [종목코드, 종목명, 이벤트유형, 차수, 시작일, 종료일, 지급일, 비율/금리, 비고]
    schedule = []
    for row in rows[1:]:
        if len(row) < 5:
            continue
        if not row[0].strip().startswith('KR'):
            continue
        
        schedule.append({
            'isin': row[0].strip(),
            'name': row[1].strip(),
            'event_type': row[2].strip(),
            'chasu': row[3].strip() if len(row) > 3 else '',
            'start_date': row[4].strip() if len(row) > 4 else '',
            'end_date': row[5].strip() if len(row) > 5 else '',
            'pay_date': row[6].strip() if len(row) > 6 else '',
            'rate': row[7].strip() if len(row) > 7 else '',
            'note': row[8].strip() if len(row) > 8 else '',
        })
    
    print(f"  ✅ 풋콜스케줄 {len(schedule)}개 이벤트 로드")
    return schedule


def load_capital_actions(sh):
    """자본변동이력 시트 로드"""
    print("📊 자본변동이력 로드 중...")
    try:
        ws = sh.worksheet(SHEET_CAPITAL_ACTION)
        rows = ws.get_all_values()
    except Exception as e:
        print(f"  ℹ 자본변동이력 시트 없음 (정상, 자본변동 없으면 시트 없음)")
        return []
    
    # 헤더: [감지일자, 종목명, 자본변동유형, 공시일자, 보고서명, DART링크]
    actions = []
    for row in rows[1:]:
        if len(row) < 6:
            continue
        actions.append({
            'detected_date': row[0].strip(),
            'stock_name': row[1].strip(),
            'action_type': row[2].strip(),
            'disclosure_date': row[3].strip(),
            'report_name': row[4].strip(),
            'link': row[5].strip(),
        })
    
    print(f"  ✅ 자본변동 {len(actions)}건 로드")
    return actions


def find_holding_for_event(event_isin, holdings):
    """이벤트의 ISIN으로 보유 정보 찾기"""
    for h in holdings:
        if h['isin'] == event_isin:
            return h
    return None


def find_holding_by_name(stock_name, holdings):
    """종목명으로 보유 정보 찾기 (자본변동용)"""
    # 정확 매칭
    for h in holdings:
        if h['name'] == stock_name:
            return h
    # 부분 매칭 (자본변동은 공시 대상 회사명이라 다를 수 있음)
    for h in holdings:
        # holdings는 채권 종목명 (예: 천보6CB), 자본변동은 회사명 (예: 천보)
        # 채권명에서 숫자 + CB/EB/BW 떼고 비교
        bond_name_clean = re.sub(r'\s*\d+(?:CB|EB|BW).*$', '', h['name']).strip()
        if bond_name_clean == stock_name:
            return h
        if stock_name in h['name'] or h['name'] in stock_name:
            return h
    return None


def format_holding_info(holding):
    """보유 정보를 알림 메시지용으로 포맷"""
    if not holding:
        return ''
    amount = holding.get('holding_amount', '').strip()
    if not amount or amount in ('-', '0'):
        return ''
    return f"\n💰 보유: <b>{amount}억원</b>"


# ============================================================
# [알람1] 보유 종목 신규 공시 (기존 유지)
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
        
        bond_name = h['name']
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
                rpt = item.get('report_nm', '')
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
                
                holding_info = format_holding_info(h)
                
                msg = (
                    f"📢 <b>보유종목 신규 공시</b>\n\n"
                    f"🔹 <b>{bond_name}</b>\n"
                    f"🏢 공시: {target_name}\n"
                    f"📄 {rpt}\n"
                    f"📅 {TODAY}"
                    f"{holding_info}\n"
                    f"🔗 <a href='{link}'>DART 바로가기</a>"
                )
                send_telegram(msg)
                sent_nos.add(rcept_no)
                new_count += 1
                print(f"   📄 {bond_name} → {target_name}: {rpt}")
                time.sleep(0.5)
        except Exception as e:
            print(f"   ⚠ {bond_name} 조회 실패: {e}")
        
        time.sleep(0.3)
    
    print(f"  → 신규 공시 {new_count}건 전송 (매칭 안 된 {skipped}개 스킵)")


# ============================================================
# [알람2 NEW] 6종 이벤트 D-day 알림 (풋콜스케줄 시트 기반)
# ============================================================
def check_event_alerts(schedule, holdings, sent_nos: set):
    """
    풋콜스케줄 시트의 6종 이벤트를 체크해서 D-day 알림 발송
    - 전환청구시작: D-7, D-1, D-day (start_date 기준)
    - 전환청구종료: D-30, D-7, D-1 (start_date 기준, 종료일이 start_date에 들어있음)
    - 만기: D-90, D-30, D-7, D-1 (start_date 기준)
    - 리픽싱: D-7, D-1, D-day (start_date 기준)
    - 풋옵션: D-7, D-1, D-day (start_date 기준 = 청구 시작일)
    - 콜옵션: D-7, D-1, D-day (start_date 기준)
    """
    # 알림 시점은 KST 09시에만 (하루 한 번)
    if KST_H != 9:
        print(f"\n⏰ [알람2] 6종 이벤트 알림 스킵 (KST {KST_H}시, 09시에만 발송)")
        return
    
    print(f"\n⏰ [알람2] 6종 이벤트 D-day 알림 체크...")
    today_dt = datetime.strptime(TODAY, '%Y-%m-%d')
    
    sent_count = 0
    
    for event in schedule:
        event_type = event['event_type']
        alert_days = ALERT_DAYS_BY_EVENT.get(event_type)
        if not alert_days:
            continue
        
        # 기준일자: start_date (모든 이벤트의 시작/예정일이 여기 들어있음)
        date_str = event['start_date']
        if not date_str or date_str in ('-', ''):
            continue
        
        try:
            target_dt = datetime.strptime(date_str, '%Y-%m-%d')
        except Exception:
            continue
        
        diff = (target_dt - today_dt).days
        if diff not in alert_days:
            continue
        
        # 중복 방지 키
        alert_key = f"EVT_{event['isin']}_{event_type}_{event['chasu']}_{date_str}_D{diff}"
        if alert_key in sent_nos:
            continue
        
        # 보유 정보
        holding = find_holding_for_event(event['isin'], holdings)
        holding_info = format_holding_info(holding)
        
        # 메시지 빌드
        emoji = EVENT_EMOJI.get(event_type, '📅')
        chasu_str = f" {event['chasu']}" if event['chasu'] else ''
        rate_str = f"\n📊 비율/금리: {event['rate']}" if event['rate'] else ''
        
        # D-day 라벨
        if diff == 0:
            d_label = "<b>D-DAY 🚨</b>"
        elif diff == 1:
            d_label = "<b>D-1</b> 내일"
        else:
            d_label = f"<b>D-{diff}</b>"
        
        # 추가 정보 (PUT/CALL은 종료일, 지급일 표시)
        extra_info = ''
        if event_type in ('풋옵션', '콜옵션'):
            if event['end_date']:
                extra_info += f"\n📅 종료일: {event['end_date']}"
            if event['pay_date']:
                extra_info += f"\n💵 지급일: {event['pay_date']}"
        
        note_info = f"\n📝 {event['note']}" if event['note'] else ''
        
        msg = (
            f"{emoji} <b>{event_type}{chasu_str} {d_label}</b>\n\n"
            f"🔹 <b>{event['name']}</b>\n"
            f"📅 일자: {date_str}"
            f"{rate_str}"
            f"{extra_info}"
            f"{holding_info}"
            f"{note_info}\n"
            f"🆔 <code>{event['isin']}</code>"
        )
        
        if send_telegram(msg):
            sent_nos.add(alert_key)
            sent_count += 1
            print(f"   {emoji} {event['name']} {event_type}{chasu_str}: D-{diff}")
        time.sleep(0.3)
    
    print(f"  → {sent_count}건 알림 전송")


# ============================================================
# [알람3] 신규 CB/EB/BW 발행결정 (기존 유지)
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
            rpt = item.get('report_nm', '')
            rcept_no = item.get('rcept_no', '')
            
            if '[첨부정정]' in rpt or '[첨부추가]' in rpt:
                continue
            if not any(kw in rpt for kw in issue_kws):
                continue
            if not rcept_no or rcept_no in sent_nos:
                continue
            
            corp_name = item.get('corp_name', '')
            link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
            
            if '전환사채' in rpt: btype = 'CB'
            elif '교환사채' in rpt: btype = 'EB'
            else: btype = 'BW'
            
            msg = (
                f"🔍 <b>신규 {btype} 발행결정</b>\n\n"
                f"🔹 <b>{corp_name}</b> [{btype}]\n"
                f"📄 {rpt}\n"
                f"📅 {TODAY}\n"
                f"🔗 <a href='{link}'>DART 바로가기</a>"
            )
            send_telegram(msg)
            sent_nos.add(rcept_no)
            new_count += 1
            print(f"   📄 {corp_name}: {rpt}")
            time.sleep(0.5)
    except Exception as e:
        print(f"  ⚠ 발행결정 조회 실패: {e}")
    
    print(f"  → 신규 발행결정 {new_count}건 전송")


# ============================================================
# [알람4 NEW] 자본변동 즉시 알림 + 일일 요약
# ============================================================
def check_capital_actions(capital_actions, holdings, sent_nos: set):
    """
    자본변동이력 시트에 새로 추가된 행을 감지해서 알림
    - 즉시 알림: 매 실행 시 새로운 자본변동 발견하면 즉시 발송
    - 일일 요약: KST 09시에 전일 자본변동 종합 요약 발송
    """
    print(f"\n💥 [알람4] 자본변동 체크...")
    
    if not capital_actions:
        print(f"  ℹ 자본변동 없음")
        return
    
    # === 즉시 알림 ===
    immediate_count = 0
    for action in capital_actions:
        # 중복 방지 키 (link 기준)
        alert_key = f"CA_{action['link']}"
        if alert_key in sent_nos:
            continue
        
        # 자본변동 유형별 이모지
        type_emoji = {
            '주식병합': '⚠️📉',
            '액면병합': '⚠️📉',
            '주식분할': '⚠️📈',
            '액면분할': '⚠️📈',
            '무상증자': '🆕',
            '유상증자': '💰',
            '감자': '⚠️',
            '주식배당': '🎁',
            '액면가변경': '⚠️',
        }
        emoji = type_emoji.get(action['action_type'], '🔔')
        
        # 보유 정보
        holding = find_holding_by_name(action['stock_name'], holdings)
        holding_info = format_holding_info(holding)
        bond_name_info = f"\n🔹 보유 채권: <b>{holding['name']}</b>" if holding else ''
        
        msg = (
            f"{emoji} <b>자본변동 감지!</b>\n\n"
            f"🏢 발행사: <b>{action['stock_name']}</b>"
            f"{bond_name_info}\n"
            f"📌 유형: <b>{action['action_type']}</b>\n"
            f"📅 공시일: {action['disclosure_date']}\n"
            f"📄 {action['report_name']}"
            f"{holding_info}\n"
            f"🔗 <a href='{action['link']}'>DART 바로가기</a>\n\n"
            f"⚠️ 행사가액 재계산 필요할 수 있음"
        )
        
        if send_telegram(msg):
            sent_nos.add(alert_key)
            immediate_count += 1
            print(f"   {emoji} {action['stock_name']}: {action['action_type']}")
        time.sleep(0.3)
    
    print(f"  → 즉시 알림 {immediate_count}건 전송")
    
    # === 일일 요약 (KST 09시) ===
    if KST_H != 9:
        return
    
    # 어제 감지된 자본변동만 모으기
    yesterday = (NOW - timedelta(days=1)).strftime('%Y-%m-%d')
    yesterday_actions = [a for a in capital_actions if a['detected_date'] == yesterday]
    
    if not yesterday_actions:
        print(f"  ℹ 어제({yesterday}) 자본변동 없음 (일일 요약 생략)")
        return
    
    summary_key = f"CA_SUMMARY_{yesterday}"
    if summary_key in sent_nos:
        print(f"  ℹ 어제 요약 이미 발송됨")
        return
    
    # 요약 메시지 빌드
    by_type = {}
    for a in yesterday_actions:
        by_type.setdefault(a['action_type'], []).append(a)
    
    lines = [f"📊 <b>자본변동 일일 요약</b> ({yesterday})\n"]
    for action_type, items in by_type.items():
        type_emoji = {'주식병합': '📉', '액면병합': '📉', '주식분할': '📈',
                      '액면분할': '📈', '무상증자': '🆕', '유상증자': '💰',
                      '감자': '⚠️', '주식배당': '🎁'}.get(action_type, '🔔')
        lines.append(f"\n{type_emoji} <b>{action_type}</b> ({len(items)}건)")
        for a in items:
            holding = find_holding_by_name(a['stock_name'], holdings)
            holding_str = f" [{holding['holding_amount']}억]" if holding and holding.get('holding_amount') else ''
            lines.append(f"  • {a['stock_name']}{holding_str}")
    
    lines.append(f"\n📌 총 {len(yesterday_actions)}건")
    msg = '\n'.join(lines)
    
    if send_telegram(msg):
        sent_nos.add(summary_key)
        print(f"  → 일일 요약 전송 (어제 {len(yesterday_actions)}건)")


# ============================================================
# [네이버 주가 조회] (기존 유지)
# ============================================================
def get_naver_stock_price(stock_code: str) -> dict:
    try:
        url = f"https://finance.naver.com/item/main.naver?code={stock_code}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, headers=headers, timeout=10)
        r.encoding = 'euc-kr'
        html = r.text
        
        cur_m = re.search(r'<p class="no_today">.*?<span class="blind">([0-9,]+)</span>', html, re.DOTALL)
        prev_m = re.search(r'전일\s*<em[^>]*>([0-9,]+)</em>', html)
        
        if cur_m and prev_m:
            current = int(cur_m.group(1).replace(',', ''))
            prev_close = int(prev_m.group(1).replace(',', ''))
            if prev_close == 0:
                return {}
            change_pct = (current - prev_close) / prev_close * 100
            return {'current': current, 'prev_close': prev_close, 'change_pct': round(change_pct, 2)}
    except Exception as e:
        print(f"   ⚠ 주가 조회 실패 ({stock_code}): {e}")
    return {}


# ============================================================
# [알람5] 주가 5% 이상 변동 (기존 유지)
# ============================================================
def check_stock_price(holdings, sent_nos: set):
    if not (9 <= KST_H <= 15):
        print(f"\n📈 [알람5] 주가 변동 스킵 (KST {KST_H}시, 장시간 외)")
        return
    if not IS_WEEKDAY:
        print(f"\n📈 [알람5] 주가 변동 스킵 (주말)")
        return
    
    print(f"\n📈 [알람5] 주가 5% 변동 체크 (기준: {PRICE_THRESHOLD}%)...")
    
    alerted_today = set(s for s in sent_nos if s.startswith('PRICE_'))
    checked_codes = set()
    
    for h in holdings:
        for code_type, code in [('발행사', h.get('issuer_code', '')), 
                                 ('교환대상', h.get('target_code', ''))]:
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
            print(f"   📊 {h['name']} ({code}): {price['current']:,}원 ({chg:+.2f}%)")
            
            if abs(chg) >= PRICE_THRESHOLD:
                direction = '📈' if chg > 0 else '📉'
                
                holding_info = format_holding_info(h)
                
                msg = (
                    f"{direction} <b>주가 급변동 알람</b>\n\n"
                    f"🔹 <b>{h['name']}</b> ({h['bond_type']} {h['hosu']}회)\n"
                    f"🏷 {code_type} 주식 [{code}]\n"
                    f"💰 현재가: {price['current']:,}원\n"
                    f"📊 전일 대비: <b>{chg:+.2f}%</b>\n"
                    f"   (전일 종가: {price['prev_close']:,}원)"
                    f"{holding_info}"
                )
                send_telegram(msg)
                sent_nos.add(alert_key)
                print(f"   {direction} {h['name']} ({code}): {chg:+.2f}% → 알람 전송")
            time.sleep(0.5)


# ============================================================
# [메인 실행]
# ============================================================
if __name__ == '__main__':
    print(f"🤖 susung-alert v2.0 시작 (KST {TODAY} {KST_H:02d}시)")
    print(f"   평일: {IS_WEEKDAY}")
    
    sent_nos = load_sent_nos()
    print(f"   📋 기존 전송 기록: {len(sent_nos)}건")
    
    # === Google Sheets 연결 ===
    creds_json = json.loads(os.environ.get('GCP_SERVICE_ACCOUNT_KEY'))
    creds = Credentials.from_service_account_info(creds_json, scopes=[
        'https://www.googleapis.com/auth/spreadsheets',
        'https://www.googleapis.com/auth/drive',
    ])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    
    # === 데이터 로드 ===
    holdings = load_holdings(sh)
    schedule = load_schedule(sh)
    capital_actions = load_capital_actions(sh)
    
    if not holdings:
        print("⚠ 보유 종목 없음. 종료.")
        exit(0)
    
    # === 알람 실행 ===
    # 알람1: 보유 종목 신규 공시 (매 실행)
    check_new_disclosures(holdings, sent_nos)
    
    # 알람2: 6종 이벤트 D-day (KST 09시만)
    check_event_alerts(schedule, holdings, sent_nos)
    
    # 알람3: 신규 발행결정 (매 실행)
    check_new_issuance(sent_nos)
    
    # 알람4: 자본변동 (즉시 + 일일요약 09시)
    check_capital_actions(capital_actions, holdings, sent_nos)
    
    # 알람5: 주가 변동 (장중만)
    check_stock_price(holdings, sent_nos)
    
    # === 저장 ===
    save_sent_nos(sent_nos)
    
    # === 텔레그램 명령어 처리는 Cloudflare Worker가 담당 ===
    # process_telegram_updates(sh, TELEGRAM_TOKEN)  ← 비활성화 (Worker 사용)
    
    print(f"\n🏁 완료!")
