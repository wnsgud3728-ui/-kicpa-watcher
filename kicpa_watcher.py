# -*- coding: utf-8 -*-
"""
한공회 구인(수습CPA) 게시판 감시 → 새 공고를 텔레그램으로 알림

설치:   pip install requests beautifulsoup4
실행:   python kicpa_watcher.py
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime
from html import escape

import requests
from bs4 import BeautifulSoup

# ====================== 설정 ======================
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "여기에_봇_토큰")
CHAT_ID = os.environ.get("TG_CHAT_ID", "")          # 비워두면 실행 시 자동으로 찾아서 알려줌

CHECK_INTERVAL_SEC = 60                              # 1분
ONLY_ACCOUNTING_FIRMS = False                        # True면 회사명에 '회계'가 들어간 공고만 알림

BOARDS = {
    "수습CPA": "https://www.kicpa.or.kr/home/jobOffrSrchNewGnrl/list.face",
    # 필요하면 다른 게시판도 추가 가능 (구조가 같은 게시판만)
    # "일반":   "https://www.kicpa.or.kr/home/jobOffrSrchGnrl/list.face",
}
LIST_PARAMS = {"listCnt": "30"}                      # 첫 페이지에서 30건까지 확인

# GitHub Actions용 설정 (워크플로 파일에서 지정)
RUN_MINUTES = int(os.environ.get("RUN_MINUTES", "0"))      # 0이면 무한 실행, 아니면 N분 후 종료
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", "")           # 알림 앞에 붙일 표시 (예: GitHub)
GIT_COMMIT = os.environ.get("GIT_COMMIT", "") == "1"        # 기록 파일을 저장소에 커밋할지

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "kicpa_seen.json")
CONFIG_FILE = os.path.join(BASE_DIR, "kicpa_config.json")   # install.sh가 만들어 줌
# =================================================

# 설정 파일이 있으면 토큰/채팅 ID를 거기서 읽음
try:
    with open(CONFIG_FILE, encoding="utf-8") as _f:
        _cfg = json.load(_f)
    BOT_TOKEN = _cfg.get("bot_token") or BOT_TOKEN
    CHAT_ID = str(_cfg.get("chat_id") or CHAT_ID)
except (FileNotFoundError, json.JSONDecodeError):
    pass

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
session = requests.Session()
session.headers.update(HEADERS)


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------- 텔레그램 ----------------
def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data={
                "chat_id": CHAT_ID, "text": text,
                "parse_mode": "HTML", "disable_web_page_preview": "true",
            }, timeout=15)
            if r.ok:
                return True
            log(f"텔레그램 전송 실패: {r.status_code} {r.text[:200]}")
        except requests.RequestException as e:
            log(f"텔레그램 오류: {e}")
        time.sleep(3)
    return False


def find_chat_id():
    """봇에게 아무 메시지나 보낸 뒤 실행하면 chat id를 찾아준다."""
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=15)
    data = r.json()
    if not data.get("ok"):
        sys.exit(f"봇 토큰이 잘못된 것 같아요: {data}")
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("edited_message")
        if msg:
            return str(msg["chat"]["id"])
    sys.exit("텔레그램에서 봇에게 아무 메시지(예: /start)를 먼저 보낸 뒤 다시 실행하세요.")


# ---------------- 파싱 ----------------
def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def extract_id(tr):
    html = str(tr)
    m = re.search(r"ijIdNum\D{0,10}(\d{6,})", html)
    if m:
        return m.group(1)
    # onclick="fnView('1786323784665')" 같은 형태 대비
    for a in tr.find_all("a"):
        attrs = " ".join(str(v) for v in a.attrs.values())
        m = re.search(r"(\d{10,})", attrs)
        if m:
            return m.group(1)
    return None


def parse_list(html, list_url):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tr.find("a")
        title = clean(a.get_text() if a else tds[1].get_text())
        if not title:
            continue
        cells = [clean(td.get_text(" ")) for td in tds]
        company = cells[2] if len(cells) > 2 else ""
        region = cells[3] if len(cells) > 3 else ""
        date = next((c for c in cells if re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", c)), "")
        emp = next((c for c in cells if c in ("풀타임", "파트타임", "인턴쉽")), "")
        pid = extract_id(tr)
        detail = (list_url.replace("list.face", "detail.face") + f"?ijIdNum={pid}") if pid else list_url
        key = pid or f"{title}|{company}|{date}"
        posts.append(dict(key=key, title=title, company=company, region=region,
                          date=date, emp=emp, url=detail, has_detail=bool(pid)))
    return posts


def fetch_detail(url):
    """상세 페이지에서 이메일/담당자/전화/마감일 추출 (실패해도 무시)."""
    info = {}
    try:
        r = session.get(url, timeout=15)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text("\n")
        emails = [e for e in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
                  if "kicpa" not in e.lower()]
        if emails:
            info["email"] = list(dict.fromkeys(emails))[:3]
        for label, key in (("마감일", "deadline"), ("담당자", "contact"), ("전화번호", "phone")):
            m = re.search(label + r"[\s·:|]*\n?\s*([^\n·|]+)", text)
            if m and clean(m.group(1)) and clean(m.group(1)) != "-":
                info[key] = clean(m.group(1))[:40]
    except Exception as e:
        log(f"상세 페이지 실패({url}): {e}")
    return info


# ---------------- 상태 ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in state.items()}, f, ensure_ascii=False, indent=0)
    os.replace(tmp, STATE_FILE)


def git_commit_state():
    """GitHub에서 실행할 때: 기록 파일을 저장소에 저장해서 다음 작업이 이어받게 함."""
    import subprocess
    cmds = [
        ["git", "add", os.path.basename(STATE_FILE)],
        ["git", "commit", "-q", "-m", "update seen posts"],
        ["git", "push", "-q"],
    ]
    for c in cmds:
        r = subprocess.run(c, cwd=BASE_DIR, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"git 실패 ({' '.join(c)}): {(r.stderr or r.stdout).strip()[:200]}")
            return
    log("기록 파일 저장소에 저장 완료")


# ---------------- 메시지 ----------------
def format_msg(board, p, info):
    src = f" · {escape(SOURCE_LABEL)}" if SOURCE_LABEL else ""
    lines = [f"🆕 <b>[{escape(board)}] 새 공고</b>{src}",
             f"<b>{escape(p['title'])}</b>",
             f"🏢 {escape(p['company'])}  📍 {escape(p['region'])}  {escape(p['emp'])}",
             f"🗓 등록 {escape(p['date'])}"]
    if info.get("deadline"):
        lines.append(f"⏰ 마감 {escape(info['deadline'])}")
    if info.get("email"):
        lines.append("✉️ " + ", ".join(f"<code>{escape(e)}</code>" for e in info["email"]))
    if info.get("contact") or info.get("phone"):
        lines.append(f"👤 {escape(info.get('contact', ''))} {escape(info.get('phone', ''))}")
    lines.append(f'<a href="{escape(p["url"])}">공고 열기</a>')
    return "\n".join(lines)


# ---------------- 메인 루프 ----------------
def check_once(state, first_run):
    new_count = 0
    for board, url in BOARDS.items():
        r = session.get(url, params=LIST_PARAMS, timeout=20)
        r.raise_for_status()
        posts = parse_list(r.text, url)
        if not posts:
            raise RuntimeError(f"{board}: 게시글을 하나도 못 읽었어요 (사이트 구조 변경 가능성)")
        seen = set(state.get(board, []))

        if first_run and not seen:
            state[board] = [p["key"] for p in posts]
            log(f"{board}: 기존 공고 {len(posts)}건 기록 (알림 없음)")
            continue

        new_posts = [p for p in posts if p["key"] not in seen]
        for p in reversed(new_posts):  # 오래된 것부터 전송
            if ONLY_ACCOUNTING_FIRMS and "회계" not in p["company"]:
                seen.add(p["key"])
                continue
            info = fetch_detail(p["url"]) if p["has_detail"] else {}
            if tg_send(format_msg(board, p, info)):
                seen.add(p["key"])
                new_count += 1
                log(f"알림: {p['company']} - {p['title']}")
        state[board] = list(seen)[-500:]
    return new_count


def main():
    global CHAT_ID
    if "여기에" in BOT_TOKEN or not BOT_TOKEN:
        sys.exit("BOT_TOKEN을 먼저 넣어주세요 (스크립트 상단 또는 환경변수 TG_BOT_TOKEN).")
    if not CHAT_ID:
        CHAT_ID = find_chat_id()
        log(f"chat id = {CHAT_ID}  (스크립트 상단 CHAT_ID에 넣어두면 다음부터 생략됩니다)")

    state = load_state()
    fresh_start = not state
    first_run = True
    fails = 0
    started = time.time()
    src = f" · {SOURCE_LABEL}" if SOURCE_LABEL else ""
    # GitHub에서는 몇 시간마다 재시작되므로, 맨 처음 한 번만 시작 메시지를 보냄
    if not RUN_MINUTES or fresh_start:
        tg_send(f"✅ 한공회 구인공고 감시 시작 ({CHECK_INTERVAL_SEC}초 간격){escape(src)}")

    while True:
        if RUN_MINUTES and time.time() - started > RUN_MINUTES * 60:
            log(f"{RUN_MINUTES}분 경과, 이번 작업 종료 (다음 작업이 이어받음)")
            break
        try:
            before = json.dumps(state, sort_keys=True)
            n = check_once(state, first_run)
            save_state(state)
            if GIT_COMMIT and json.dumps(state, sort_keys=True) != before:
                git_commit_state()
            if first_run and fresh_start:
                tg_send("📋 현재 게시된 공고는 기록해뒀어요. 이제부터 새 글만 알려드립니다.")
            first_run = False
            if fails >= 3:
                tg_send("🔄 접속이 다시 정상화됐어요.")
            fails = 0
            log(f"확인 완료 (새 공고 {n}건)")
        except Exception as e:
            fails += 1
            log(f"오류 {fails}회: {e}")
            if fails == 3:
                tg_send(f"⚠️ 한공회 사이트 확인이 3번 연속 실패했어요: {escape(str(e))[:200]}")
        time.sleep(CHECK_INTERVAL_SEC + random.uniform(-5, 5))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("종료")
