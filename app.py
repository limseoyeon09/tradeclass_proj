"""
TradeTable v6 — KSA 시간표 트레이드 시스템
- 학생: 트레이드 신청 → 대기 상태
- 관리자: 일괄 매칭 실행 / 신청 기간 설정 / 분반 변경 승인
- 교수 연락처 탭 (강의계획서 PDF 기반)
"""
import json, os, re, time
from flask import Flask, request, jsonify, render_template
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Tuple
from collections import defaultdict
from datetime import datetime

try:
    import requests as _req
    from bs4 import BeautifulSoup
    HAS_NET = True
except ImportError:
    HAS_NET = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
ADMIN_PW = os.environ.get("ADMIN_PW", "tradetable-admin-2026")

KSAIN_LOGIN  = "https://ksain.net/pages/cert/login.php"
KSAIN_TABLER = "https://ksain.net/web/tabler/tabler_d.php"

_HERE = os.path.dirname(__file__)

with open(os.path.join(_HERE, "static", "data.json"), encoding="utf-8") as f:
    _DB = json.load(f)
COURSES = _DB["courses"]

with open(os.path.join(_HERE, "static", "professors.json"), encoding="utf-8") as f:
    PROFESSORS = json.load(f)

# ── 인메모리 저장소 ───────────────────────────────────────────
# 트레이드 신청: {sid: [{course_key, from_section, to_sections, name, enrolled, ts}]}
_trade_requests: Dict[str, List] = {}
# 분반 변경 신청 대기: [{sid, name, course, grade, section, teacher, slots, ts}]
_section_change_requests: List = []
# 승인된 분반 변경: {sid: [{course, grade, section, ck, slots}]}
_approved_changes: Dict[str, List] = {}
# 추가과목 신청 대기: [{sid, course, grade, section, teacher, slots, ts}]
_extra_requests: List = []
# 승인된 추가과목: {sid: [{...}]}
_approved_extras: Dict[str, List] = {}
# 마지막 매칭 결과
_last_match: Dict = {}
# 신청 기간 설정
_trade_period: Dict = {"open": False, "start": None, "end": None, "note": ""}

