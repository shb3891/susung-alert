"""
telegram_commands.py v2.0 — 텔레그램 봇 명령어 처리 모듈

기존 8개 명령어 (매칭 관리):
- /match ISIN 주식코드 종목명...  : 수동매칭 등록/갱신
- /status ISIN                     : 매칭 정보 조회
- /list_failed                     : 매칭 실패 목록
- /list_alias                      : 별칭매칭 목록
- /list_manual                     : 수동확정 목록
- /aliases                         : 별칭사전 목록
- /add_alias 원본 DART표기         : 별칭 추가
- /help                            : 도움말

신규 추가 5개 명령어 (보유 정보):
- /list                            : 보유 종목 전체 목록
- /total                           : 보유 통계 요약
- /upcoming                        : 7일 내 다가오는 이벤트
- /upcoming30                      : 30일 내 다가오는 이벤트
- /capital                         : 최근 자본변동 내역

신규 자유 텍스트 검색:
- <종목명>: 종목 상세 정보 (예: "천보", "디앤디파마텍")
- <ISIN>: ISIN으로 직접 검색

사용법: alert.py에서 호출
    from telegram_commands import process_telegram_updates
    process_telegram_updates(sh, telegram_token)
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta


# 마지막 처리한 update_id 저장 파일 (중복 처리 방지)
LAST_UPDATE_FILE = 'last_update_id.json'

# 시트 이름
MATCH_SHEET = '주식코드매칭'
ALIAS_SHEET = '별칭사전'
PORTFOLIO_SHEET = 0  # 첫 번째 시트 (포트폴리오)
SCHEDULE_SHEET = '풋콜스케줄'
CAPITAL_SHEET = '자본변동이력'
STOCK_CODE_SHEET = '주식코드'

# 매칭상태 표시
STATUS_DISPLAY = {
    'AUTO':   '✅ 자동매칭',
    'ALIAS':  '⚠️ 별칭매칭(검토)',
    'MANUAL': '🔒 수동확정',
    'FAILED': '❌ 매칭실패',
}

# 이벤트 이모지
EVENT_EMOJI = {
    '전환청구시작': '🟦',
    '전환청구종료': '🟦',
    '만기': '🟥',
    '리픽싱': '🟨',
    '풋옵션': '🟢',
    '콜옵션': '🟣',
}


# ============================================================
# [last_update_id 로드/저장]
# ============================================================
def load_last_update_id():
    try:
        with open(LAST_UPDATE_FILE, 'r') as f:
            return json.load(f).get('last_update_id', 0)
    except Exception:
        return 0


def save_last_update_id(update_id):
    try:
        with open(LAST_UPDATE_FILE, 'w') as f:
            json.dump({'last_update_id': update_id}, f)
    except Exception as e:
        print(f"  ⚠ last_update_id 저장 실패: {e}")


# ============================================================
# [텔레그램 메시지 전송]
# ============================================================
def send_reply(token, chat_id, message, reply_to=None):
    """텔레그램 메시지 전송 (답장 형식 가능)."""
    try:
        payload = {
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }
        if reply_to:
            payload['reply_to_message_id'] = reply_to
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload, timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"  ⚠ 텔레그램 전송 실패: {e}")
        return False


# ============================================================
# [텔레그램 메시지 조회 (Polling)]
# ============================================================
def get_telegram_updates(token, offset=0):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={'offset': offset, 'timeout': 0, 'limit': 50},
            timeout=15,
        )
        data = r.json()
        if not data.get('ok'):
            print(f"  ⚠ getUpdates 실패: {data}")
            return []
        return data.get('result', [])
    except Exception as e:
        print(f"  ⚠ getUpdates 오류: {e}")
        return []


# ============================================================
# [명령어 파싱]
# ============================================================
def parse_command(text):
    """텍스트에서 명령어 추출. 명령어 아니면 (None, None) 반환."""
    text = text.strip()
    if not text.startswith('/'):
        return None, None
    
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if '@' in cmd:
        cmd = cmd.split('@')[0]
    args = parts[1] if len(parts) > 1 else ''
    return cmd, args


def is_isin(text):
    """ISIN 형식 체크"""
    return bool(re.match(r'^KR\d[A-Z0-9]{9}$', text.strip().upper()))


# ============================================================
# [숫자 포맷 헬퍼]
# ============================================================
def fmt_amount(val):
    """억원 단위 포맷"""
    try:
        v = float(str(val).replace(',', ''))
        if v == 0:
            return '0'
        return f"{v:.1f}".rstrip('0').rstrip('.') if v != int(v) else f"{int(v):,}"
    except:
        return str(val) if val else '-'


def fmt_pct(val):
    """수익률 포맷"""
    try:
        v = float(str(val).replace(',', '').replace('%', ''))
        return f"{v:+.2f}%"
    except:
        return str(val) if val else '-'


def fmt_price(val):
    """가격 포맷 (콤마)"""
    try:
        v = int(float(str(val).replace(',', '')))
        return f"{v:,}원"
    except:
        return str(val) if val else '-'


# ============================================================
# [DART corp_code 조회 (보조)]
# ============================================================
_dart_corp_cache = None

def lookup_dart_corp_code(stock_code):
    """주식코드로 DART corp_code 조회 (캐시)."""
    global _dart_corp_cache
    
    if _dart_corp_cache is None:
        _dart_corp_cache = _load_dart_codes()
    
    return _dart_corp_cache.get(stock_code, '')


def _load_dart_codes():
    """DART corpCode.xml 로드 (최초 1회)."""
    import zipfile
    import io
    import xml.etree.ElementTree as ET
    
    dart_key = os.environ.get('DART_API_KEY', '')
    if not dart_key:
        return {}
    
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/corpCode.xml",
            params={'crtfc_key': dart_key}, timeout=30,
        )
        z = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(z.read('CORPCODE.xml'))
        codes = {}
        for item in root.findall('.//list'):
            corp_code = item.findtext('corp_code', '').strip()
            stock_code = item.findtext('stock_code', '').strip()
            if stock_code and len(stock_code) == 6 and corp_code:
                codes[stock_code] = corp_code
        return codes
    except Exception as e:
        print(f"  ⚠ DART 코드 로드 실패: {e}")
        return {}


# ============================================================
# [네이버 주가 조회]
# ============================================================
def get_naver_stock_price(stock_code):
    """네이버 주가 조회 (현재가, 전일대비)"""
    if not stock_code or len(stock_code) != 6:
        return None
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
                return None
            change_pct = (current - prev_close) / prev_close * 100
            return {
                'current': current,
                'prev_close': prev_close,
                'change_pct': round(change_pct, 2),
            }
    except Exception as e:
        print(f"   ⚠ 주가 조회 실패 ({stock_code}): {e}")
    return None


# ============================================================
# [기존 핸들러: /match]
# ============================================================
def handle_match(sh, args, chat_id, message_id, token):
    """
    형식: /match ISIN 주식코드 종목명(공백포함가능)
    예: /match KR60156S1G12 435570 에르코스 농업회사법인
    """
    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        send_reply(token, chat_id,
            "❌ 형식 오류\n\n사용법:\n<code>/match ISIN 주식코드 종목명</code>\n\n"
            "예시:\n<code>/match KR60156S1G12 435570 에르코스 농업회사법인</code>",
            reply_to=message_id)
        return
    
    isin, stock_code, corp_name = parts[0].strip(), parts[1].strip(), parts[2].strip()
    
    if not re.match(r'^KR\d[A-Z0-9]{9}$', isin):
        send_reply(token, chat_id,
            f"❌ 잘못된 ISIN 형식: <code>{isin}</code>\n"
            "ISIN은 KR로 시작하는 12자리여야 합니다.",
            reply_to=message_id)
        return
    
    if not re.match(r'^\d{6}$', stock_code):
        send_reply(token, chat_id,
            f"❌ 잘못된 주식코드: <code>{stock_code}</code>\n"
            "주식코드는 숫자 6자리여야 합니다.",
            reply_to=message_id)
        return
    
    if not corp_name:
        send_reply(token, chat_id, "❌ 종목명이 필요합니다.", reply_to=message_id)
        return
    
    dart_corp_code = lookup_dart_corp_code(stock_code)
    today = datetime.now().strftime('%Y-%m-%d')
    
    try:
        ws = sh.worksheet(MATCH_SHEET)
        all_rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    existing_row = None
    bond_name_existing = ''
    
    for i, row in enumerate(all_rows[1:], start=2):
        if row and row[0].strip() == isin:
            existing_row = i
            bond_name_existing = row[1].strip() if len(row) > 1 else ''
            break
    
    if existing_row:
        update_data = [
            corp_name,
            stock_code,
            dart_corp_code,
            STATUS_DISPLAY['MANUAL'],
            'MANUAL_INPUT',
        ]
        ws.update([update_data], range_name=f'F{existing_row}:J{existing_row}')
        ws.update([[today]], range_name=f'L{existing_row}')
        
        send_reply(token, chat_id,
            f"✅ <b>매칭 정보 업데이트 완료</b>\n\n"
            f"📌 ISIN: <code>{isin}</code>\n"
            f"📌 채권: {bond_name_existing or '(미확인)'}\n"
            f"🏢 공시대상: <b>{corp_name}</b>\n"
            f"📊 주식코드: <code>{stock_code}</code>\n"
            f"📊 DART코드: <code>{dart_corp_code or '(조회실패)'}</code>\n"
            f"📌 상태: 🔒 수동확정",
            reply_to=message_id)
    else:
        new_row = [
            isin, '', '', '', '',
            corp_name, stock_code, dart_corp_code,
            STATUS_DISPLAY['MANUAL'], 'MANUAL_INPUT',
            today, today, f'/match 명령으로 등록',
        ]
        next_row = len(all_rows) + 1
        ws.update([new_row], range_name=f'A{next_row}:M{next_row}')
        
        send_reply(token, chat_id,
            f"✅ <b>신규 매칭 등록 완료</b>\n\n"
            f"📌 ISIN: <code>{isin}</code>\n"
            f"🏢 공시대상: <b>{corp_name}</b>\n"
            f"📊 주식코드: <code>{stock_code}</code>\n"
            f"📊 DART코드: <code>{dart_corp_code or '(조회실패)'}</code>\n"
            f"📌 상태: 🔒 수동확정",
            reply_to=message_id)


# ============================================================
# [기존 핸들러: /status]
# ============================================================
def handle_status(sh, args, chat_id, message_id, token):
    isin = args.strip().upper()
    if not re.match(r'^KR\d[A-Z0-9]{9}$', isin):
        send_reply(token, chat_id,
            f"❌ ISIN 형식 오류: <code>{isin}</code>\n사용법: <code>/status KR...</code>",
            reply_to=message_id)
        return
    
    try:
        ws = sh.worksheet(MATCH_SHEET)
        rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    for row in rows[1:]:
        if row and row[0].strip() == isin:
            def g(i): return row[i].strip() if len(row) > i else ''
            msg = (
                f"📋 <b>매칭 정보 조회</b>\n\n"
                f"📌 ISIN: <code>{isin}</code>\n"
                f"📌 채권명: {g(1) or '(없음)'}\n"
                f"📌 종류: {g(2) or '(없음)'} {g(3)}\n\n"
                f"🏢 공시대상: <b>{g(5) or '(미매칭)'}</b>\n"
                f"📊 주식코드: <code>{g(6) or '(미매칭)'}</code>\n"
                f"📊 DART코드: <code>{g(7) or '(미매칭)'}</code>\n\n"
                f"📌 상태: {g(8)}\n"
                f"📌 방법: {g(9)}\n"
                f"📅 최초등록: {g(10)}\n"
                f"📅 최근검증: {g(11)}\n"
            )
            if g(12):
                msg += f"📝 메모: {g(12)}"
            send_reply(token, chat_id, msg, reply_to=message_id)
            return
    
    send_reply(token, chat_id,
        f"❌ ISIN을 찾을 수 없습니다: <code>{isin}</code>\n"
        f"/match 명령으로 새로 등록하세요.",
        reply_to=message_id)


# ============================================================
# [기존 핸들러: /list_*]
# ============================================================
def handle_list_status(sh, args, chat_id, message_id, token, filter_keyword, list_name):
    try:
        ws = sh.worksheet(MATCH_SHEET)
        rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    matched = []
    for row in rows[1:]:
        if len(row) < 9:
            continue
        status = row[8].strip()
        if filter_keyword in status:
            isin = row[0].strip()
            bond_name = row[1].strip() or '(채권명없음)'
            target = row[5].strip() or '(미매칭)'
            matched.append((isin, bond_name, target))
    
    if not matched:
        msg = f"✨ <b>{list_name}</b>\n\n해당 종목 없음"
    else:
        lines = [f"📋 <b>{list_name}</b> ({len(matched)}건)\n"]
        for isin, bond, target in matched[:30]:
            lines.append(f"• {bond} (<code>{isin}</code>)")
            lines.append(f"  └ {target}")
        if len(matched) > 30:
            lines.append(f"\n... 외 {len(matched) - 30}건")
        msg = '\n'.join(lines)
    
    send_reply(token, chat_id, msg, reply_to=message_id)


def handle_list_failed(sh, args, chat_id, message_id, token):
    handle_list_status(sh, args, chat_id, message_id, token, '❌', '매칭 실패 종목')


def handle_list_alias(sh, args, chat_id, message_id, token):
    handle_list_status(sh, args, chat_id, message_id, token, '⚠️', '별칭매칭 종목 (검토 권장)')


def handle_list_manual(sh, args, chat_id, message_id, token):
    handle_list_status(sh, args, chat_id, message_id, token, '🔒', '수동확정 종목')


# ============================================================
# [기존 핸들러: /aliases, /add_alias]
# ============================================================
def handle_aliases(sh, args, chat_id, message_id, token):
    try:
        ws = sh.worksheet(ALIAS_SHEET)
        rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    aliases = []
    for row in rows[1:]:
        if len(row) >= 2 and row[0].strip() and row[1].strip():
            aliases.append((row[0].strip(), row[1].strip(),
                          row[2].strip() if len(row) > 2 else '',
                          row[3].strip() if len(row) > 3 else ''))
    
    if not aliases:
        msg = "📚 <b>별칭사전</b>\n\n등록된 별칭 없음"
    else:
        lines = [f"📚 <b>별칭사전</b> ({len(aliases)}개)\n"]
        for orig, dart, method, note in aliases[:40]:
            lines.append(f"• {orig} → <b>{dart}</b>")
            if note:
                lines.append(f"  └ <i>{note}</i>")
        if len(aliases) > 40:
            lines.append(f"\n... 외 {len(aliases) - 40}개")
        msg = '\n'.join(lines)
    
    send_reply(token, chat_id, msg, reply_to=message_id)


def handle_add_alias(sh, args, chat_id, message_id, token):
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        send_reply(token, chat_id,
            "❌ 형식 오류\n\n사용법:\n<code>/add_alias 원본표기 DART표기</code>\n\n"
            "예시:\n<code>/add_alias 케이씨그린홀딩스 KC그린홀딩스</code>",
            reply_to=message_id)
        return
    
    original = parts[0].strip()
    dart = parts[1].strip()
    
    try:
        ws = sh.worksheet(ALIAS_SHEET)
        rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].strip() == original:
            existing = row[1].strip() if len(row) > 1 else ''
            send_reply(token, chat_id,
                f"⚠️ 이미 등록된 별칭:\n\n<b>{original}</b> → <b>{existing}</b>",
                reply_to=message_id)
            return
    
    today = datetime.now().strftime('%Y-%m-%d')
    new_row = [original, dart, '/add_alias 명령', f'추가일: {today}']
    next_row = len(rows) + 1
    ws.update([new_row], range_name=f'A{next_row}:D{next_row}')
    
    send_reply(token, chat_id,
        f"✅ <b>별칭 추가 완료</b>\n\n"
        f"<b>{original}</b> → <b>{dart}</b>\n\n"
        f"다음 매칭/재검증부터 자동 적용됩니다.",
        reply_to=message_id)


# ============================================================
# [포트폴리오 데이터 로드 헬퍼]
# ============================================================
def load_portfolio_data(sh):
    """
    포트폴리오 시트 전체 로드 (컬럼 자동 인식)
    
    Returns: list of dict
    """
    try:
        ws = sh.get_worksheet(PORTFOLIO_SHEET)
        all_values = ws.get_all_values()
    except Exception as e:
        print(f"⚠ 포트폴리오 로드 실패: {e}")
        return []
    
    if len(all_values) < 2:
        return []
    
    headers = all_values[0]
    
    # 헤더 인덱스 매핑 자동 탐색
    col_idx = {}
    for i, h in enumerate(headers):
        h_clean = h.strip()
        if h_clean in ('종목명',) and 'name' not in col_idx:
            col_idx['name'] = i
        elif h_clean in ('ISIN', '채권ISIN', '종목코드') and 'isin' not in col_idx:
            col_idx['isin'] = i
        elif h_clean == '회차':
            col_idx['hosu'] = i
        elif h_clean == '종류':
            col_idx['bond_type'] = i
        elif '발행일' in h_clean and 'issue_date' not in col_idx:
            col_idx['issue_date'] = i
        elif '만기일' in h_clean:
            col_idx['maturity'] = i
        elif h_clean.lower() == 'coupon' or h_clean == '쿠폰':
            col_idx['coupon'] = i
        elif h_clean.upper() == 'YTM':
            col_idx['ytm'] = i
        elif h_clean.upper() == 'YTC':
            col_idx['ytc'] = i
        elif '행사가액' in h_clean:
            col_idx['exercise_price'] = i
        elif '리픽싱플로어' in h_clean or '플로어' in h_clean:
            col_idx['refix_floor'] = i
        elif '전환청구시작' in h_clean or h_clean == 'K':
            col_idx['xrc_begin'] = i
        elif '전환청구종료' in h_clean:
            col_idx['xrc_end'] = i
        elif '발행사 주식코드' in h_clean or '발행사주식코드' in h_clean:
            col_idx['issuer_code'] = i
        elif '교환대상 회사명' in h_clean or '교환대상회사명' in h_clean:
            col_idx['target_name'] = i
        elif '교환대상 주식코드' in h_clean or '교환대상주식코드' in h_clean:
            col_idx['target_code'] = i
        elif '보유금액' in h_clean or '보유수량' in h_clean:
            col_idx['amount'] = i
        elif '취득가' in h_clean:
            col_idx['acq'] = i
        elif '시가평가액' in h_clean or '평가액' in h_clean:
            col_idx['eval'] = i
        elif '수익률' in h_clean:
            col_idx['return'] = i
    
    # 기본값 (못 찾은 경우)
    col_idx.setdefault('name', 0)
    col_idx.setdefault('isin', 1)
    
    portfolio = []
    for row in all_values[1:]:
        if len(row) <= col_idx.get('isin', 1):
            continue
        isin = row[col_idx['isin']].strip() if col_idx.get('isin') is not None else ''
        if not isin.startswith('KR'):
            continue
        
        def g(key):
            idx = col_idx.get(key)
            if idx is None or len(row) <= idx:
                return ''
            return row[idx].strip()
        
        portfolio.append({
            'name': g('name'),
            'isin': isin,
            'hosu': g('hosu'),
            'bond_type': g('bond_type'),
            'issue_date': g('issue_date'),
            'maturity': g('maturity'),
            'coupon': g('coupon'),
            'ytm': g('ytm'),
            'ytc': g('ytc'),
            'exercise_price': g('exercise_price'),
            'refix_floor': g('refix_floor'),
            'xrc_begin': g('xrc_begin'),
            'xrc_end': g('xrc_end'),
            'issuer_code': g('issuer_code'),
            'target_name': g('target_name'),
            'target_code': g('target_code'),
            'amount': g('amount'),
            'acq': g('acq'),
            'eval': g('eval'),
            'return': g('return'),
        })
    
    return portfolio


def load_schedule_data(sh):
    """풋콜스케줄 시트 로드"""
    try:
        ws = sh.worksheet(SCHEDULE_SHEET)
        rows = ws.get_all_values()
    except Exception:
        return []
    
    schedule = []
    for row in rows[1:]:
        if len(row) < 5 or not row[0].strip().startswith('KR'):
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
    return schedule


def load_capital_data(sh):
    """자본변동이력 시트 로드"""
    try:
        ws = sh.worksheet(CAPITAL_SHEET)
        rows = ws.get_all_values()
    except Exception:
        return []
    
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
    return actions


# ============================================================
# [종목 검색 함수]
# ============================================================
def search_portfolio(query, portfolio):
    """
    종목명/ISIN으로 검색
    
    Returns: list of matched items
    """
    query = query.strip()
    query_upper = query.upper()
    
    # 1. ISIN 직접 매칭
    if is_isin(query):
        return [p for p in portfolio if p['isin'].upper() == query_upper]
    
    # 2. 정확 종목명 매칭
    exact = [p for p in portfolio if p['name'] == query]
    if exact:
        return exact
    
    # 3. 부분 매칭 (종목명, 교환대상명)
    partial = []
    for p in portfolio:
        if query in p['name'] or query in p.get('target_name', ''):
            partial.append(p)
    
    return partial


# ============================================================
# [종목 상세 출력]
# ============================================================
def format_stock_detail(stock, schedule, capital_actions, today_str):
    """한 종목의 상세 정보를 텔레그램 메시지로 포맷"""
    lines = []
    
    # 헤더
    bond_type = stock.get('bond_type', '-')
    hosu = stock.get('hosu', '-')
    name = stock['name']
    lines.append(f"🔹 <b>{name}</b> ({bond_type} {hosu}회)")
    lines.append(f"🆔 <code>{stock['isin']}</code>")
    lines.append(f"📅 발행: {stock.get('issue_date', '-')} / 만기: {stock.get('maturity', '-')}")
    lines.append("")
    
    # 보유 정보
    amount = stock.get('amount', '')
    if amount and amount != '-' and amount != '0':
        lines.append(f"💰 <b>보유 정보</b>")
        lines.append(f"   • 보유금액: <b>{amount}억원</b>")
        if stock.get('acq'):
            lines.append(f"   • 취득가: {stock['acq']}억원")
        if stock.get('eval'):
            lines.append(f"   • 평가액: {stock['eval']}억원")
        if stock.get('return'):
            lines.append(f"   • 수익률: <b>{fmt_pct(stock['return'])}</b>")
        lines.append("")
    
    # 발행 조건
    lines.append(f"📊 <b>발행 조건</b>")
    if stock.get('coupon'):
        lines.append(f"   • Coupon: {stock['coupon']}% / YTM: {stock.get('ytm', '-')}%")
    if stock.get('ytc') and stock['ytc'] not in ('-', ''):
        lines.append(f"   • YTC: {stock['ytc']}%")
    if stock.get('exercise_price'):
        lines.append(f"   • 행사가액: {stock['exercise_price']}원")
    if stock.get('refix_floor'):
        lines.append(f"   • 리픽싱플로어: {stock['refix_floor']}원")
    lines.append("")
    
    # 전환청구기간
    xrc_b = stock.get('xrc_begin', '')
    xrc_e = stock.get('xrc_end', '')
    if xrc_b or xrc_e:
        lines.append(f"🔄 <b>전환청구기간</b>")
        lines.append(f"   {xrc_b or '-'} ~ {xrc_e or '-'}")
        lines.append("")
    
    # 다가오는 이벤트 (90일 내) + 전체 PUT/CALL 스케줄 (다음 5건)
    upcoming_events = []
    all_put_call = []
    
    for event in schedule:
        if event['isin'] != stock['isin']:
            continue
        try:
            event_date = datetime.strptime(event['start_date'], '%Y-%m-%d')
            today_dt = datetime.strptime(today_str, '%Y-%m-%d')
            diff = (event_date - today_dt).days
            
            # 90일 내 다가오는 모든 이벤트
            if 0 <= diff <= 90:
                upcoming_events.append({**event, 'diff': diff})
            
            # PUT/CALL 전체 (미래)
            if event['event_type'] in ('풋옵션', '콜옵션') and diff >= 0:
                all_put_call.append({**event, 'diff': diff})
        except:
            continue
    
    if upcoming_events:
        upcoming_events.sort(key=lambda x: x['diff'])
        lines.append(f"📅 <b>다가오는 이벤트 (90일 내)</b>")
        for e in upcoming_events[:10]:
            emoji = EVENT_EMOJI.get(e['event_type'], '📌')
            chasu_str = f" {e['chasu']}" if e['chasu'] else ''
            d_label = "D-DAY" if e['diff'] == 0 else f"D-{e['diff']}"
            lines.append(f"   {emoji} {e['event_type']}{chasu_str}: {e['start_date']} ({d_label})")
        lines.append("")
    
    # PUT/CALL 다음 5건 (전체 스케줄에서)
    if all_put_call:
        all_put_call.sort(key=lambda x: x['diff'])
        next_pc = [e for e in all_put_call if e['diff'] > 90][:5]
        if next_pc:
            lines.append(f"⏭ <b>이후 PUT/CALL (다음 5건)</b>")
            for e in next_pc:
                emoji = EVENT_EMOJI.get(e['event_type'], '📌')
                chasu_str = f" {e['chasu']}" if e['chasu'] else ''
                rate_str = f" [{e['rate']}]" if e.get('rate') else ''
                lines.append(f"   {emoji} {e['event_type']}{chasu_str}: {e['start_date']}{rate_str}")
            lines.append("")
    
    # 발행사 / 교환대상
    issuer_code = stock.get('issuer_code', '')
    target_name = stock.get('target_name', '')
    target_code = stock.get('target_code', '')
    
    if issuer_code or target_name:
        lines.append(f"🏢 <b>발행사 / 교환대상</b>")
        if issuer_code:
            lines.append(f"   • 발행사 주식코드: <code>{issuer_code}</code>")
        if target_name and target_name != stock['name']:
            lines.append(f"   • 교환대상: {target_name} ({target_code})")
        
        # 발행사 또는 교환대상 주식 현재가
        primary_code = target_code or issuer_code
        if primary_code:
            price = get_naver_stock_price(primary_code)
            if price:
                arrow = '📈' if price['change_pct'] > 0 else ('📉' if price['change_pct'] < 0 else '➖')
                lines.append(f"   • 현재가: {price['current']:,}원 {arrow} ({price['change_pct']:+.2f}%)")
        lines.append("")
    
    # 자본변동 이력
    related_actions = []
    name_clean = re.sub(r'\s*\d+(?:CB|EB|BW).*$', '', stock['name']).strip()
    for action in capital_actions:
        if action['stock_name'] == name_clean or name_clean in action['stock_name']:
            related_actions.append(action)
    
    if related_actions:
        lines.append(f"⚠️ <b>자본변동 이력</b>")
        for a in related_actions[:5]:
            lines.append(f"   • {a['disclosure_date']}: {a['action_type']}")
        lines.append("")
    
    # DART 링크
    if issuer_code:
        dart_corp = lookup_dart_corp_code(issuer_code)
        if dart_corp:
            lines.append(f"🔗 <a href='https://dart.fss.or.kr/dsab001/main.do?corpCd={dart_corp}'>DART 공시 모음</a>")
    
    return '\n'.join(lines)


# ============================================================
# [신규 핸들러: 종목 검색 (자유 텍스트)]
# ============================================================
def handle_search(sh, query, chat_id, message_id, token):
    """종목명 또는 ISIN으로 검색"""
    portfolio = load_portfolio_data(sh)
    if not portfolio:
        send_reply(token, chat_id, "⚠ 포트폴리오 데이터를 불러올 수 없습니다.", reply_to=message_id)
        return
    
    matches = search_portfolio(query, portfolio)
    
    if not matches:
        send_reply(token, chat_id,
            f"🔍 \"<b>{query}</b>\" 검색 결과: <b>0개</b>\n\n"
            f"💡 팁:\n"
            f"• 종목명 일부로 검색: 천보, 디앤디파마텍\n"
            f"• ISIN 직접 입력: KR6278282EB9\n"
            f"• 전체 목록 보기: /list",
            reply_to=message_id)
        return
    
    schedule = load_schedule_data(sh)
    capital_actions = load_capital_data(sh)
    today_str = datetime.now().strftime('%Y-%m-%d')
    
    # 보유금액 큰 것부터 정렬
    def amount_key(s):
        try:
            return -float(str(s.get('amount', '0')).replace(',', '') or 0)
        except:
            return 0
    matches.sort(key=amount_key)
    
    # 매칭 결과가 많으면 요약, 1개면 상세
    if len(matches) == 1:
        msg = format_stock_detail(matches[0], schedule, capital_actions, today_str)
        send_reply(token, chat_id, msg, reply_to=message_id)
    else:
        # 모두 자세히 (옵션 b)
        send_reply(token, chat_id,
            f"🔍 \"<b>{query}</b>\" 검색 결과: <b>{len(matches)}개</b>\n"
            f"보유금액 순으로 표시합니다.",
            reply_to=message_id)
        
        import time
        for stock in matches[:5]:  # 최대 5개까지
            msg = format_stock_detail(stock, schedule, capital_actions, today_str)
            send_reply(token, chat_id, msg, reply_to=message_id)
            time.sleep(0.5)
        
        if len(matches) > 5:
            send_reply(token, chat_id,
                f"... 외 {len(matches) - 5}개 더 있음\n"
                f"정확한 종목명으로 다시 검색해주세요.",
                reply_to=message_id)


# ============================================================
# [신규 핸들러: /list]
# ============================================================
def handle_portfolio_list(sh, args, chat_id, message_id, token):
    """보유 종목 전체 목록"""
    portfolio = load_portfolio_data(sh)
    if not portfolio:
        send_reply(token, chat_id, "⚠ 포트폴리오 데이터 없음", reply_to=message_id)
        return
    
    # 보유금액 있는 것만, 큰 순서대로
    holdings = []
    for p in portfolio:
        try:
            amt = float(str(p.get('amount', '0')).replace(',', '') or 0)
            if amt > 0:
                holdings.append((p, amt))
        except:
            continue
    
    holdings.sort(key=lambda x: -x[1])
    
    lines = [f"📊 <b>보유 종목 목록</b> ({len(holdings)}개)\n"]
    for i, (p, amt) in enumerate(holdings[:50], 1):
        ret = p.get('return', '')
        ret_str = f" ({fmt_pct(ret)})" if ret else ''
        lines.append(f"{i}. {p['name']}: {amt:.0f}억{ret_str}")
    
    if len(holdings) > 50:
        lines.append(f"\n... 외 {len(holdings) - 50}개")
    
    lines.append(f"\n💡 종목명 입력으로 상세 보기")
    
    send_reply(token, chat_id, '\n'.join(lines), reply_to=message_id)


# ============================================================
# [신규 핸들러: /total]
# ============================================================
def handle_total(sh, args, chat_id, message_id, token):
    """보유 통계 요약"""
    portfolio = load_portfolio_data(sh)
    if not portfolio:
        send_reply(token, chat_id, "⚠ 포트폴리오 데이터 없음", reply_to=message_id)
        return
    
    holdings = []
    total_amt = 0
    total_acq = 0
    total_eval = 0
    type_count = {'CB': 0, 'EB': 0, 'BW': 0}
    
    for p in portfolio:
        try:
            amt = float(str(p.get('amount', '0')).replace(',', '') or 0)
            if amt > 0:
                holdings.append((p, amt))
                total_amt += amt
                acq = float(str(p.get('acq', '0')).replace(',', '') or 0)
                ev = float(str(p.get('eval', '0')).replace(',', '') or 0)
                total_acq += acq
                total_eval += ev
                
                bt = p.get('bond_type', '')
                if bt in type_count:
                    type_count[bt] += 1
        except:
            continue
    
    holdings.sort(key=lambda x: -x[1])
    
    pnl = total_eval - total_acq
    pnl_pct = (pnl / total_acq * 100) if total_acq > 0 else 0
    
    lines = [f"📊 <b>보유 현황 요약</b>\n"]
    lines.append(f"📌 총 종목: <b>{len(holdings)}개</b>")
    lines.append(f"💰 총 보유: <b>{total_amt:,.0f}억원</b>")
    lines.append(f"💵 취득가 총액: {total_acq:,.0f}억원")
    lines.append(f"📊 시가평가 총액: {total_eval:,.0f}억원")
    
    pnl_emoji = '📈' if pnl > 0 else ('📉' if pnl < 0 else '➖')
    lines.append(f"{pnl_emoji} 평가손익: <b>{pnl:+.1f}억원</b> ({pnl_pct:+.2f}%)\n")
    
    lines.append(f"📊 <b>종류별</b>")
    for bt, cnt in type_count.items():
        if cnt > 0:
            lines.append(f"   • {bt}: {cnt}개")
    lines.append("")
    
    lines.append(f"🏆 <b>보유 상위 5개</b>")
    for p, amt in holdings[:5]:
        ret = p.get('return', '')
        ret_str = f" ({fmt_pct(ret)})" if ret else ''
        lines.append(f"   • {p['name']}: {amt:.0f}억{ret_str}")
    
    send_reply(token, chat_id, '\n'.join(lines), reply_to=message_id)


# ============================================================
# [신규 핸들러: /upcoming, /upcoming30]
# ============================================================
def handle_upcoming(sh, args, chat_id, message_id, token, days=7):
    """다가오는 이벤트"""
    schedule = load_schedule_data(sh)
    portfolio = load_portfolio_data(sh)
    
    # ISIN → 보유종목 매핑
    holdings_map = {p['isin']: p for p in portfolio}
    
    today_dt = datetime.now()
    today_str = today_dt.strftime('%Y-%m-%d')
    
    upcoming = []
    for event in schedule:
        # 보유 종목만
        holding = holdings_map.get(event['isin'])
        if not holding:
            continue
        try:
            amt = float(str(holding.get('amount', '0')).replace(',', '') or 0)
            if amt <= 0:
                continue
        except:
            continue
        
        try:
            ev_dt = datetime.strptime(event['start_date'], '%Y-%m-%d')
            diff = (ev_dt - today_dt).days
            if 0 <= diff <= days:
                upcoming.append({**event, 'diff': diff, 'amount': amt})
        except:
            continue
    
    if not upcoming:
        send_reply(token, chat_id,
            f"📅 <b>{days}일 내 다가오는 이벤트</b>\n\n"
            f"해당 기간에 보유 종목 이벤트 없음 ✨",
            reply_to=message_id)
        return
    
    # 이벤트 유형별 그룹화
    by_type = {}
    for e in upcoming:
        by_type.setdefault(e['event_type'], []).append(e)
    
    lines = [f"📅 <b>{days}일 내 다가오는 이벤트</b> ({len(upcoming)}건)\n"]
    
    # 우선순위 순서
    type_order = ['만기', '전환청구종료', '전환청구시작', '풋옵션', '콜옵션', '리픽싱']
    for event_type in type_order:
        items = by_type.get(event_type)
        if not items:
            continue
        items.sort(key=lambda x: x['diff'])
        emoji = EVENT_EMOJI.get(event_type, '📌')
        lines.append(f"\n{emoji} <b>{event_type}</b> ({len(items)}건)")
        for e in items[:10]:
            d_label = "D-DAY" if e['diff'] == 0 else f"D-{e['diff']}"
            chasu_str = f" {e['chasu']}" if e['chasu'] else ''
            lines.append(f"   • {e['start_date']} ({d_label}): {e['name']}{chasu_str}")
            lines.append(f"     💰 {e['amount']:.0f}억")
    
    send_reply(token, chat_id, '\n'.join(lines), reply_to=message_id)


def handle_upcoming7(sh, args, chat_id, message_id, token):
    handle_upcoming(sh, args, chat_id, message_id, token, days=7)


def handle_upcoming30(sh, args, chat_id, message_id, token):
    handle_upcoming(sh, args, chat_id, message_id, token, days=30)


# ============================================================
# [신규 핸들러: /capital]
# ============================================================
def handle_capital(sh, args, chat_id, message_id, token):
    """최근 자본변동 내역"""
    actions = load_capital_data(sh)
    if not actions:
        send_reply(token, chat_id,
            "⚠️ <b>자본변동 이력</b>\n\n자본변동 내역 없음 ✨",
            reply_to=message_id)
        return
    
    # 최근 30일 자본변동
    today_dt = datetime.now()
    cutoff = today_dt - timedelta(days=30)
    
    recent = []
    for a in actions:
        try:
            ad = datetime.strptime(a['disclosure_date'], '%Y-%m-%d')
            if ad >= cutoff:
                recent.append(a)
        except:
            continue
    
    if not recent:
        send_reply(token, chat_id,
            "⚠️ <b>최근 30일 자본변동</b>\n\n자본변동 없음",
            reply_to=message_id)
        return
    
    # 유형별 이모지
    type_emoji = {
        '주식병합': '⚠️📉', '액면병합': '⚠️📉',
        '주식분할': '📈', '액면분할': '📈',
        '무상증자': '🆕', '유상증자': '💰',
        '감자': '⚠️', '주식배당': '🎁',
    }
    
    # 정렬 (최신순)
    recent.sort(key=lambda x: x['disclosure_date'], reverse=True)
    
    lines = [f"⚠️ <b>최근 30일 자본변동</b> ({len(recent)}건)\n"]
    
    # 종목별 그룹화
    by_stock = {}
    for a in recent:
        by_stock.setdefault(a['stock_name'], []).append(a)
    
    for stock_name, items in by_stock.items():
        lines.append(f"\n🏢 <b>{stock_name}</b>")
        for a in items[:3]:
            emoji = type_emoji.get(a['action_type'], '🔔')
            lines.append(f"   {emoji} {a['action_type']} ({a['disclosure_date']})")
    
    send_reply(token, chat_id, '\n'.join(lines), reply_to=message_id)


# ============================================================
# [신규 /help (확장된 도움말)]
# ============================================================
def handle_help(sh, args, chat_id, message_id, token):
    msg = """🤖 <b>메자닌 모니터링 봇 명령어</b>

