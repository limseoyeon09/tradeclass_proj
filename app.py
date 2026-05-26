"""
TradeTable — KSA 시간표 트레이드 + 카카오 챗봇
완전한 단일 파일 버전
"""
import json, os, re, time, hashlib, datetime
from flask import Flask, request, jsonify, render_template
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Tuple
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
ADMIN_PW  = os.environ.get("ADMIN_PW", "tradetable-admin-2026")
_HERE     = os.path.dirname(__file__)

# ── 정적 데이터 로드 ─────────────────────────────────────────
with open(os.path.join(_HERE, "static", "data.json"), encoding="utf-8") as f:
    _DB = json.load(f)
COURSES  = _DB["courses"]
STUDENTS = _DB["students"]

with open(os.path.join(_HERE, "static", "professors.json"), encoding="utf-8") as f:
    PROFESSORS = json.load(f)

with open(os.path.join(_HERE, "static", "classrooms.json"), encoding="utf-8") as f:
    CLASSROOMS = json.load(f)

# ── 인메모리 저장소 ──────────────────────────────────────────
_trade_requests:  Dict[str, List] = {}
_last_match:      Dict            = {}
_trade_period:    Dict            = {"open": False, "start": None, "end": None}
_approved_moves:  Dict[str, List] = {}  # sid → [{ck, new_section, new_slots, new_room}]
_extra_courses:   Dict[str, List] = {}  # sid → [{course,grade,section,ck,slots,room}]
_kakao_users:     Dict[str, str]  = {}  # uid → sid
_kakao_admin:     Dict[str, bool] = {}  # uid → is_admin
_kakao_step:      Dict[str, dict] = {}  # uid → {s, ...} 로그인/입력 단계
_kakao_pins:      Dict[str, str]  = {}  # sid → hashed_pin
_kakao_lang:      Dict[str, str]  = {}  # uid → "ko" | "en"

# ── 번역 딕셔너리 ──────────────────────────────────────────
_T = {
    "ko": {
        "welcome":       _t(uid,"welcome"),
        "enter_id":      "한과영 학번을 입력해주세요.\n예) 25-096",
        "enter_name":    "본인 확인을 위해 이름을 입력해주세요.",
        "name_ok":       _t(uid,"name_ok"),
        "name_ok_exist": _t(uid,"name_ok_exist"),
        "name_fail":     _t(uid,"name_fail"),
        "pin_set":       _t(uid,"pin_set"),
        "pin_confirm":   _t(uid,"pin_confirm"),
        "pin_mismatch":  _t(uid,"pin_mismatch"),
        "pin_ok":        "🔐 PIN 설정 완료!\n{name}님 환영해요 👋",
        "login_ok":      "✅ 로그인 성공!\n{name}님 환영해요 👋",
        "pin_fail":      _t(uid,"pin_fail"),
        "id_not_found":  "❌ {sid}은 등록되지 않은 학번이에요.\n다시 입력해주세요.",
        "menu_q":        "{name}님, 무엇을 도와드릴까요?",
        "logout_done":   "로그아웃 됐습니다.\n학번을 입력해 다시 로그인하세요.",
        "cancel_done":   "취소됐습니다.",
        "period_closed": "⚠️ 현재 트레이드 신청 기간이 아닙니다.",
        "trade_cancelled":"신청이 취소됐습니다.",
        "no_match_yet":  "아직 매칭 결과가 없어요.",
        "no_my_match":   _t(uid,"no_my_match"),
        "trade_result":  "📊 내 트레이드 결과\n",
        "success_line":  "✅ {cn}: {fr}분반 → {to}분반 성공!",
        "fail_line":     "❌ {cn}: {fr}분반 → 실패",
        "conflict_hint": "⚠️ {to}분반 → {course}({slot})와 시간 충돌",
        "submitted_tail":"관리자 매칭 후 결과를 확인하세요.",
        "timetable":"📅 내 시간표", "free":"공강",
        "trade":"🔀 트레이드 신청", "result":"📊 결과 확인",
        "prof":"📞 선생님 연락망", "room":"🏫 형설관 공실",
        "add_course":"➕ 과목 추가", "move_sec":"🔄 분반 이동",
        "change_id":"🔄 다른 학번", "menu_btn":"🏠 메뉴로",
        "cancel_btn":"❌ 취소", "lang_btn":"🌐 English",
        "back":"↩️ 돌아가기", "next":"▶ 다음", "prev":"◀ 이전",
    },
    "en": {
        "welcome":       "Hello! This is TradeTable 🟡\n\nPlease enter your KSA student ID.\nEx) 25-096",
        "enter_id":      "Please enter your KSA student ID.\nEx) 25-096",
        "enter_name":    "Please enter your name for verification.",
        "name_ok":       "✅ Name verified!\nFirst login.\n\nPlease set a 4-digit PIN.",
        "name_ok_exist": "✅ Name verified!\n\nPlease enter your PIN.",
        "name_fail":     "❌ Name does not match.\nPlease start again with your student ID.",
        "pin_set":       "Enter 4 digits. (e.g. 1234)",
        "pin_confirm":   "Enter the PIN once more to confirm.",
        "pin_mismatch":  "PINs do not match.\nPlease enter a new 4-digit PIN.",
        "pin_ok":        "🔐 PIN set!\nWelcome, {name}! 👋",
        "login_ok":      "✅ Login successful!\nWelcome, {name}! 👋",
        "pin_fail":      "❌ Incorrect PIN.\nPlease start again with your student ID.",
        "id_not_found":  "❌ {sid} is not a registered ID.\nPlease try again.",
        "menu_q":        "{name}, how can I help you?",
        "logout_done":   "Logged out.\nEnter your student ID to log in again.",
        "cancel_done":   "Cancelled.",
        "period_closed": "⚠️ Trade requests are not open now.",
        "trade_cancelled":"Trade request cancelled.",
        "no_match_yet":  "No matching results yet.",
        "no_my_match":   "No trade request found for this round.",
        "trade_result":  "📊 My Trade Results\n",
        "success_line":  "✅ {cn}: Section {fr} → Section {to} Success!",
        "fail_line":     "❌ {cn}: Section {fr} → Failed",
        "conflict_hint": "⚠️ Sec {to} conflicts with {course} ({slot})",
        "submitted_tail":"Check results after admin runs matching.",
        "timetable":"📅 My Timetable", "free":"Free",
        "trade":"🔀 Trade Request", "result":"📊 Results",
        "prof":"📞 Teacher Contacts", "room":"🏫 Available Rooms",
        "add_course":"➕ Add Course", "move_sec":"🔄 Change Section",
        "change_id":"🔄 Switch Account", "menu_btn":"🏠 Menu",
        "cancel_btn":"❌ Cancel", "lang_btn":"🌐 한국어",
        "back":"↩️ Back", "next":"▶ Next", "prev":"◀ Prev",
    }
}

def _t(uid: str, key: str, **kw) -> str:
    lang = _kakao_lang.get(uid, "ko")
    text = _T[lang].get(key, _T["ko"].get(key, key))
    return text.format(**kw) if kw else text

def _menu_for(uid: str) -> list:
    return [
        _kb(_t(uid,"timetable"), "내 시간표"),
        _kb(_t(uid,"trade"),     "트레이드 신청"),
        _kb(_t(uid,"result"),    "결과 확인"),
        _kb(_t(uid,"prof"),      "선생님 연락망"),
        _kb(_t(uid,"add_course"),"과목추가"),
        _kb(_t(uid,"move_sec"),  "분반이동"),
        _kb(_t(uid,"room"),      "공실조회"),
        _kb(_t(uid,"lang_btn"),  "언어전환"),
    ]

def _t(uid: str, key: str, **kw) -> str:
    """uid의 언어 설정에 맞게 번역 반환"""
    lang = _kakao_lang.get(uid, "ko")
    text = _T[lang].get(key, _T["ko"].get(key, key))
    return text.format(**kw) if kw else text

def _menu_for(uid: str) -> list:
    return [
        _kb(_t(uid,"timetable"), "내 시간표"),
        _kb(_t(uid,"trade"),     "트레이드 신청"),
        _kb(_t(uid,"result"),    "결과 확인"),
        _kb(_t(uid,"prof"),      "선생님 연락망"),
        _kb(_t(uid,"add_course"),"과목추가"),
        _kb(_t(uid,"move_sec"),  "분반이동"),
        _kb(_t(uid,"room"),      "공실조회"),
        _kb(_t(uid,"lang_btn"),  "언어전환"),
    ]