# ═══════════════════════════════════════════════════════════════
# 유틸
# ═══════════════════════════════════════════════════════════════
def _norm(sid: str) -> str:
    m = re.match(r"^(\d{2})(\d{3})$", sid.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else sid.strip()

def is_period_open() -> bool:
    """신청 기간 확인"""
    if _trade_period["open"]:
        return True
    s, e = _trade_period.get("start"), _trade_period.get("end")
    if s and e:
        now = time.time()
        return s <= now <= e
    return False

# ═══════════════════════════════════════════════════════════════
# 가온누리 로그인 (백엔드 프록시)
# ═══════════════════════════════════════════════════════════════
_sessions: Dict[str, object] = {}

def ksain_login(username: str, password: str) -> Tuple[bool, str, str, str]:
    sid = _norm(username)
    if not HAS_NET:
        return False, "", "", "requests/beautifulsoup4 패키지 필요"
    sess = _req.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    try:
        resp = sess.post(KSAIN_LOGIN,
            data={"id": username, "pw": password, "api_key": "f5rtxw-89mzkp-c2qbvd-l7shnj-a4p1re"},
            timeout=10, allow_redirects=True)
    except Exception as e:
        return False, "", "", f"가온누리 연결 실패: {e}"
    if any(k in resp.text for k in ["비밀번호", "아이디를", "로그인 실패", "incorrect"]):
        return False, "", "", "아이디 또는 비밀번호가 틀렸습니다."
    name = sid
    nm = re.search(r"\d{2}-\d{3}\(([^)]+)\)", resp.text)
    if nm: name = nm.group(1)
    _sessions[sid] = sess
    return True, sid, name, ""

COURSE_RE = re.compile(r"([가-힣\w]+(?:\(EC\))?)\((\d+)\)_(\d+)")

def _parse_tabler(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser")
    DAYS = ["월","화","수","목","금"]
    found = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3: continue
        day_col = {}
        for ci, cell in enumerate(rows[0].find_all(["th","td"])):
            txt = cell.get_text(strip=True)
            for d in DAYS:
                if d in txt: day_col[d] = ci
        if not day_col: continue
        n_cols = max(day_col.values()) + 2
        occupied = {}; cur_period = None
        for ri, row in enumerate(rows[1:], 1):
            cells = row.find_all(["th","td"])
            if not cells: continue
            pm = re.match(r"^(\d+)", cells[0].get_text(strip=True))
            if pm: cur_period = int(pm.group(1))
            if cur_period is None: continue
            col_ptr = 0; cell_ptr = 0
            while cell_ptr < len(cells):
                while (ri, col_ptr) in occupied: col_ptr += 1
                if col_ptr > n_cols + 5: break
                cell = cells[cell_ptr]
                try: rs = int(cell.get("rowspan",1)); cs = int(cell.get("colspan",1))
                except: rs = cs = 1
                txt = cell.get_text(" ", strip=True)
                for ro in range(rs):
                    for co in range(cs): occupied[(ri+ro, col_ptr+co)] = True
                day = next((d for d,dc in day_col.items() if dc==col_ptr), None)
                if day:
                    for mo in COURSE_RE.finditer(txt):
                        cname,grade,section = mo.group(1).strip(),mo.group(2),mo.group(3)
                        ck = f"{cname}({grade})"; key = f"{ck}_{section}"
                        if key not in found:
                            found[key] = {"course":cname,"grade":grade,"section":section,"ck":ck,"slots":[]}
                        sl = {"d":day,"p":cur_period}
                        if sl not in found[key]["slots"]: found[key]["slots"].append(sl)
                col_ptr += cs; cell_ptr += 1
    return list(found.values())

def fetch_timetable(sid: str) -> Tuple[List, str]:
    sess = _sessions.get(sid)
    if not sess: return [], "로그인 세션이 없습니다."
    try:
        resp = sess.get(f"{KSAIN_TABLER}?stuId={sid}", timeout=10)
        if resp.status_code != 200: return [], f"HTTP {resp.status_code}"
        items = _parse_tabler(resp.text)
        if not items: return [], f"파싱 결과 없음. HTML: {resp.text[:200]}"
        # 분반 변경 승인 항목 병합
        for ch in _approved_changes.get(sid, []):
            items = [e for e in items if e["ck"] != ch["ck"]]
            items.append(ch)
        for ex in _approved_extras.get(sid, []):
            if not any(e["ck"]==ex["ck"] for e in items):
                items.append(ex)
        return items, ""
    except Exception as e:
        return [], str(e)

# ═══════════════════════════════════════════════════════════════
# 알고리즘
# ═══════════════════════════════════════════════════════════════
@dataclass
class _Cand:
    sid: str; sname: str; ck: str; fr: str; to: str; pri: int

def _conflict(a, b):
    return bool({(s["d"],s["p"]) for s in a} & {(s["d"],s["p"]) for s in b})

def solve(reqs, enrolled_map):
    cands = [_Cand(r["sid"],r["sname"],r["course_key"],r["from_section"],to,i)
             for r in reqs for i,to in enumerate(r["to_sections"])]
    def gk(c): return (c.sid,c.ck)
    groups = [sorted(list(g),key=lambda x:x.pri)
              for _,g in groupby(sorted(cands,key=gk),key=gk)]
    state = {f"{ck}|{sec}":cnt for ck,ci in COURSES.items() for sec,cnt in ci["sections"].items()}
    best=[]; bs=[-1]
    def sc(ch): return sum(3-c.pri for c in ch)
    def apply(c,st):
        kf,kt=f"{c.ck}|{c.fr}",f"{c.ck}|{c.to}"
        if st.get(kf,0)<=0: return False
        st[kf]-=1; st[kt]=st.get(kt,0)+1; return True
    def revert(c,st): st[f"{c.ck}|{c.fr}"]+=1; st[f"{c.ck}|{c.to}"]-=1
    def balanced(st): return all(st.get(f"{ck}|{sec}",0)==cnt for ck,ci in COURSES.items() for sec,cnt in ci["sections"].items())
    def ok(c):
        to_sl=COURSES.get(c.ck,{}).get("slots",{}).get(c.to,[])
        if not to_sl: return True
        other=[sl for e in enrolled_map.get(c.sid,[]) if e.get("ck","")!=c.ck for sl in e.get("slots",[])]
        return not _conflict(to_sl,other)
    def bt(idx,ch,st):
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
    by_ck=defaultdict(list)
    for c in best: by_ck[c.ck].append(c)
    cycles=[]
    for ck,ts in by_ck.items():
        g={t.fr:t for t in ts}; vis=set()
        for start in list(g.keys()):
            if start in vis: continue
            path,cur,seen=[],start,set()
            while cur in g and cur not in seen:
                seen.add(cur); t=g[cur]
                path.append({"sid":t.sid,"name":t.sname,"from":t.fr,"to":t.to,"pri":t.pri}); cur=t.to
            if cur==start and len(path)>=2:
                for p in path: vis.add(p["from"])
                cycles.append({"course":ck,"members":path})
    return best,cycles

def build_result(reqs,chosen,cycles):
    cm={(c.sid,c.ck):c for c in chosen}; per={}
    for r in reqs:
        sid=r["sid"]
        if sid not in per: per[sid]={"sid":sid,"name":r["sname"],"successes":[],"failures":[]}
        c=cm.get((sid,r["course_key"]))
        if c:
            to_slots=COURSES.get(r["course_key"],{}).get("slots",{}).get(c.to,[])
            per[sid]["successes"].append({"course_key":r["course_key"],"from":r["from_section"],
                "to":c.to,"priority":c.pri,"to_list":r["to_sections"],"to_slots":to_slots})
        else:
            per[sid]["failures"].append({"course_key":r["course_key"],"from":r["from_section"],"to_list":r["to_sections"]})
    return {"total_success":len(chosen),"total_requests":len(reqs),"students":list(per.values()),"cycles":cycles}

# ── 테스트 케이스 ─────────────────────────────────────────────
_E06=[{"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]},{"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},{"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},{"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},{"course":"철학","grade":"2","section":"2","ck":"철학(2)","slots":[{"d":"월","p":6},{"d":"월","p":7}]},{"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]}]
_E96=[{"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]},{"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},{"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},{"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},{"course":"철학","grade":"2","section":"3","ck":"철학(2)","slots":[{"d":"금","p":3},{"d":"금","p":4}]},{"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]}]
TEST_CASES=[
    {"id":"tc1","title":"같은 분반끼리 교환","description":"두 학생 모두 영어Ⅲ 4분반","tag":"edge","expected":"성사 0건","requests":[{"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]},{"sid":"25-027","sname":"김준아","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]}],"enrolled_by_sid":{"25-006":_E06,"25-027":_E96}},
    {"id":"tc2","title":"✅ 2인 직접 교환","description":"구정준(5→9) ↔ 임서연(9→5)","tag":"success","expected":"성사 2건","requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},{"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","3"]}],"enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc3","title":"✅ 3인 순환 교환","description":"A(1→2),B(2→3),C(3→1)","tag":"success","expected":"성사 3건","requests":[{"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["2"]},{"sid":"26-001","sname":"강재원","course_key":"체육3(2)","from_section":"2","to_sections":["3"]},{"sid":"26-006","sname":"김도윤","course_key":"체육3(2)","from_section":"3","to_sections":["1"]}],"enrolled_by_sid":{"26-008":[{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}],"26-001":[{"course":"체육3","grade":"2","section":"2","ck":"체육3(2)","slots":[{"d":"월","p":8}]}],"26-006":[{"course":"체육3","grade":"2","section":"3","ck":"체육3(2)","slots":[{"d":"화","p":4}]}]}},
    {"id":"tc4","title":"🚫 시간표 충돌","description":"체육3 3분반 → 영어Ⅲ 충돌","tag":"conflict","expected":"성사 0건","requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["3"]},{"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["3"]}],"enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc5","title":"◐ 복수 과목 부분 성사","description":"체육3 성공/영어Ⅲ 실패","tag":"partial","expected":"2건/1건","requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9"]},{"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5"]},{"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5"]}],"enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc6","title":"🎯 1지망 우선 최적화","description":"3명 체육3 지망 최적","tag":"priority","expected":"성사 3건","requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},{"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","1"]},{"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["9","5"]}],"enrolled_by_sid":{"25-006":[{"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]}],"25-096":[{"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]}],"26-008":[{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}]}},
]

