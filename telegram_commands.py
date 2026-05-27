"""
telegram_commands.py — 텔레그램 봇 명령어 처리 모듈

8개 명령어 지원:
- /match ISIN 주식코드 종목명...  : 수동매칭 등록/갱신
- /status ISIN                     : 매칭 정보 조회
- /list_failed                     : 매칭 실패 목록
- /list_alias                      : 별칭매칭 목록
- /list_manual                     : 수동확정 목록
- /aliases                         : 별칭사전 목록
- /add_alias 원본 DART표기         : 별칭 추가
- /help                            : 도움말

사용법: alert.py에서 호출
    from telegram_commands import process_telegram_updates
    process_telegram_updates(sh, telegram_token)
"""

import os
import json
import re
import requests
from datetime import datetime


# 마지막 처리한 update_id 저장 파일 (중복 처리 방지)
LAST_UPDATE_FILE = 'last_update_id.json'

# 시트 이름
MATCH_SHEET = '주식코드매칭'
ALIAS_SHEET = '별칭사전'

# 매칭상태 표시
STATUS_DISPLAY = {
    'AUTO':   '✅ 자동매칭',
    'ALIAS':  '⚠️ 별칭매칭(검토)',
    'MANUAL': '🔒 수동확정',
    'FAILED': '❌ 매칭실패',
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
    """텔레그램 새 메시지 조회 (long polling 비활성화, 즉시 응답)."""
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
    """텍스트에서 명령어 추출.
    
    Returns:
        (cmd, args_str) 또는 (None, None)
    """
    text = text.strip()
    if not text.startswith('/'):
        return None, None
    
    # 봇 멘션 처리 (예: /match@my_bot ISIN ...)
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    if '@' in cmd:
        cmd = cmd.split('@')[0]
    args = parts[1] if len(parts) > 1 else ''
    return cmd, args


# ============================================================
# [명령어 핸들러: /help]
# ============================================================
def handle_help(sh, args, chat_id, message_id, token):
    msg = """🤖 <b>메자닌 매칭 봇 명령어</b>

<b>📌 매칭 관리</b>
/match ISIN 주식코드 종목명
  └ 수동매칭 등록
  └ 예: <code>/match KR60156S1G12 435570 에르코스 농업회사법인</code>

/status ISIN
  └ 매칭 정보 조회
  └ 예: <code>/status KR60156S1G12</code>

<b>📋 목록 조회</b>
/list_failed   - 매칭 실패 종목
/list_alias    - 별칭매칭 종목 (검토 권장)
/list_manual   - 수동확정 종목
/aliases       - 별칭사전 전체

<b>📝 별칭 추가</b>
/add_alias 원본표기 DART표기
  └ 예: <code>/add_alias 케이씨그린홀딩스 KC그린홀딩스</code>

<b>💡 안내</b>
/help          - 이 도움말"""
    send_reply(token, chat_id, msg, reply_to=message_id)


# ============================================================
# [명령어 핸들러: /match]
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
    
    # 검증
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
    
    # DART corp_code 조회 (DART에서 stock_code로 검색)
    dart_corp_code = lookup_dart_corp_code(stock_code)
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 시트에 이미 있는지 확인 → 있으면 업데이트, 없으면 추가
    try:
        ws = sh.worksheet(MATCH_SHEET)
        all_rows = ws.get_all_values()
    except Exception as e:
        send_reply(token, chat_id, f"❌ 시트 접근 실패: {e}", reply_to=message_id)
        return
    
    existing_row = None
    bond_name_existing = ''
    bond_type_existing = ''
    call_status_existing = ''
    issuer_code_existing = ''
    first_date_existing = today
    
    for i, row in enumerate(all_rows[1:], start=2):
        if row and row[0].strip() == isin:
            existing_row = i
            bond_name_existing = row[1].strip() if len(row) > 1 else ''
            bond_type_existing = row[2].strip() if len(row) > 2 else ''
            call_status_existing = row[3].strip() if len(row) > 3 else ''
            issuer_code_existing = row[4].strip() if len(row) > 4 else ''
            first_date_existing = row[10].strip() if len(row) > 10 else today
            break
    
    if existing_row:
        # 업데이트: F, G, H, I, J, L 컬럼
        update_data = [
            corp_name,                          # F: 공시대상종목명
            stock_code,                         # G: 공시대상주식코드
            dart_corp_code,                     # H: DARTcorp_code
            STATUS_DISPLAY['MANUAL'],           # I: 매칭상태
            'MANUAL_INPUT',                     # J: 매칭방법
        ]
        ws.update([update_data], range_name=f'F{existing_row}:J{existing_row}')
        ws.update([[today]], range_name=f'L{existing_row}')  # 최근검증일
        
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
        # 신규 추가
        new_row = [
            isin,                               # A
            '',                                 # B: 채권명 (미확인)
            '',                                 # C: 종류
            '',                                 # D: 콜상태
            '',                                 # E: 발행사주식코드
            corp_name,                          # F: 공시대상종목명
            stock_code,                         # G: 공시대상주식코드
            dart_corp_code,                     # H
            STATUS_DISPLAY['MANUAL'],           # I
            'MANUAL_INPUT',                     # J
            today,                              # K: 최초등록일
            today,                              # L: 최근검증일
            f'/match 명령으로 등록',            # M: 메모
        ]
        next_row = len(all_rows) + 1
        ws.update([new_row], range_name=f'A{next_row}:M{next_row}')
        
        send_reply(token, chat_id,
            f"✅ <b>신규 매칭 등록 완료</b>\n\n"
            f"📌 ISIN: <code>{isin}</code>\n"
            f"🏢 공시대상: <b>{corp_name}</b>\n"
            f"📊 주식코드: <code>{stock_code}</code>\n"
            f"📊 DART코드: <code>{dart_corp_code or '(조회실패)'}</code>\n"
            f"📌 상태: 🔒 수동확정\n\n"
            f"⚠️ 채권명/종류 등은 다음 재검증 시 자동 채워집니다.",
            reply_to=message_id)


# ============================================================
# [명령어 핸들러: /status]
# ============================================================
def handle_status(sh, args, chat_id, message_id, token):
    isin = args.strip()
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
                f"📌 종류: {g(2) or '(없음)'} {g(3)}\n"
                f"\n"
                f"🏢 공시대상: <b>{g(5) or '(미매칭)'}</b>\n"
                f"📊 주식코드: <code>{g(6) or '(미매칭)'}</code>\n"
                f"📊 DART코드: <code>{g(7) or '(미매칭)'}</code>\n"
                f"\n"
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
# [명령어 핸들러: /list_*]
# ============================================================
def handle_list(sh, args, chat_id, message_id, token, filter_keyword, list_name):
    """매칭상태별 필터링 후 리스트 반환."""
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
        for isin, bond, target in matched[:30]:  # 텔레그램 메시지 길이 제한 고려
            lines.append(f"• {bond} (<code>{isin}</code>)")
            lines.append(f"  └ {target}")
        if len(matched) > 30:
            lines.append(f"\n... 외 {len(matched) - 30}건")
        msg = '\n'.join(lines)
    
    send_reply(token, chat_id, msg, reply_to=message_id)


def handle_list_failed(sh, args, chat_id, message_id, token):
    handle_list(sh, args, chat_id, message_id, token, '❌', '매칭 실패 종목')


def handle_list_alias(sh, args, chat_id, message_id, token):
    handle_list(sh, args, chat_id, message_id, token, '⚠️', '별칭매칭 종목 (검토 권장)')


def handle_list_manual(sh, args, chat_id, message_id, token):
    handle_list(sh, args, chat_id, message_id, token, '🔒', '수동확정 종목')


# ============================================================
# [명령어 핸들러: /aliases]
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


# ============================================================
# [명령어 핸들러: /add_alias]
# ============================================================
def handle_add_alias(sh, args, chat_id, message_id, token):
    """
    형식: /add_alias 원본표기 DART표기
    예: /add_alias 케이씨그린홀딩스 KC그린홀딩스
    """
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
    
    # 중복 체크
    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].strip() == original:
            existing = row[1].strip() if len(row) > 1 else ''
            send_reply(token, chat_id,
                f"⚠️ 이미 등록된 별칭:\n\n"
                f"<b>{original}</b> → <b>{existing}</b>\n\n"
                f"수정하려면 시트에서 직접 변경하세요.",
                reply_to=message_id)
            return
    
    # 새 행 추가
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
# [메인 디스패처]
# ============================================================
HANDLERS = {
    '/match':        handle_match,
    '/status':       handle_status,
    '/list_failed':  handle_list_failed,
    '/list_alias':   handle_list_alias,
    '/list_manual':  handle_list_manual,
    '/aliases':      handle_aliases,
    '/add_alias':    handle_add_alias,
    '/help':         handle_help,
    '/start':        handle_help,  # 텔레그램 봇 시작 시 도움말 표시
}


def process_telegram_updates(sh, token):
    """텔레그램 새 메시지 폴링 및 처리.
    
    Args:
        sh: gspread 스프레드시트 객체
        token: 텔레그램 봇 토큰
    """
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
        if not cmd:
            save_last_update_id(update_id)
            continue
        
        # 명령어 처리
        handler = HANDLERS.get(cmd)
        if handler:
            print(f"  🎯 처리 중: {cmd} (chat={chat_id})")
            try:
                handler(sh, args, chat_id, message_id, token)
                processed += 1
            except Exception as e:
                print(f"  ⚠ 명령 처리 오류: {e}")
                send_reply(token, chat_id,
                    f"❌ 명령 처리 중 오류:\n<code>{str(e)[:200]}</code>",
                    reply_to=message_id)
        else:
            # 알 수 없는 명령어는 무시 (도배 방지)
            pass
        
        save_last_update_id(update_id)
    
    print(f"  ✅ 처리 완료: {processed}건")