# ── 유틸 ────────────────────────────────────────────────────
def _norm(sid: str) -> str:
    m = re.match(r"^(\d{2})(\d{3})$", sid.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else sid.strip()

def _hash(pin: str) -> str:
    return hashlib.sha256(pin.encode()).hexdigest()[:16]

def _get_name(sid: str) -> str:
    return STUDENTS.get(sid, {}).get("name", sid)

def _verify_name(sid: str, inp: str) -> bool:
    real = _get_name(sid)
    return inp.strip() in real or real in inp.strip()

def is_period_open() -> bool:
    if _trade_period["open"]: return True
    s, e = _trade_period.get("start"), _trade_period.get("end")
    return bool(s and e and s <= time.time() <= e)

def get_timetable(sid: str) -> Tuple[List[Dict], str]:
    s = STUDENTS.get(sid)
    if not s:
        return [], f"학번 {sid}을 찾을 수 없습니다."
    enrolled = []
    for e in s.get("enrolled", []):
        ck = f"{e['course']}({e['grade']})"
        enrolled.append({
            "course":  e["course"],
            "grade":   e["grade"],
            "section": e["section"],
            "ck":      ck,
            "slots":   e.get("slots", []),
            "room":    e.get("room", ""),
        })
    # 분반이동 반영
    for mv in _approved_moves.get(sid, []):
        for e in enrolled:
            if e["ck"] == mv["ck"]:
                e["section"] = mv["new_section"]
                e["slots"]   = mv["new_slots"]
                e["room"]    = mv.get("new_room", "")
    # 추가과목 반영
    for ex in _extra_courses.get(sid, []):
        if not any(e["ck"] == ex["ck"] for e in enrolled):
            enrolled.append(ex)
    return enrolled, ""

# ═══════════════════════════════════════════════════════════
# 매칭 알고리즘
# ═══════════════════════════════════════════════════════════
@dataclass
class _Cand:
    sid: str; sname: str; ck: str; fr: str; to: str; pri: int

def _conflict(a, b):
    return bool({(s["d"],s["p"]) for s in a} & {(s["d"],s["p"]) for s in b})

def solve(reqs, enrolled_map):
    cands = [_Cand(r["sid"],r["sname"],r["course_key"],r["from_section"],to,i)
             for r in reqs for i,to in enumerate(r["to_sections"])]
    def gk(c): return (c.sid, c.ck)
    groups = [sorted(list(g), key=lambda x: x.pri)
              for _,g in groupby(sorted(cands, key=gk), key=gk)]
    state = {f"{ck}|{sec}":cnt
             for ck,ci in COURSES.items() for sec,cnt in ci["sections"].items()}
    best=[]; bs=[-1]
    def sc(ch): return sum(3-c.pri for c in ch)
    def apply(c, st):
        kf,kt = f"{c.ck}|{c.fr}", f"{c.ck}|{c.to}"
        if st.get(kf,0)<=0: return False
        st[kf]-=1; st[kt]=st.get(kt,0)+1; return True
    def revert(c, st):
        st[f"{c.ck}|{c.fr}"]+=1; st[f"{c.ck}|{c.to}"]-=1
    def balanced(st):
        return all(st.get(f"{ck}|{sec}",0)==cnt
                   for ck,ci in COURSES.items() for sec,cnt in ci["sections"].items())
    def ok(c):
        to_sl = COURSES.get(c.ck,{}).get("slots",{}).get(c.to,[])
        if not to_sl: return True
        # 실시간 시간표로 충돌 체크 (분반이동/추가과목 반영됨)
        live_enrolled, _ = get_timetable(c.sid)
        other = [sl for e in live_enrolled
                 if e.get("ck","")!=c.ck for sl in e.get("slots",[])]
        return not _conflict(to_sl, other)
    def bt(idx, ch, st):
        if sc(ch)+(len(groups)-idx)*3<=bs[0]: return
        if idx==len(groups):
            if balanced(st):
                s=sc(ch)
                if s>bs[0]: bs[0]=s; best.clear(); best.extend(ch)
            return
        for c in groups[idx]:
            if apply(c,st):
                if ok(c): ch.append(c); bt(idx+1,ch,st); ch.pop()
                revert(c,st)
        bt(idx+1,ch,st)
    bt(0,[],state)
    by_ck = defaultdict(list)
    for c in best: by_ck[c.ck].append(c)
    cycles=[]
    for ck,ts in by_ck.items():
        g={t.fr:t for t in ts}; vis=set()
        for start in list(g.keys()):
            if start in vis: continue
            path,cur,seen=[],start,set()
            while cur in g and cur not in seen:
                seen.add(cur); t=g[cur]
                path.append({"sid":t.sid,"name":t.sname,"from":t.fr,"to":t.to,"pri":t.pri})
                cur=t.to
            if cur==start and len(path)>=2:
                for p in path: vis.add(p["from"])
                cycles.append({"course":ck,"members":path})
    return best, cycles

def build_result(reqs, chosen, cycles):
    cm={(c.sid,c.ck):c for c in chosen}; per={}
    for r in reqs:
        sid=r["sid"]
        if sid not in per:
            per[sid]={"sid":sid,"name":r["sname"],"successes":[],"failures":[]}
        c=cm.get((sid,r["course_key"]))
        if c:
            per[sid]["successes"].append({
                "course_key":r["course_key"],"from":r["from_section"],
                "to":c.to,"priority":c.pri,"to_list":r["to_sections"]})
        else:
            per[sid]["failures"].append({
                "course_key":r["course_key"],"from":r["from_section"],
                "to_list":r["to_sections"]})
    return {"total_success":len(chosen),"total_requests":len(reqs),
            "students":list(per.values()),"cycles":cycles}


def apply_match_to_timetable(result: dict):
    """
    매칭 결과를 _approved_moves에 자동 반영.
    성공한 트레이드: 시간표 적용 + _trade_requests에서 제거
    실패한 트레이드: _trade_requests에 유지 (다음 매칭 때 자동 사용)
    """
    for student in result.get("students", []):
        sid = student["sid"]

        # ── 성공한 트레이드 → 시간표 반영 + 신청 제거 ──
        success_cks = set()
        for t in student.get("successes", []):
            ck     = t["course_key"]
            to_sec = t["to"]
            info   = COURSES.get(ck, {})
            new_slots = info.get("slots", {}).get(to_sec, [])
            new_room = ""
            if new_slots:
                cn = ck.split("(")[0]
                sk = f"{new_slots[0]['d']}{new_slots[0]['p']}"
                for room, sched in CLASSROOMS.items():
                    v = sched.get(sk, "")
                    if cn in v and f"_{to_sec}" in v:
                        new_room = room; break
            entry = {"ck": ck, "new_section": to_sec,
                     "new_slots": new_slots, "new_room": new_room}
            _approved_moves.setdefault(sid, [])
            _approved_moves[sid] = [m for m in _approved_moves[sid] if m["ck"] != ck]
            _approved_moves[sid].append(entry)
            success_cks.add(ck)

        # ── 성공한 과목만 신청 목록에서 제거 (실패는 유지) ──
        if sid in _trade_requests and success_cks:
            _trade_requests[sid] = [
                r for r in _trade_requests[sid]
                if r["course_key"] not in success_cks
            ]
            # 모두 성공했으면 키 자체 제거
            if not _trade_requests[sid]:
                del _trade_requests[sid]
            else:
                # 남은 실패 신청의 enrolled 갱신 (분반 변경 반영)
                live_enrolled, _ = get_timetable(sid)
                for r in _trade_requests[sid]:
                    r["enrolled"] = live_enrolled

# ═══════════════════════════════════════════════════════════
# 웹 API 라우트
# ═══════════════════════════════════════════════════════════
@app.route("/")
def index(): return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status":"ok",
                    "students":len(STUDENTS),
                    "trade_requests":sum(len(v) for v in _trade_requests.values()),
                    "period_open":is_period_open(),
                    "professors":len(PROFESSORS)})

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True)
    username = d.get("username","").strip()
    password = d.get("password","").strip()
    if not username:
        return jsonify({"ok":False,"error":"학번을 입력하세요."}), 400
    if password == ADMIN_PW:
        sid  = _norm(username)
        name = _get_name(sid)
        return jsonify({"ok":True,"sid":sid,"name":name,"admin":True})
    sid = _norm(username)
    s   = STUDENTS.get(sid)
    if not s:
        return jsonify({"ok":False,"error":f"등록되지 않은 학번입니다: {sid}"}), 401
    return jsonify({"ok":True,"sid":sid,"name":s["name"],"admin":False})

@app.route("/api/timetable/<sid>")
def api_timetable(sid):
    enrolled, err = get_timetable(sid)
    if err and not enrolled:
        return jsonify({"ok":False,"error":err}), 400
    return jsonify({"ok":True,"enrolled":enrolled,"warning":err})