# ═══════════════════════════════════════════════════════════════
# Flask 라우트
# ═══════════════════════════════════════════════════════════════
@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.get_json(force=True)
    username = d.get("username","").strip()
    password = d.get("password","").strip()
    if not username or not password:
        return jsonify({"ok":False,"error":"학번과 비밀번호를 입력하세요."}), 400
    if password == ADMIN_PW:
        return jsonify({"ok":True,"sid":_norm(username),"name":"관리자","admin":True})
    if password == "__paste_mode__":
        sid = _norm(username)
        return jsonify({"ok":True,"sid":sid,"name":sid,"admin":False})
    ok, sid, name, err = ksain_login(username, password)
    if not ok: return jsonify({"ok":False,"error":err}), 401
    return jsonify({"ok":True,"sid":sid,"name":name,"admin":False})

@app.route("/api/timetable/<sid>")
def api_timetable(sid):
    enrolled, err = fetch_timetable(sid)
    if err and not enrolled: return jsonify({"ok":False,"error":err}), 400
    return jsonify({"ok":True,"enrolled":enrolled,"warning":err})

@app.route("/api/courses")
def api_courses():
    return jsonify({k:v for k,v in COURSES.items() if len(v["sections"])>=2})

@app.route("/api/professors")
def api_professors():
    return jsonify(PROFESSORS)