🔍 <b>종목 정보 검색</b>
  종목명 입력 → 상세 정보
  ISIN 입력 → 직접 검색
  예: <code>천보</code>, <code>디앤디파마텍</code>
       <code>KR6278282EB9</code>

📊 <b>보유 현황</b>
/list        - 보유 종목 전체 목록
/total       - 보유 통계 요약 (총액, 손익)
/upcoming    - 7일 내 다가오는 이벤트
/upcoming30  - 30일 내 다가오는 이벤트
/capital     - 최근 30일 자본변동

📌 <b>매칭 관리</b>
/match ISIN 주식코드 종목명
  └ 예: <code>/match KR60156S1G12 435570 에르코스 농업회사법인</code>
/status ISIN - 매칭 정보 조회
/list_failed - 매칭 실패 종목
/list_alias  - 별칭매칭 (검토 권장)
/list_manual - 수동확정 종목
/aliases     - 별칭사전 전체
/add_alias 원본 DART표기
  └ 예: <code>/add_alias 케이씨그린홀딩스 KC그린홀딩스</code>

💡 /help - 이 도움말"""
    send_reply(token, chat_id, msg, reply_to=message_id)


# ============================================================
# [메인 디스패처]
# ============================================================
HANDLERS = {
    # 기존 매칭 관리
    '/match':        handle_match,
    '/status':       handle_status,
    '/list_failed':  handle_list_failed,
    '/list_alias':   handle_list_alias,
    '/list_manual':  handle_list_manual,
    '/aliases':      handle_aliases,
    '/add_alias':    handle_add_alias,
    '/help':         handle_help,
    '/start':        handle_help,
    
    # 신규 보유 정보
    '/list':         handle_portfolio_list,
    '/total':        handle_total,
    '/upcoming':     handle_upcoming7,
    '/upcoming30':   handle_upcoming30,
    '/capital':      handle_capital,
}


def process_telegram_updates(sh, token):
    """텔레그램 새 메시지 폴링 및 처리."""
    print(f"\n📨 텔레그램 명령어 체크 중...")
    
    last_id = load_last_update_id()
    print(f"  📋 마지막 처리 ID: {last_id}")
    
    updates = get_telegram_updates(token, offset=last_id + 1)
    if not updates:
        print(f"  ℹ 새 메시지 없음")
        return
    
    print(f"  📬 새 메시지 {len(updates)}건")
    
    processed = 0
    for update in updates:
        update_id = update.get('update_id', 0)
        message = update.get('message') or update.get('edited_message')
        if not message:
            save_last_update_id(update_id)
            continue
        
        chat_id = message.get('chat', {}).get('id')
        message_id = message.get('message_id')
        text = message.get('text', '')
        
        if not chat_id or not text:
            save_last_update_id(update_id)
            continue
        
        # 명령어 파싱
        cmd, args = parse_command(text)
        
        if cmd:
            # 명령어 처리
            handler = HANDLERS.get(cmd)
            if handler:
                print(f"  🎯 명령어 처리: {cmd} (chat={chat_id})")
                try:
                    handler(sh, args, chat_id, message_id, token)
                    processed += 1
                except Exception as e:
                    print(f"  ⚠ 명령 처리 오류: {e}")
                    send_reply(token, chat_id,
                        f"❌ 명령 처리 중 오류:\n<code>{str(e)[:200]}</code>",
                        reply_to=message_id)
        else:
            # 명령어가 아니면 → 종목 검색 시도
            # 단, 너무 짧거나 흔한 단어는 무시 (1자 이하)
            text_clean = text.strip()
            if len(text_clean) >= 2 and not text_clean.startswith('/'):
                print(f"  🔍 종목 검색: '{text_clean}' (chat={chat_id})")
                try:
                    handle_search(sh, text_clean, chat_id, message_id, token)
                    processed += 1
                except Exception as e:
                    print(f"  ⚠ 검색 오류: {e}")
        
        save_last_update_id(update_id)
    
    print(f"  ✅ 처리 완료: {processed}건")