@app.route("/api/courses")
def api_courses():
    return jsonify({k:v for k,v in COURSES.items() if len(v["sections"])>=2})

@app.route("/api/professors")
def api_professors():
    return jsonify(PROFESSORS)

@app.route("/api/trade-request", methods=["POST"])
def api_trade_request():
    if not is_period_open():
        return jsonify({"ok":False,"error":"현재 트레이드 신청 기간이 아닙니다."}), 403
    d = request.get_json(force=True)
    sid=d.get("sid","").strip(); name=d.get("name","").strip()
    reqs=d.get("requests",[]); enrolled=d.get("enrolled",[])
    if not sid or not reqs:
        return jsonify({"ok":False,"error":"필수 데이터 누락"}), 400
    _trade_requests[sid] = [{**r,"sid":sid,"name":name,"enrolled":enrolled,
                              "ts":int(time.time())} for r in reqs]
    return jsonify({"ok":True,"message":f"트레이드 신청 {len(reqs)}건 접수됨."})

@app.route("/api/trade-request/<sid>", methods=["DELETE"])
def api_cancel(sid):
    _trade_requests.pop(sid, None)
    return jsonify({"ok":True})

@app.route("/api/trade-requests/my/<sid>")
def api_my(sid):
    return jsonify({"requests":_trade_requests.get(sid,[]),
                    "total_all":sum(len(v) for v in _trade_requests.values())})

@app.route("/api/last-match")
def api_last_match():
    if not _last_match.get("students"):
        return jsonify({"ok":False,"error":"아직 매칭이 실행되지 않았습니다."}), 404
    return jsonify({"ok":True,"result":_last_match})

@app.route("/api/admin/run-match", methods=["POST"])
def api_run_match():
    all_reqs=[]; em={}
    for sid,reqs in _trade_requests.items():
        for r in reqs:
            all_reqs.append({"sid":r["sid"],"sname":r["name"],
                              "course_key":r["course_key"],
                              "from_section":r["from_section"],
                              "to_sections":r["to_sections"]})
            if r.get("enrolled"): em[sid]=r["enrolled"]
    if not all_reqs:
        return jsonify({"ok":False,"error":"신청된 트레이드가 없습니다."}), 400
    try:
        chosen,cycles=solve(all_reqs,em)
        result=build_result(all_reqs,chosen,cycles)
        _last_match.clear(); _last_match.update(result)
        apply_match_to_timetable(result)   # 시간표 자동 반영
        return jsonify({"ok":True,"result":_last_match})
    except Exception as e:
        import traceback
        return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/api/admin/period", methods=["GET","POST"])
def api_period():
    if request.method=="GET":
        p=dict(_trade_period); p["is_open"]=is_period_open()
        return jsonify(p)
    d=request.get_json(force=True)
    if "open"  in d: _trade_period["open"]  = bool(d["open"])
    if "start" in d: _trade_period["start"] = d["start"]
    if "end"   in d: _trade_period["end"]   = d["end"]
    return jsonify({"ok":True,"period":_trade_period})

@app.route("/api/admin/all-requests")
def api_all_reqs():
    result=[]
    for sid,reqs in _trade_requests.items():
        for r in reqs:
            result.append({"sid":r["sid"],"name":r["name"],
                           "course_key":r["course_key"],
                           "from":r["from_section"],"to":r["to_sections"],"ts":r["ts"]})
    return jsonify(result)

@app.route("/api/admin/clear-requests", methods=["POST"])
def api_clear():
    _trade_requests.clear(); return jsonify({"ok":True})

# ═══════════════════════════════════════════════════════════
# 카카오 오픈빌더 스킬 서버
# ═══════════════════════════════════════════════════════════

# 발화 정규화 맵
_UTT_MAP = {
    # 시간표
    "📅 내 시간표":"내 시간표", "내 시간표 보기":"내 시간표",
    # 트레이드
    "🔀 트레이드 신청":"트레이드 신청", "트레이드":"트레이드 신청",
    # 결과
    "📊 결과 확인":"결과 확인",
    # 선생님
    "📞 선생님 연락망":"선생님 연락망", "선생님 연락처":"선생님 연락망",
    "교사 연락망":"선생님 연락망", "연락처":"선생님 연락망",
    # 공실
    "🏫 형설관 공실":"공실조회", "형설관 공실":"공실조회",
    "공실 조회":"공실조회", "형설관":"공실조회",
    # 메뉴
    "🏠 메뉴로":"메뉴", "🏠 학생 메뉴":"메뉴", "메인 메뉴":"메뉴",
    # 과목추가
    "➕ 과목 추가":"과목추가", "과목 추가":"과목추가",
    "교과목 추가 신청":"과목추가", "과목 추가 신청":"과목추가",
    "교과목 추가":"과목추가", "수강 추가":"과목추가",
    # 분반이동
    "🔄 분반 이동":"분반이동", "분반 이동":"분반이동",
    "분반이동 신청":"분반이동", "분반 변경":"분반이동",
    # 기타
    "↩️ 학부 목록":"선생님 연락망", "↩️ 돌아가기":"트레이드 신청",
    "🔄 새로고침":"공실조회",
    "📋 신청 현황":"신청현황", "신청 현황":"신청현황",
    "▶ 매칭 실행":"매칭실행", "매칭 실행":"매칭실행",
    "🟢 기간 열기":"기간열기", "기간 열기":"기간열기",
    "🔴 기간 닫기":"기간닫기", "기간 닫기":"기간닫기",
    "🔍 이름 검색":"이름검색", "🔍 다시 검색":"이름검색",
    "이름 검색":"이름검색", "선생님 검색":"이름검색",
    "🔀 다른 과목 추가":"트레이드 신청",
    "🏛 수리정보과학부":"수리정보과학부",
    "🔭 물리지구과학부":"물리지구과학부",
    "📚 인문예술학부":"인문예술학부",
    "🧪 화학생물학부":"화학생물학부",
    "🔄 다른 학번":"로그아웃", "다른 학번":"로그아웃",
    "🔄 Switch Account":"로그아웃",
    "로그아웃":"로그아웃", "❌ 취소":"취소", "❌ Cancel":"취소",
    "📅 현재 시간표":"내 시간표",
    # 언어 전환
    "🌐 English":"언어전환", "🌐 한국어":"언어전환",
    "언어전환":"언어전환", "영어":"언어전환", "한국어":"언어전환",
    # 영어 메뉴 별칭
    "📅 My Timetable":"내 시간표",
    "🔀 Trade Request":"트레이드 신청",
    "📊 Results":"결과 확인",
    "📞 Teacher Contacts":"선생님 연락망",
    "🏫 Available Rooms":"공실조회",
    "➕ Add Course":"과목추가",
    "🔄 Change Section":"분반이동",
    "🏠 Menu":"메뉴",
}


def _kt(msg, btns=None):
    r = {"version":"2.0","template":{"outputs":[{"simpleText":{"text":msg}}]}}
    if btns: r["template"]["quickReplies"] = btns
    return jsonify(r)

def _kc(cards, btns=None):
    r = {"version":"2.0","template":{"outputs":[{"carousel":{"type":"basicCard","items":cards}}]}}
    if btns: r["template"]["quickReplies"] = btns
    return jsonify(r)

def _kb(label, msg=None):
    return {"label":label[:14],"action":"message","messageText":msg or label}

def _menu(uid=""):
    """uid가 있으면 언어 설정 반영, 없으면 한국어"""
    if uid:
        return _menu_for(uid)
    return [
        _kb("📅 내 시간표"), _kb("🔀 트레이드 신청"),
        _kb("📊 결과 확인"), _kb("📞 선생님 연락망"),
        _kb("➕ 과목 추가","과목추가"), _kb("🔄 분반 이동","분반이동"),
        _kb("🏫 형설관 공실","공실조회"), _kb("🔄 다른 학번","로그아웃"),
        _kb("🌐 English","언어전환"),
    ]

def _admin_menu():
    return [
        _kb("📋 신청 현황","신청현황"), _kb("▶ 매칭 실행","매칭실행"),
        _kb("🟢 기간 열기","기간열기"), _kb("🔴 기간 닫기","기간닫기"),
        _kb("🏠 학생 메뉴","메뉴"),
    ]