# ── 트레이드 신청 ─────────────────────────────────────────────
@app.route("/api/trade-request", methods=["POST"])
def api_trade_request():
    if not is_period_open():
        return jsonify({"ok":False,"error":"현재 트레이드 신청 기간이 아닙니다."}), 403
    d = request.get_json(force=True)
    sid=d.get("sid","").strip(); name=d.get("name","").strip()
    reqs=d.get("requests",[]); enrolled=d.get("enrolled",[])
    if not sid or not reqs: return jsonify({"ok":False,"error":"필수 데이터 누락"}), 400
    _trade_requests[sid] = [{**r,"sid":sid,"name":name,"enrolled":enrolled,"ts":int(time.time())} for r in reqs]
    total = sum(len(v) for v in _trade_requests.values())
    return jsonify({"ok":True,"message":f"트레이드 신청 {len(reqs)}건 접수됨. 관리자 매칭 후 결과를 확인하세요.","total_pending":total})

@app.route("/api/trade-request/<sid>", methods=["DELETE"])
def api_cancel(sid):
    _trade_requests.pop(sid,None)
    return jsonify({"ok":True})

@app.route("/api/trade-requests/my/<sid>")
def api_my(sid):
    return jsonify({"requests":_trade_requests.get(sid,[]),"total_all":sum(len(v) for v in _trade_requests.values())})

@app.route("/api/trade-requests/summary")
def api_summary():
    sm = {}
    for reqs in _trade_requests.values():
        for r in reqs: sm[r["course_key"]] = sm.get(r["course_key"],0)+1
    return jsonify({"summary":sm,"total":sum(len(v) for v in _trade_requests.values()),"students":len(_trade_requests)})

@app.route("/api/last-match")
def api_last_match():
    if not _last_match: return jsonify({"ok":False,"error":"아직 매칭이 실행되지 않았습니다."}), 404
    return jsonify({"ok":True,"result":_last_match})

# ── 분반 변경 신청 (학생) ────────────────────────────────────
@app.route("/api/section-change-request", methods=["POST"])
def api_section_change():
    d = request.get_json(force=True)
    sid=d.get("sid","").strip(); name=d.get("name","").strip()
    course=d.get("course","").strip(); grade=d.get("grade","?").strip()
    from_sec=d.get("from_section","").strip(); to_sec=d.get("to_section","").strip()
    reason=d.get("reason","").strip()
    if not all([sid,course,from_sec,to_sec]):
        return jsonify({"ok":False,"error":"필수 항목 누락"}), 400
    _section_change_requests.append({
        "sid":sid,"name":name,"course":course,"grade":grade,
        "from_section":from_sec,"to_section":to_sec,
        "reason":reason,"ts":int(time.time()),"status":"pending"
    })
    return jsonify({"ok":True,"message":"분반 변경 신청이 접수됐습니다. 관리자 승인 후 반영됩니다."})