def _tt_cards(sid, uid=""):
    """시간표 캐러셀 카드 생성 (uid로 언어 설정 반영)"""
    enrolled, _ = get_timetable(sid)
    if not enrolled: return []
    lang = _kakao_lang.get(uid, "ko")
    DAYS_KO = ['월','화','수','목','금']
    DAYS_EN = ['Mon','Tue','Wed','Thu','Fri']
    DAYS    = DAYS_KO if lang == "ko" else DAYS_EN
    DAYS_MAP = dict(zip(DAYS_KO, DAYS))  # 월→Mon 등
    today_idx = datetime.datetime.now().weekday()
    today_ko  = DAYS_KO[today_idx] if today_idx < 5 else None
    today_loc = DAYS[today_idx]    if today_idx < 5 else None
    free_word = _t(uid, "free")    # "공강" or "Free"
    sc = {}
    for e in enrolled:
        room = e.get('room','')
        for sl in e.get('slots',[]):
            r   = sl.get('r', room)
            loc = f" ({r})" if r else ""
            sc[(sl['d'], sl['p'])] = f"{e['course']}{loc}"
    day_order_ko = ([today_ko]+[d for d in DAYS_KO if d!=today_ko]) if today_ko else DAYS_KO
    cards = []
    for d_ko in day_order_ko:
        d_loc = DAYS_MAP.get(d_ko, d_ko)
        lines = []
        for p in range(1, 13):
            val = sc.get((d_ko, p), free_word)
            if lang == "ko":
                lines.append(f"{p}교시: {val}")
            else:
                lines.append(f"Period {p}: {val}")
        is_today = (d_ko == today_ko)
        if lang == "ko":
            label = f"{'오늘 ' if is_today else ''}{d_ko}요일"
            title = f"📅 {label}"
        else:
            label = f"{'Today ' if is_today else ''}{d_loc}"
            title = f"📅 {label}"
        cards.append({"title": title, "description": "\n".join(lines)})
    course_lines = []
    for e in enrolled:
        room = e.get('room','')
        loc  = f" ({room})" if room else ""
        if lang == "ko":
            course_lines.append(f"• {e['course']} {e['section']}분반{loc}")
        else:
            course_lines.append(f"• {e['course']} Sec.{e['section']}{loc}")
    title_all = "📚 전체 수강 과목" if lang == "ko" else "📚 All Courses"
    cards.append({"title": title_all, "description": "\n".join(course_lines)})
    return cards