@app.route("/api/section-change-requests/my/<sid>")
def api_my_section_change(sid):
    mine = [r for r in _section_change_requests if r["sid"]==sid]
    return jsonify(mine)

# ── 추가과목 신청 (학생) ──────────────────────────────────────
@app.route("/api/extra-request", methods=["POST"])
def api_extra_req():
    d = request.get_json(force=True)
    sid=d.get("sid","").strip(); course=d.get("course","").strip()
    grade=d.get("grade","?"); section=d.get("section","").strip()
    teacher=d.get("teacher",""); slots=d.get("slots",[])
    if not all([sid,course,section,slots]):
        return jsonify({"ok":False,"error":"필수 항목 누락"}), 400
    _extra_requests.append({"sid":sid,"name":d.get("name",""),"course":course,"grade":grade,
        "section":section,"teacher":teacher,"ck":f"{course}({grade})","slots":slots,"ts":int(time.time())})
    return jsonify({"ok":True,"message":"추가신청 접수. 관리자 승인 후 반영됩니다."})

# ═══════════════════════════════════════════════════════════════
# 관리자 API
# ═══════════════════════════════════════════════════════════════

# ── 매칭 실행 ────────────────────────────────────────────────
@app.route("/api/admin/run-match", methods=["POST"])
def api_run_match():
    global _last_match
    all_reqs=[]; em={}
    for sid,reqs in _trade_requests.items():
        for r in reqs:
            all_reqs.append({"sid":r["sid"],"sname":r["name"],"course_key":r["course_key"],
                "from_section":r["from_section"],"to_sections":r["to_sections"]})
            if r.get("enrolled"): em[sid]=r["enrolled"]
    if not all_reqs: return jsonify({"ok":False,"error":"신청된 트레이드가 없습니다."}), 400
    try:
        chosen,cycles=solve(all_reqs,em)
        _last_match=build_result(all_reqs,chosen,cycles)
        return jsonify({"ok":True,"result":_last_match})
    except Exception as e:
        import traceback; return jsonify({"error":str(e),"trace":traceback.format_exc()}), 500

@app.route("/api/admin/clear-requests", methods=["POST"])
def api_clear():
    _trade_requests.clear(); return jsonify({"ok":True})

# ── 신청 기간 설정 ────────────────────────────────────────────
@app.route("/api/admin/period", methods=["GET","POST"])
def api_period():
    global _trade_period
    if request.method == "GET":
        p = dict(_trade_period)
        p["is_open"] = is_period_open()
        return jsonify(p)
    d = request.get_json(force=True)
    # open: True/False (수동 개폐)
    if "open" in d: _trade_period["open"] = bool(d["open"])
    if "start" in d: _trade_period["start"] = d["start"]
    if "end"   in d: _trade_period["end"]   = d["end"]
    if "note"  in d: _trade_period["note"]  = d["note"]
    return jsonify({"ok":True,"period":_trade_period})

# ── 전체 신청 현황 ────────────────────────────────────────────
@app.route("/api/admin/all-requests")
def api_all_reqs():
    result = []
    for sid,reqs in _trade_requests.items():
        for r in reqs:
            result.append({"sid":r["sid"],"name":r["name"],
                "course_key":r["course_key"],"from":r["from_section"],
                "to":r["to_sections"],"ts":r["ts"]})
    return jsonify(result)

# ── 분반 변경 승인/거절 ──────────────────────────────────────
@app.route("/api/admin/section-change-requests")
def api_sc_reqs():
    return jsonify(_section_change_requests)