@app.route("/kakao", methods=["POST"])
def kakao_skill():
    d       = request.get_json(force=True)
    utt_raw = d.get("userRequest",{}).get("utterance","").strip()
    uid     = d.get("userRequest",{}).get("user",{}).get("id","")
    utt     = _UTT_MAP.get(utt_raw, utt_raw)
    sid     = _kakao_users.get(uid)
    step    = _kakao_step.get(uid, {})
    is_admin= _kakao_admin.get(uid, False)

    # ── 언어 전환 ───────────────────────────────────────
    if utt == "언어전환":
        cur = _kakao_lang.get(uid, "ko")
        new_lang = "en" if cur == "ko" else "ko"
        _kakao_lang[uid] = new_lang
        if new_lang == "en":
            msg = "\U0001f310 Switched to English!\nWhat can I help you with?"
        else:
            msg = "\U0001f310 \ud55c\uad6d\uc5b4\ub85c \uc804\ud658\ub429\ub2c8\ub2e4!\n\ubb34\uc5c7\uc744 \ub3c4\uc640\ub4dc\ub9b4\uae4c\uc694?"
        return _kt(msg, _menu(uid))

    # ── 카카오 시스템 메시지 무시 ───────────────────────
    # 채널 추가/차단 등 시스템 발화는 무시
    if any(kw in utt_raw for kw in ["채널을 추가", "채널 추가", "채널을 차단", "대화 상대를 차단"]):
        if not sid:
            return _kt(_t(uid,"welcome"))
        name = _get_name(sid)
        return _kt(f"{name}님, 무엇을 도와드릴까요?", _menu())

    # ── 로그아웃 ────────────────────────────────────────────
    if utt == "로그아웃":
        for d_ in [_kakao_users,_kakao_admin,_kakao_step]:
            d_.pop(uid, None)
        return _kt(_t(uid,"logout_done"))

    # ── 취소 ────────────────────────────────────────────────
    if utt == "취소":
        _kakao_step.pop(uid, None)
        if sid:
            return _kt(_t(uid,"cancel_done"), _menu(uid))
        return _kt(_t(uid,"cancel_done"))

    # ── 미로그인: 로그인 흐름 ────────────────────────────────
    if not sid:
        s = step.get("s")

        # STEP 2: 이름 입력
        if s == "name":
            psid = step["sid"]
            if _verify_name(psid, utt_raw):
                if psid in _kakao_pins:
                    _kakao_step[uid] = {"s":"pin","sid":psid}
                    return _kt(_t(uid,"name_ok_exist"),
                               [_kb("❌ 취소","취소")])
                else:
                    _kakao_step[uid] = {"s":"pin_set","sid":psid}
                    return _kt(_t(uid,"name_ok"),
                               [_kb("❌ 취소","취소")])
            else:
                _kakao_step.pop(uid, None)
                return _kt(_t(uid,"name_fail"))

        # STEP 3a: PIN 설정
        if s == "pin_set":
            psid = step["sid"]
            if not utt_raw.isdigit() or len(utt_raw) != 4:
                return _kt(_t(uid,"pin_set"),
                           [_kb("❌ 취소","취소")])
            _kakao_step[uid] = {"s":"pin_confirm","sid":psid,"pin":utt_raw}
            return _kt(_t(uid,"pin_confirm"),
                       [_kb("❌ 취소","취소")])

        # STEP 3b: PIN 확인
        if s == "pin_confirm":
            psid = step["sid"]
            if utt_raw == step["pin"]:
                _kakao_pins[psid] = _hash(utt_raw)
                _kakao_users[uid] = psid
                _kakao_step.pop(uid, None)
                return _kt(_t(uid,"pin_ok",name=_get_name(psid)), _menu(uid))
            else:
                _kakao_step[uid] = {"s":"pin_set","sid":psid}
                return _kt(_t(uid,"pin_mismatch"),
                           [_kb("❌ 취소","취소")])

        # STEP 3c: PIN 입력 (기존 사용자)
        if s == "pin":
            psid = step["sid"]
            if _hash(utt_raw) == _kakao_pins.get(psid, "X"):
                _kakao_users[uid] = psid
                _kakao_step.pop(uid, None)
                return _kt(_t(uid,"login_ok",name=_get_name(psid)), _menu(uid))
            else:
                _kakao_step.pop(uid, None)
                return _kt(_t(uid,"pin_fail"))

        # STEP 1: 학번 입력
        m = re.match(r"^(\d{2})-?(\d{3})$", utt_raw)
        if m:
            nsid = f"{m.group(1)}-{m.group(2)}"
            if nsid not in STUDENTS:
                return _kt(f"❌ {nsid}은 등록되지 않은 학번이에요.\n다시 입력해주세요.")
            _kakao_step[uid] = {"s":"name","sid":nsid}
            return _kt(f"🔐 {nsid} ({_get_name(nsid)})\n\n본인 확인을 위해\n이름을 입력해주세요.",
                       [_kb("❌ 취소","취소")])

        # 학번 형식이 아닌 모든 입력 → 안내
        return _kt(_t(uid,"welcome"))

    # ── 로그인 완료 ─────────────────────────────────────────
    name = _get_name(sid)
    s    = step.get("s")

    # 과목추가 입력 대기
    if s == "extra_input":
        parts = utt_raw.strip().rsplit(" ",1)
        if len(parts)<2 or not parts[1].isdigit():
            return _kt("Format: [name] [section]\nEx) Calculus 3" if _kakao_lang.get(uid,"ko")=="en" else "형식: 과목명 분반번호\n예) 미적분학 3",
                       [_kb("❌ 취소","취소")])
        cname, sec = parts[0].strip(), parts[1].strip()
        matching = [(ck,info) for ck,info in COURSES.items()
                    if ck.split("(")[0] == cname or cname in ck]
        if not matching:
            return _kt(f"'{cname}' 과목을 찾을 수 없어요.\n과목명을 정확히 입력해주세요.",
                       [_kb("❌ 취소","취소")])
        ck, info = matching[0]
        if sec not in info.get("sections",{}):
            secs = ", ".join(sorted(info["sections"].keys(), key=int))
            return _kt(f"{sec}분반이 없어요.\n개설 분반: {secs}",
                       [_kb("❌ 취소","취소")])
        slots = info.get("slots",{}).get(sec,[])
        sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in slots[:3])
        _kakao_step[uid] = {"s":"extra_confirm","ck":ck,"sec":sec,
                            "slots":slots,"cname":cname}
        return _kt(f"➕ 추가 확인\n\n{ck} {sec}분반\n{sl_str}\n\n시간표에 추가할까요?",
                   [_kb("✅ 추가하기","extra_yes"), _kb("❌ 취소","취소")])

    if s == "extra_confirm" and utt == "extra_yes":
        ck    = step["ck"]; sec = step["sec"]
        slots = step["slots"]; cname = step["cname"]
        grade = ck.split("(")[1].rstrip(")") if "(" in ck else "?"
        entry = {"course":cname,"grade":grade,"section":sec,"ck":ck,"slots":slots,"room":""}
        _extra_courses.setdefault(sid,[])
        _extra_courses[sid] = [e for e in _extra_courses[sid] if e["ck"]!=ck]
        _extra_courses[sid].append(entry)
        # 트레이드 신청의 enrolled도 갱신
        if sid in _trade_requests:
            live_enrolled, _ = get_timetable(sid)
            for r in _trade_requests[sid]:
                r["enrolled"] = live_enrolled
        _kakao_step.pop(uid,None)
        return _kt(f"✅ {cname} {sec}분반이 시간표에 추가됐어요!",
                   [_kb("📅 내 시간표","내 시간표"), _kb("🏠 메뉴로","메뉴")])

    # 분반이동 입력 대기
    if s == "move_sec":
        ck = step["ck"]; from_sec = step["from_sec"]; cn = step["cn"]
        to_sec = utt_raw.strip()
        info   = COURSES.get(ck,{})
        if to_sec not in info.get("sections",{}):
            secs = ", ".join(s_ for s_ in sorted(info["sections"].keys(),key=int) if s_!=from_sec)
            return _kt(f"{to_sec}분반이 없어요.\n이동 가능 분반: {secs}",
                       [_kb("❌ 취소","취소")])
        new_slots = info.get("slots",{}).get(to_sec,[])
        sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in new_slots[:2])
        _kakao_step[uid] = {"s":"move_confirm","ck":ck,"from_sec":from_sec,
                            "to_sec":to_sec,"slots":new_slots,"cn":cn}
        return _kt(f"🔄 분반이동 확인\n\n{cn}\n{from_sec}분반 → {to_sec}분반\n{sl_str}\n\n확정할까요?",
                   [_kb("✅ 확정","move_yes"), _kb("❌ 취소","취소")])

    if s == "move_confirm" and utt == "move_yes":
        ck       = step["ck"]; from_sec = step["from_sec"]
        to_sec   = step["to_sec"]; new_slots = step["slots"]; cn = step["cn"]
        # 강의실 찾기
        new_room = ""
        if new_slots:
            sk = f"{new_slots[0]['d']}{new_slots[0]['p']}"
            for room, sched in CLASSROOMS.items():
                v = sched.get(sk,"")
                if ck.split("(")[0] in v and f"_{to_sec}" in v:
                    new_room = room; break
        entry = {"ck":ck,"new_section":to_sec,"new_slots":new_slots,"new_room":new_room}
        _approved_moves.setdefault(sid,[])
        _approved_moves[sid] = [m for m in _approved_moves[sid] if m["ck"]!=ck]
        _approved_moves[sid].append(entry)
        _kakao_step.pop(uid,None)
        sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in new_slots[:2])
        return _kt(f"✅ {cn} 분반이동 완료!\n{from_sec}분반 → {to_sec}분반\n{sl_str}",
                   [_kb("📅 내 시간표","내 시간표"), _kb("🏠 메뉴로","메뉴")])

    # ── 메뉴 ────────────────────────────────────────────────
    if utt in ["메뉴","처음으로","홈","시작","메인"]:
        btns = _admin_menu() if is_admin else _menu(uid)
        return _kt(_t(uid,"menu_q",name=name), btns)

    # ── 관리자 진입 ──────────────────────────────────────────
    if utt.startswith("관리자 "):
        pw = utt[4:].strip()
        if pw == ADMIN_PW:
            _kakao_admin[uid] = True
            total = sum(len(v) for v in _trade_requests.values())
            period = "열림 ✅" if is_period_open() else "닫힘 ❌"
            return _kt(f"🛠 관리자 모드\n신청: {len(_trade_requests)}명 {total}건\n기간: {period}",
                       _admin_menu())
        return _kt("❌ Incorrect password." if _kakao_lang.get(uid,"ko")=="en" else "❌ 비밀번호가 틀렸습니다.")

    # ── 내 시간표 ────────────────────────────────────────────
    if utt in ["내 시간표","시간표"]:
        cards = _tt_cards(sid, uid)
        if not cards:
            return _kt("No timetable found." if _kakao_lang.get(uid,"ko")=="en" else "시간표 정보가 없어요.", [_kb("🏠 메뉴로","메뉴")])
        return _kc(cards, [_kb("🔀 트레이드 신청"),
                           _kb("➕ 과목 추가","과목추가"),
                           _kb("🏠 메뉴로","메뉴")])

    # 다른 학생 시간표
    m_o = re.search(r"(\d{2})-?(\d{3})", utt_raw)
    if m_o and ("시간표" in utt or "조회" in utt):
        osid = f"{m_o.group(1)}-{m_o.group(2)}"
        if osid in STUDENTS:
            cards = _tt_cards(osid, uid)
            if cards: cards[0]["title"] = f"📅 {_get_name(osid)}({osid})"
            return _kc(cards, [_kb("🏠 메뉴로","메뉴")])
        return _kt(f"❌ {osid}은 등록되지 않은 학번이에요.", [_kb("🏠 메뉴로","메뉴")])

    # ── 트레이드 신청 ────────────────────────────────────────
    if utt in ["트레이드 신청","신청"] or utt.startswith("과목목록 "):
        if not is_period_open():
            return _kt(_t(uid,"period_closed"), [_kb(_t(uid,"menu_btn"),"메뉴")])
        enrolled, _ = get_timetable(sid)
        tradeable = [e for e in enrolled
                     if e["ck"] in COURSES and len(COURSES[e["ck"]]["sections"])>=2]
        if not tradeable:
            return _kt("No courses available for trade." if _kakao_lang.get(uid,"ko")=="en" else "트레이드 가능한 과목이 없어요.", [_kb("🏠 메뉴로","메뉴")])
        # 페이지네이션
        page = int(utt.split(" ")[1]) if utt.startswith("과목목록 ") else 0
        PAGE = 4
        page_t = tradeable[page*PAGE:(page+1)*PAGE]
        existing = _trade_requests.get(sid,[])
        if _kakao_lang.get(uid,"ko") == "en":
            lines = [f"🔀 Trade Request ({page*PAGE+1}~{min((page+1)*PAGE,len(tradeable))}/{len(tradeable)})\n"]
            lines.append("Select a course or type the course name.\n")
        else:
            lines = [f"🔀 트레이드 신청 ({page*PAGE+1}~{min((page+1)*PAGE,len(tradeable))}/{len(tradeable)}개)\n"]
            lines.append("버튼으로 선택하거나 과목명을 직접 입력하세요.\n")
        btns = []
        for e in tradeable:  # 전체 목록 텍스트로 표시
            req = next((r for r in existing if r["course_key"]==e["ck"]),None)
            mark = " ✅" if req else ""
            lines.append(f"• {e['course']} ({e['section']}분반){mark}")
        lines.append("")
        for e in page_t:    # 현재 페이지 버튼
            req = next((r for r in existing if r["course_key"]==e["ck"]),None)
            mark = " ✅" if req else ""
            btns.append(_kb((e['course']+mark)[:14],
                           f"과목선택 {e['ck']} {e['section']}"))
        shown = (page+1)*PAGE
        if shown < len(tradeable):
            btns.append(_kb(f"▶ 다음({shown}/{len(tradeable)})",f"과목목록 {page+1}"))
        if page > 0:
            btns.append(_kb("◀ 이전", f"과목목록 {page-1}"))
        if existing:
            btns.append(_kb("📤 신청 제출","신청제출"))
        btns.append(_kb("🏠 메뉴로","메뉴"))
        return _kt("\n".join(lines), btns[:6])

    # 과목명 직접 타이핑해서 신청
    _RESERVED = ("과목선택","분반선택","지망확정","과목목록","이동과목","이동확정",
                 "신청","검색","관리자","메뉴","홈","시작","처음으로","메인",
                 "과목추가","분반이동","공실조회","로그아웃","취소","결과",
                 "신청제출","신청취소","이름검색","수리정보","물리지구","인문예술","화학생물",
                 "신청현황","매칭실행","기간열기","기간닫기","내 시간표","시간표",
                 "트레이드","선생님","연락망","extra_yes","move_yes","학부상세","영역보기",
                 "언어전환","영어","한국어")
    if not any(utt.startswith(r) or utt==r for r in _RESERVED):
        # 수강 중인 과목명과 매칭 시도
        enrolled, _ = get_timetable(sid)
        tradeable = [e for e in enrolled
                     if e["ck"] in COURSES and len(COURSES[e["ck"]]["sections"])>=2]
        matched = [e for e in tradeable if utt in e['course'] or e['course'] in utt]
        if matched and is_period_open():
            e = matched[0]
            ck, my_sec = e['ck'], e['section']
            cn = ck.split("(")[0]
            info = COURSES.get(ck,{})
            all_s = [s for s in sorted(info.get("sections",{}).keys(),key=lambda x:int(x))
                     if s!=my_sec]
            PAGE = 4
            btns = []
            for sec in all_s[:PAGE]:
                sl = info.get("slots",{}).get(sec,[])
                sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in sl[:2])
                btns.append(_kb(f"{sec}분반({sl_str})"[:14],
                               f"분반선택 {ck} {my_sec} {sec}"))
            if len(all_s)>PAGE:
                btns.append(_kb(f"▶ 다음({PAGE}/{len(all_s)})",
                               f"과목선택 {ck} {my_sec} 1"))
            btns.append(_kb("↩️ 돌아가기","트레이드 신청"))
            return _kt(f"📚 {cn} (현재 {my_sec}분반)\n이동할 분반을 선택하세요:", btns[:6])

    # 과목 선택 (페이지네이션)
    if utt.startswith("과목선택 "):
        parts = utt.split(" ")
        if len(parts)<3: return _kt("오류", [_kb("🏠 메뉴로","메뉴")])
        ck, my_sec = parts[1], parts[2]
        page = int(parts[3]) if len(parts)>3 else 0
        cn   = ck.split("(")[0]
        info = COURSES.get(ck,{})
        all_s = [s for s in sorted(info.get("sections",{}).keys(),key=lambda x:int(x))
                 if s!=my_sec]
        PAGE = 4
        page_s = all_s[page*PAGE:(page+1)*PAGE]
        btns = []
        for sec in page_s:
            sl = info.get("slots",{}).get(sec,[])
            sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in sl[:2])
            btns.append(_kb(f"{sec}분반({sl_str})"[:14],
                           f"분반선택 {ck} {my_sec} {sec}"))
        shown = (page+1)*PAGE
        if shown < len(all_s):
            btns.append(_kb(f"▶다음({shown}/{len(all_s)})",
                           f"과목선택 {ck} {my_sec} {page+1}"))
        if page>0:
            btns.append(_kb("◀이전", f"과목선택 {ck} {my_sec} {page-1}"))
        btns.append(_kb("↩️돌아가기","트레이드 신청"))
        if _kakao_lang.get(uid,"ko") == "en":
            return _kt(f"📚 {cn} (Current: Sec.{my_sec})\nSelect section ({page*PAGE+1}~{min(shown,len(all_s))}/{len(all_s)}):", btns[:6])
        else:
            return _kt(f"📚 {cn} (현재 {my_sec}분반)\n분반 선택 ({page*PAGE+1}~{min(shown,len(all_s))}/{len(all_s)}):", btns[:6])

    # 분반 선택 → 지망 확인
    if utt.startswith("분반선택 "):
        parts = utt.split(" ")
        if len(parts)<4: return _kt("오류", [_kb("🏠 메뉴로","메뉴")])
        ck, from_s, to_s = parts[1], parts[2], parts[3]
        cn   = ck.split("(")[0]
        reqs = _trade_requests.get(sid,[])
        req  = next((r for r in reqs if r["course_key"]==ck),None)
        cur  = req["to_sections"] if req else []
        n    = len(cur)+1
        if to_s in cur:
            return _kt(f"⚠️ {to_s}분반은 이미 {cur.index(to_s)+1}지망이에요.",
                       [_kb("↩️ 다른 분반",f"과목선택 {ck} {from_s}")])
        if n>3:
            return _kt("Already selected up to 3rd preference." if _kakao_lang.get(uid,"ko")=="en" else "이미 3지망까지 선택했어요.",
                       [_kb("❌ 신청 취소","신청취소"), _kb("🏠 메뉴로","메뉴")])
        prev = " / ".join(f"{i+1}지망 {s_}분반" for i,s_ in enumerate(cur))
        if prev: prev += f" / {n}지망 {to_s}분반(예정)"
        else:    prev = f"1지망 {to_s}분반(예정)"
        return _kt(f"📚 {cn}\n{from_s}분반 → ?\n\n{prev}\n\n{to_s}분반을 {n}지망으로 확정할까요?",
                   [_kb(f"✅ {n}지망 확정",f"지망확정 {ck} {from_s} {to_s}"),
                    _kb("↩️ 다른 분반",f"과목선택 {ck} {from_s}"),
                    _kb("🏠 메뉴로","메뉴")])

    # 지망 확정
    if utt.startswith("지망확정 "):
        parts = utt.split(" ")
        if len(parts)<4: return _kt("오류", [_kb("🏠 메뉴로","메뉴")])
        ck, from_s, to_s = parts[1], parts[2], parts[3]
        cn = ck.split("(")[0]
        enrolled, _ = get_timetable(sid)
        reqs = _trade_requests.get(sid,[])
        req  = next((r for r in reqs if r["course_key"]==ck),None)
        if req:
            if to_s not in req["to_sections"] and len(req["to_sections"])<3:
                req["to_sections"].append(to_s)
        else:
            reqs.append({"sid":sid,"name":name,"course_key":ck,
                         "from_section":from_s,"to_sections":[to_s],
                         "enrolled":enrolled,"ts":int(time.time())})
            _trade_requests[sid] = reqs
        req = next((r for r in _trade_requests.get(sid,[]) if r["course_key"]==ck),None)
        w   = " / ".join(f"{i+1}지망 {s_}분반" for i,s_ in enumerate(req["to_sections"]))
        n   = len(req["to_sections"])
        btns = []
        if n<3: btns.append(_kb(f"➕{n+1}지망 추가",f"과목선택 {ck} {from_s}"))
        btns += [_kb("🔀 다른 과목","트레이드 신청"),
                 _kb("📤 신청 제출","신청제출")]
        return _kt(f"✅ {cn} {n}지망 등록!\n{from_s}분반 → {w}", btns)

    if utt=="신청제출":
        reqs = _trade_requests.get(sid,[])
        if not reqs: return _kt("No trade requests found." if _kakao_lang.get(uid,"ko")=="en" else "신청 내역이 없어요.", [_kb("🏠 메뉴로","메뉴")])
        lines = ["📤 신청 완료!\n"]
        for r in reqs:
            cn = r["course_key"].split("(")[0]
            w  = " / ".join(f"{i+1}지망 {s_}분반" for i,s_ in enumerate(r["to_sections"]))
            lines.append(f"• {cn}: {r['from_section']}분반 → {w}")
        lines.append("\n관리자 매칭 후 '결과 확인'에서 확인하세요.")
        return _kt("\n".join(lines), [_kb("📊 결과 확인"), _kb("🏠 메뉴로","메뉴")])

    if utt=="신청취소":
        _trade_requests.pop(sid,None)
        return _kt(_t(uid,"trade_cancelled"), _menu(uid))

    # ── 결과 확인 ────────────────────────────────────────────
    if utt in ["결과 확인","결과"]:
        if not _last_match.get("students"):
            return _kt(_t(uid,"no_match_yet"), [_kb(_t(uid,"menu_btn"),"메뉴")])
        my = next((s_ for s_ in _last_match["students"] if s_["sid"]==sid),None)
        if not my: return _kt(_t(uid,"no_my_match"), [_kb("🏠 메뉴로","메뉴")])
        lines = ["📊 내 트레이드 결과\n"]
        for t in my.get("successes",[]):
            cn = t["course_key"].split("(")[0]
            lines.append(f"✅ {cn}: {t['from']}분반 → {t['to']}분반 성공!")
        for t in my.get("failures",[]):
            cn = t["course_key"].split("(")[0]
            # 충돌 여부 확인
            to_list = t.get("to_list",[])
            enrolled_now, _ = get_timetable(sid)
            conflict_info = ""
            for to_sec in to_list:
                to_slots = COURSES.get(t["course_key"],{}).get("slots",{}).get(to_sec,[])
                for sl in to_slots:
                    conflict = next((e for e in enrolled_now
                                     if e["ck"]!=t["course_key"]
                                     and any(s["d"]==sl["d"] and s["p"]==sl["p"]
                                             for s in e.get("slots",[]))), None)
                    if conflict:
                        conflict_info = f"\n   ⚠️ {to_sec}분반 → {conflict['course']}({sl['d']}{sl['p']}교시)와 시간 충돌"
                        break
            lines.append(f"❌ {cn}: {t['from']}분반 → 실패{conflict_info}")
            if not conflict_info:
                lines.append(f"   교수님께 직접 분반 변경을 문의하세요.")
        btns = [_kb("📅 현재 시간표","내 시간표"), _kb("🏠 메뉴로","메뉴")]
        if my.get("failures"): btns.insert(0,_kb("📞 선생님 연락망"))
        return _kt("\n".join(lines), btns)

    # ── 과목 추가 ────────────────────────────────────────────
    if utt=="과목추가":
        _kakao_step[uid] = {"s":"extra_input"}
        return _kt("➕ Add Course\n\nEnter course name and section.\nFormat: [name] [section]\nEx) Calculus 3" if _kakao_lang.get(uid,"ko")=="en" else "➕ 과목 추가신청\n\n과목명과 분반을 입력해주세요.\n형식: 과목명 분반번호\n예) 미적분학 3",
                   [_kb("❌ 취소","취소")])

    # ── 분반 이동 ────────────────────────────────────────────
    if utt=="분반이동":
        enrolled, _ = get_timetable(sid)
        if not enrolled: return _kt("No timetable found." if _kakao_lang.get(uid,"ko")=="en" else "시간표 정보가 없어요.", [_kb("🏠 메뉴로","메뉴")])
        lines = ["🔄 분반 이동\n이동할 과목을 선택하세요:\n"]
        btns  = []
        for e in enrolled[:5]:
            lines.append(f"• {e['course']} ({e['section']}분반)")
            btns.append(_kb(e['course'][:14],
                           f"이동과목 {e['ck']} {e['section']}"))
        btns.append(_kb("🏠 메뉴로","메뉴"))
        return _kt("\n".join(lines), btns)

    if utt.startswith("이동과목 "):
        parts = utt.split(" ")
        if len(parts)<3: return _kt("오류", [_kb("🏠 메뉴로","메뉴")])
        ck, from_s = parts[1], parts[2]
        page = int(parts[3]) if len(parts)>3 else 0
        cn   = ck.split("(")[0]
        info = COURSES.get(ck,{})
        all_s = [s_ for s_ in sorted(info.get("sections",{}).keys(),key=lambda x:int(x))
                 if s_!=from_s]
        PAGE = 4
        page_s = all_s[page*PAGE:(page+1)*PAGE]
        btns = []
        for sec in page_s:
            sl = info.get("slots",{}).get(sec,[])
            sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in sl[:2])
            btns.append(_kb(f"{sec}분반({sl_str})"[:14],
                           f"이동확정 {ck} {from_s} {sec}"))
        shown = (page+1)*PAGE
        if shown < len(all_s):
            btns.append(_kb(f"▶ 다음({shown}/{len(all_s)})",
                           f"이동과목 {ck} {from_s} {page+1}"))
        if page>0:
            btns.append(_kb("◀ 이전", f"이동과목 {ck} {from_s} {page-1}"))
        btns.append(_kb("↩️ 돌아가기","분반이동"))
        if _kakao_lang.get(uid,"ko") == "en":
            msg_move = (f"🔄 {cn} (Current: Sec.{my_sec})\n"
                        f"Select section ({page*PAGE+1}~{min(shown,len(all_s))}/{len(all_s)})\n"
                        f"(Only move to sections approved by your teacher)")
        else:
            msg_move = (f"🔄 {cn} (현재 {my_sec}분반)\n"
                        f"분반 선택 ({page*PAGE+1}~{min(shown,len(all_s))}/{len(all_s)})\n"
                        f"(선생님 허락을 받은 분반으로만 이동하세요)")
        return _kt(msg_move, btns[:6])

    if utt.startswith("이동확정 "):
        parts = utt.split(" ")
        if len(parts)<4: return _kt("오류", [_kb("🏠 메뉴로","메뉴")])
        ck, from_s, to_s = parts[1], parts[2], parts[3]
        cn        = ck.split("(")[0]
        info      = COURSES.get(ck,{})
        new_slots = info.get("slots",{}).get(to_s,[])
        new_room  = ""
        if new_slots:
            sk = f"{new_slots[0]['d']}{new_slots[0]['p']}"
            for room, sched in CLASSROOMS.items():
                v = sched.get(sk,"")
                if cn in v and f"_{to_s}" in v:
                    new_room = room; break
        entry = {"ck":ck,"new_section":to_s,"new_slots":new_slots,"new_room":new_room}
        _approved_moves.setdefault(sid,[])
        _approved_moves[sid] = [m for m in _approved_moves[sid] if m["ck"]!=ck]
        _approved_moves[sid].append(entry)
        # 트레이드 신청의 enrolled도 갱신 (기존 신청이 있으면)
        if sid in _trade_requests:
            live_enrolled, _ = get_timetable(sid)
            for r in _trade_requests[sid]:
                r["enrolled"] = live_enrolled
        sl_str = " ".join(f"{s_['d']}{s_['p']}교시" for s_ in new_slots[:2])
        return _kt(f"✅ {cn} 분반이동 완료!\n{from_s}분반 → {to_s}분반\n{sl_str}",
                   [_kb("📅 내 시간표","내 시간표"), _kb("🏠 메뉴로","메뉴")])

    # ── 선생님 연락망 ────────────────────────────────────────
    if utt in ["선생님 연락망","연락망","연락처"]:
        return _kt("📞 Teacher Contacts\n\nSelect a department or search by name.\nName search: search [name]" if _kakao_lang.get(uid,"ko")=="en" else "📞 선생님 연락망\n\n학부를 선택하거나 이름으로 검색하세요.\n이름 검색: 검색 홍길동",
                   [_kb("🏛 수리정보과학부","수리정보과학부"),
                    _kb("🔭 물리지구과학부","물리지구과학부"),
                    _kb("📚 인문예술학부","인문예술학부"),
                    _kb("🧪 화학생물학부","화학생물학부"),
                    _kb("🏠 메뉴로","메뉴")])

    if utt in ["수리정보과학부","물리지구과학부","인문예술학부","화학생물학부"] \
            or utt.startswith("학부상세 ") or utt.startswith("영역보기 "):
        if utt.startswith("학부상세 ") or utt.startswith("영역보기 "):
            parts       = utt.split(" ")
            dept_name   = parts[1]
            area_filter = parts[2] if len(parts)>2 else None
            area_page   = int(parts[3]) if len(parts)>3 else 0
        else:
            dept_name   = utt
            area_filter = None
            area_page   = 0
        profs   = [p for p in PROFESSORS if p["dept"]==dept_name]
        by_area = defaultdict(list)
        for p in profs: by_area[p["area"]].append(p)
        if not area_filter:
            # 영역 버튼 목록
            lines_ = [f"📞 {dept_name}\n\n영역(과)를 선택하세요:"]
            area_btns = []
            for area, members in by_area.items():
                emoji = members[0].get("area_emoji","") if members else ""
                lines_.append(f"  {emoji} {area} ({len(members)}명)")
                area_btns.append(_kb(f"{emoji} {area}"[:14], f"영역보기 {dept_name} {area}"))
            area_btns += [_kb("🔍 이름 검색","이름검색"), _kb("🏠 메뉴로","메뉴")]
            return _kt("\n".join(lines_), area_btns[:6])
        # 1명 1카드
        members = by_area.get(area_filter, [])
        if not members:
            return _kt(f"{area_filter} 선생님 정보가 없어요.",
                       [_kb(f"↩️ 목록", f"학부상세 {dept_name}"), _kb("🏠 메뉴로","메뉴")])
        PAGE  = 10
        chunk = members[area_page*PAGE:(area_page+1)*PAGE]
        cards = []
        for p in chunk:
            emoji = p.get("area_emoji","")
            desc_lines = []
            if p.get("office"): desc_lines.append(f"🏢 {p['office']}")
            if p.get("email"):  desc_lines.append(f"✉️ {p['email']}")
            if p.get("phone"):  desc_lines.append(f"📞 {p['phone']}")
            cards.append({"title": f"{emoji} {p.get('name','?')}",
                          "description": "\n".join(desc_lines) or "연락처 없음"})
        nav = []
        if (area_page+1)*PAGE < len(members):
            nav.append(_kb(f"▶ 다음({(area_page+1)*PAGE}/{len(members)})",
                           f"영역보기 {dept_name} {area_filter} {area_page+1}"))
        if area_page > 0:
            nav.append(_kb("◀ 이전", f"영역보기 {dept_name} {area_filter} {area_page-1}"))
        nav += [_kb("↩️ 영역 목록", f"학부상세 {dept_name}"), _kb("🏠 메뉴로","메뉴")]
        return _kc(cards, nav[:4])

    if utt=="이름검색":
        return _kt("🔍 Search Teacher\n\nFormat: search [name]\nPartial name works too." if _kakao_lang.get(uid,"ko")=="en" else "🔍 선생님 이름 검색\n\n형식: 검색 홍길동\n성함 일부만 입력해도 됩니다.",
                   [_kb("↩️ 학부 목록","선생님 연락망")])

    if utt.startswith("검색 "):
        q = utt[3:].strip()
        results = [p for p in PROFESSORS if q in p.get("name","")]
        if not results:
            return _kt(f"'{q}' 선생님을 찾을 수 없어요.",
                       [_kb("🔍 다시 검색","이름검색"), _kb("🏠 메뉴로","메뉴")])
        cards = []
        for p in results[:5]:
            desc = f"🏛 {p.get('dept','')} / {p.get('area','')}"
            if p.get("email"):  desc += f"\n✉️ {p['email']}"
            if p.get("phone"):  desc += f"\n📞 {p['phone']}"
            if p.get("office"): desc += f"\n🏢 {p['office']}"
            cards.append({"title":f"📞 {p.get('name','?')}","description":desc})
        return _kc(cards, [_kb("🔍 다시 검색","이름검색"), _kb("🏠 메뉴로","메뉴")])

    # ── 형설관 공실 ──────────────────────────────────────────
    if utt=="공실조회":
        now    = datetime.datetime.now()
        today  = ["월","화","수","목","금","토","일"][now.weekday()]
        now_t  = now.hour*60+now.minute
        if today not in ["월","화","수","목","금"]:
            msg_wknd = "No classes on weekends 😊\nAll Hyungseol rooms available." if _kakao_lang.get(uid,"ko")=="en" else "오늘은 주말이라 수업이 없어요 😊\n형설관 강의실 모두 사용 가능합니다."
            return _kt(msg_wknd, [_kb(_t(uid,"menu_btn"),"메뉴")])
        hyung = sorted(r for r in CLASSROOMS if r.startswith("형"))
        # 1학년 자습 전용 강의실 (평일 07:30~09:30)
        STUDY_ROOMS = {"형3206","형3207","형3302","형3303","형3304",
                       "형3305","형3306","형3307","형3402","형3404","형3405"}
        is_study_time = (7*60+30 <= now_t <= 9*60+30)
        # 교시 계산: 교시 시작~종료 (50분 수업)
        # 1교시 08:50, 2교시 09:50, ..., 4교시 11:50, (점심), 5교시 13:40...
        # 교시 시작-종료 (50분 수업, 10분 쉬는시간)
        # 1~4교시: 08:50~12:30, 점심 12:30~13:40, 5~9교시: 13:40~18:30
        # 10~12교시: 자습/야간 수업 (학교마다 다름 → 기준 없음)
        period_slots = [
            (8,50,9,40),(9,50,10,40),(10,50,11,40),(11,50,12,40),
            (13,40,14,30),(14,40,15,30),(15,40,16,30),(16,40,17,30),(17,40,18,30),
        ]
        cur_p = None
        for i,(sh,sm,eh,em_) in enumerate(period_slots):
            t_start = sh*60+sm
            t_end   = eh*60+em_
            if t_start <= now_t <= t_end:
                cur_p = i+1
                break
        day_start = 8*60+50
        day_end   = 18*60+30  # 9교시 종료
        after_school  = now_t > day_end
        before_school = now_t < day_start
        if after_school or before_school:
            lines = [f"🏫 형설관 강의실 현황 ({today}요일 {'일과 전' if before_school else '일과 후'})\n"]
            lines.append("현재 수업이 없습니다. 모든 강의실 사용 가능합니다.\n")
            if is_study_time:
                lines.append("⚠️ 07:30~09:30 1학년 자습시간\n아래 강의실은 사용 불가:")
                for r in sorted(STUDY_ROOMS): lines.append(f"  • {r}")
                lines.append("\n아래 강의실은 사용 가능:")
                avail = [r for r in hyung if r not in STUDY_ROOMS]
                for r in avail: lines.append(f"  • {r}")
            else:
                for r in hyung: lines.append(f"  • {r}")
        elif cur_p:
            sk  = f"{today}{cur_p}"
            vac = [r for r in hyung if sk not in CLASSROOMS.get(r,{})]
            occ = [r for r in hyung if sk in CLASSROOMS.get(r,{})]
            # 자습시간 중이면 자습 전용실 제외
            if is_study_time:
                vac = [r for r in vac if r not in STUDY_ROOMS]
            lines = [f"🏫 형설관 공실 ({today}요일 {cur_p}교시)\n"]
            if is_study_time:
                lines.append("⚠️ 자습시간: 1학년 전용 강의실 제외\n")
            lines.append(f"✅ 빈 강의실 ({len(vac)}개):")
            for r in vac: lines.append(f"  • {r}")
            if occ:
                lines.append(f"\n❌ 사용 중 ({len(occ)}개):")
                for r in occ[:6]: lines.append(f"  • {r}: {CLASSROOMS[r].get(sk,'')[:12]}")
                if len(occ)>6: lines.append(f"  ... 외 {len(occ)-6}개")
        else:
            # 쉬는 시간
            lines = [f"🏫 형설관 ({today}요일) — 쉬는 시간\n다음 교시 시간표:\n"]
            for r in hyung:
                tc = sorted([k for k in CLASSROOMS.get(r,{}) if k.startswith(today)])
                lines.append(f"• {r}: {' '.join(k[1:]+'교시' for k in tc) if tc else '공실'}")
        return _kt("\n".join(lines), [_kb("🔄 새로고침","공실조회"), _kb("🏠 메뉴로","메뉴")])

    # ── 관리자 전용 ──────────────────────────────────────────
    if utt=="신청현황" and is_admin:
        total  = sum(len(v) for v in _trade_requests.values())
        period = "열림 ✅" if is_period_open() else "닫힘 ❌"
        sm     = defaultdict(int)
        for reqs in _trade_requests.values():
            for r in reqs: sm[r["course_key"].split("(")[0]]+=1
        top = "\n".join(f"  {k}: {v}건" for k,v in sorted(sm.items(),key=lambda x:-x[1])[:5])
        return _kt(f"📋 신청 현황\n기간: {period}\n학생: {len(_trade_requests)}명\n건수: {total}건\n\n인기 과목:\n{top or '없음'}",
                   _admin_menu())

    if utt=="기간열기" and is_admin:
        _trade_period["open"] = True
        return _kt("🟢 트레이드 신청 기간을 열었습니다.", _admin_menu())

    if utt=="기간닫기" and is_admin:
        _trade_period["open"] = False
        return _kt("🔴 트레이드 신청 기간을 닫았습니다.", _admin_menu())

    if utt=="매칭실행" and is_admin:
        all_reqs=[]; em={}
        for s_id,reqs in _trade_requests.items():
            for r in reqs:
                all_reqs.append({"sid":r["sid"],"sname":r["name"],
                                  "course_key":r["course_key"],
                                  "from_section":r["from_section"],
                                  "to_sections":r["to_sections"]})
                if r.get("enrolled"): em[s_id]=r["enrolled"]
        if not all_reqs:
            return _kt("No trade requests found." if _kakao_lang.get(uid,"ko")=="en" else "신청 내역이 없어요.", _admin_menu())
        try:
            chosen,cycles = solve(all_reqs,em)
            result = build_result(all_reqs,chosen,cycles)
            _last_match.clear(); _last_match.update(result)
            apply_match_to_timetable(result)   # 시간표 자동 반영
            ts,tr = result["total_success"],result["total_requests"]
            pct   = round(ts/tr*100) if tr else 0
            return _kt(f"✅ 매칭 완료!\n{ts}/{tr}건 성사 ({pct}%)\n순환 그룹: {len(result['cycles'])}개",
                       _admin_menu())
        except Exception as e:
            return _kt(f"⚠️ 오류: {str(e)[:80]}", _admin_menu())

    # ── 폴백 ────────────────────────────────────────────────
    # 알 수 없는 입력 → 현재 단계 초기화 후 메뉴 표시
    _kakao_step.pop(uid, None)
    return _kt(f"{name}님, 아래 메뉴에서 선택해주세요.", _menu())


# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG","true").lower()=="true"
    app.run(host="0.0.0.0", port=port, debug=debug)