@app.route("/api/admin/approve-section-change", methods=["POST"])
def api_approve_sc():
    d = request.get_json(force=True)
    idx = d.get("idx",-1)
    if idx < 0 or idx >= len(_section_change_requests):
        return jsonify({"ok":False,"error":"항목 없음"}), 404
    req = _section_change_requests[idx]
    req["status"] = "approved"
    ck = f"{req['course']}({req['grade']})"
    slots = COURSES.get(ck,{}).get("slots",{}).get(req["to_section"],[])
    entry = {"course":req["course"],"grade":req["grade"],"section":req["to_section"],"ck":ck,"slots":slots}
    _approved_changes.setdefault(req["sid"],[]).append(entry)
    _section_change_requests.pop(idx)
    return jsonify({"ok":True,"message":f"{req['sid']} {req['course']} {req['from_section']}→{req['to_section']}분반 승인"})

@app.route("/api/admin/reject-section-change", methods=["POST"])
def api_reject_sc():
    d = request.get_json(force=True)
    idx = d.get("idx",-1)
    if idx < 0 or idx >= len(_section_change_requests):
        return jsonify({"ok":False,"error":"항목 없음"}), 404
    _section_change_requests[idx]["status"] = "rejected"
    _section_change_requests.pop(idx)
    return jsonify({"ok":True})

# ── 추가과목 승인/거절 ────────────────────────────────────────
@app.route("/api/admin/extra-requests")
def api_extra_reqs(): return jsonify(_extra_requests)

@app.route("/api/admin/approve-extra", methods=["POST"])
def api_approve_extra():
    d=request.get_json(force=True); idx=d.get("idx",-1)
    if idx<0 or idx>=len(_extra_requests): return jsonify({"ok":False,"error":"항목 없음"}),404
    entry=dict(_extra_requests.pop(idx))
    _approved_extras.setdefault(entry["sid"],[]).append(entry)
    return jsonify({"ok":True,"message":f"{entry['sid']} {entry['course']} 승인"})

@app.route("/api/admin/reject-extra", methods=["POST"])
def api_reject_extra():
    d=request.get_json(force=True); idx=d.get("idx",-1)
    if idx<0 or idx>=len(_extra_requests): return jsonify({"ok":False,"error":"항목 없음"}),404
    _extra_requests.pop(idx); return jsonify({"ok":True})

# ── 테스트 ────────────────────────────────────────────────────
@app.route("/api/admin/test-cases")
def api_tcs():
    return jsonify([{"id":t["id"],"title":t["title"],"description":t["description"],"tag":t["tag"],"expected":t["expected"]} for t in TEST_CASES])

@app.route("/api/admin/run-test/<tc_id>", methods=["POST"])
def api_rt(tc_id):
    tc=next((t for t in TEST_CASES if t["id"]==tc_id),None)
    if not tc: return jsonify({"error":"없음"}),404
    try:
        chosen,cycles=solve(tc["requests"],tc["enrolled_by_sid"])
        return jsonify({"ok":True,"result":build_result(tc["requests"],chosen,cycles),
            "test_case":{"id":tc["id"],"title":tc["title"],"expected":tc["expected"],"tag":tc["tag"]}})
    except Exception as e:
        import traceback; return jsonify({"error":str(e),"trace":traceback.format_exc()}),500

@app.route("/api/admin/run-all-tests", methods=["POST"])
def api_rat():
    results=[]
    for tc in TEST_CASES:
        try:
            chosen,cycles=solve(tc["requests"],tc["enrolled_by_sid"])
            results.append({"id":tc["id"],"title":tc["title"],"tag":tc["tag"],"expected":tc["expected"],
                "result":build_result(tc["requests"],chosen,cycles),"error":None})
        except Exception as e:
            results.append({"id":tc["id"],"title":tc["title"],"tag":tc["tag"],"expected":tc["expected"],"result":None,"error":str(e)})
    return jsonify({"ok":True,"tests":results})

@app.route("/health")
def health():
    return jsonify({"status":"ok","trade_requests":sum(len(v) for v in _trade_requests.values()),
        "period_open":is_period_open(),"professors":len(PROFESSORS)})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    debug=os.environ.get("FLASK_DEBUG","true").lower()=="true"
    app.run(host="0.0.0.0",port=port,debug=debug)
