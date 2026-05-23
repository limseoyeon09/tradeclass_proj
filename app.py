"""
TradeTable v7 — KSA 시간표 트레이드 시스템
- 가온누리 연결 없음: 학번으로 로그인, 시간표는 data.json 사용
- 학생: 트레이드 신청 → 대기 상태
- 관리자: 일괄 매칭 실행 / 신청 기간 설정 / 분반 변경 승인
- 교수 연락처 탭
"""
import json, os, re, time
from flask import Flask, request, jsonify, render_template
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Tuple
from collections import defaultdict

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
ADMIN_PW = os.environ.get("ADMIN_PW", "tradetable-admin-2026")

_HERE = os.path.dirname(__file__)

with open(os.path.join(_HERE, "static", "data.json"), encoding="utf-8") as f:
    _DB = json.load(f)
COURSES  = _DB["courses"]   # 분반 정원·슬롯 (알고리즘용)
STUDENTS = _DB["students"]  # 학생 수강 정보 (시간표용)

with open(os.path.join(_HERE, "static", "professors.json"), encoding="utf-8") as f:
    PROFESSORS = json.load(f)

with open(os.path.join(_HERE, "static", "classrooms.json"), encoding="utf-8") as f:
    CLASSROOMS = json.load(f)   # {강의실: {"월3": "과목_분반", ...}}

# ── 인메모리 저장소 ────────────────────────────────────────────
_trade_requests: Dict[str, List] = {}
_section_change_requests: List = []
_approved_changes: Dict[str, List] = {}
_extra_requests: List = []
_approved_extras: Dict[str, List] = {}
_last_match: Dict = {}
_trade_period: Dict = {"open": False, "start": None, "end": None, "note": ""}

# ── 유틸 ────────────────────────────────────────────────────────
def _norm(sid: str) -> str:
    m = re.match(r"^(\d{2})(\d{3})$", sid.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else sid.strip()

def is_period_open() -> bool:
    if _trade_period["open"]:
        return True
    s, e = _trade_period.get("start"), _trade_period.get("end")
    if s and e:
        return s <= time.time() <= e
    return False

# ── 시간표 조회 (data.json 기반) ────────────────────────────────
def get_timetable(sid: str) -> Tuple[List[Dict], str]:
    """
    data.json의 STUDENTS에서 수강 정보를 가져옴.
    승인된 추가과목·분반 변경도 반영.
    """
    s = STUDENTS.get(sid)
    if not s:
        return [], f"학번 {sid}을 찾을 수 없습니다. (등록된 학생: {len(STUDENTS)}명)"

    enrolled = []
    for e in s.get("enrolled", []):
        ck = f"{e['course']}({e['grade']})"
        enrolled.append({
            "course":  e["course"],
            "grade":   e["grade"],
            "section": e["section"],
            "ck":      ck,
            "slots":   e.get("slots", []),
        })

    # 분반 변경 승인 항목 반영
    for ch in _approved_changes.get(sid, []):
        enrolled = [e for e in enrolled if e["ck"] != ch["ck"]]
        enrolled.append(ch)

    # 추가과목 반영
    for ex in _approved_extras.get(sid, []):
        if not any(e["ck"] == ex["ck"] for e in enrolled):
            enrolled.append(ex)

    name = s.get("name", sid)
    return enrolled, ""

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
    """
    학번으로만 로그인. 가온누리 연결 없음.
    등록된 학번이면 바로 입장, 비밀번호 불필요.
    관리자는 ADMIN_PW 입력 시 진입.
    """
    d = request.get_json(force=True)
    username = d.get("username","").strip()
    password = d.get("password","").strip()

    if not username:
        return jsonify({"ok":False,"error":"학번을 입력하세요."}), 400

    # 관리자
    if password == ADMIN_PW:
        sid  = _norm(username)
        name = STUDENTS.get(sid, {}).get("name", "관리자")
        return jsonify({"ok":True,"sid":sid,"name":name,"admin":True})

    # 학생: 학번만 확인 (비밀번호 불필요)
    sid = _norm(username)
    s   = STUDENTS.get(sid)
    if not s:
        return jsonify({"ok":False,"error":f"등록되지 않은 학번입니다: {sid}"}), 401

    return jsonify({"ok":True,"sid":sid,"name":s["name"],"admin":False})

@app.route("/api/timetable/<sid>")
def api_timetable(sid):
    enrolled, err = get_timetable(sid)
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

# ================================================================
# TradeTable 카카오 스킬 서버 - 최종판
# app.py 맨 아래 if __name__ 블록 바로 위에 붙여넣기
# 기존 카카오 코드 전부 삭제 후 이것만
# ================================================================
import datetime as _dt

_kakao_users: Dict[str, str] = {}   # uid → sid
_kakao_admin: Dict[str, bool] = {}  # uid → is_admin


# ── 응답 헬퍼 ──────────────────────────────────────────────────

def _kt(msg, btns=None):
    """텍스트 응답"""
    r = {"version":"2.0","template":{"outputs":[{"simpleText":{"text":msg}}]}}
    if btns: r["template"]["quickReplies"] = btns
    return jsonify(r)

def _kc(cards, btns=None):
    """캐러셀 카드 (가로 스크롤)"""
    r = {"version":"2.0","template":{"outputs":[{"carousel":{"type":"basicCard","items":cards}}]}}
    if btns: r["template"]["quickReplies"] = btns
    return jsonify(r)

def _kb(label, msg=None):
    return {"label": label[:14], "action":"message", "messageText": msg or label}

def _menu():
    return [_kb("📅 내 시간표"), _kb("🔀 트레이드 신청"),
            _kb("📊 결과 확인"), _kb("📞 선생님 연락망"),
            _kb("🏫 형설관 공실","공실조회")]

def _admin_menu():
    return [_kb("📋 신청 현황"), _kb("▶ 매칭 실행"),
            _kb("🟢 기간 열기", "기간열기"), _kb("🔴 기간 닫기", "기간닫기")]


# ── 시간표 카드 ────────────────────────────────────────────────

def _timetable_cards(enrolled):
    """요일별 캐러셀 카드. 오늘 요일 첫 번째. 강의실 표시. 공강 표시."""
    DAYS = ['월','화','수','목','금']
    today = ["월","화","수","목","금","토","일"][_dt.datetime.now().weekday()]

    # 슬롯맵: (요일, 교시) → "과목명 (강의실)"
    sc = {}
    for e in enrolled:
        room = e.get('room', '')
        for sl in e.get('slots', []):
            room_sl = sl.get('r', room)
            loc = f" ({room_sl})" if room_sl else ""
            sc[(sl['d'], sl['p'])] = f"{e['course']}{loc}"

    day_order = ([today] + [d for d in DAYS if d != today]) if today in DAYS else DAYS

    cards = []
    for d in day_order:
        lines = []
        for p in range(1, 10):
            if (d, p) in sc:
                lines.append(f"{p}교시: {sc[(d,p)]}")
            else:
                lines.append(f"{p}교시: 공강")
        label = f"{'오늘 ' if d==today else ''}{d}요일"
        cards.append({"title": f"📅 {label}", "description": "\n".join(lines)})

    # 전체 과목 카드 (강의실 포함)
    course_lines = []
    for e in enrolled:
        room = e.get('room', '')
        loc = f" ({room})" if room else ""
        course_lines.append(f"• {e['course']} {e['section']}분반{loc}")
    cards.append({"title": "📚 전체 수강 과목", "description": "\n".join(course_lines)})
    return cards


# ── 메인 라우트 ────────────────────────────────────────────────

@app.route("/kakao", methods=["POST"])
def kakao_skill():
    d   = request.get_json(force=True)
    utt = d.get("userRequest",{}).get("utterance","").strip()
    uid = d.get("userRequest",{}).get("user",{}).get("id","")
    sid = _kakao_users.get(uid)
    is_admin = _kakao_admin.get(uid, False)

    # ── 학번 미등록 ────────────────────────────────────────────
    if not sid:
        m = re.match(r"^(\d{2})-?(\d{3})$", utt)
        if m:
            new_sid = f"{m.group(1)}-{m.group(2)}"
            if new_sid in STUDENTS:
                _kakao_users[uid] = new_sid
                name = STUDENTS[new_sid]["name"]
                return _kt(f"✅ 인증 완료!\n{name}님 환영해요 👋\n\n무엇을 도와드릴까요?", _menu())
            return _kt(f"❌ {utt}은 등록되지 않은 학번이에요.\n다시 입력해주세요.\n예) 25-096")
        return _kt("안녕하세요! TradeTable입니다 🟡\n\n한과영 학번을 입력해주세요.\n예) 25-096")

    # ── 메뉴 ───────────────────────────────────────────────────
    if utt in ["메뉴","처음으로","홈","시작","메인"]:
        name = STUDENTS.get(sid,{}).get("name","")
        btns = _menu()
        if is_admin: btns = _admin_menu() + [_kb("🏠 학생 메뉴", "메뉴_학생")]
        return _kt(f"{name}님, 무엇을 도와드릴까요?", btns)

    if utt == "메뉴_학생":
        return _kt("학생 메뉴입니다.", _menu())

    # ── 내 시간표 ──────────────────────────────────────────────
    if utt in ["📅 내 시간표","내 시간표","시간표"]:
        enrolled, _ = get_timetable(sid)
        if not enrolled:
            return _kt("시간표 정보가 없어요.", [_kb("🏠 메뉴로","메뉴")])
        return _kc(_timetable_cards(enrolled),
                   [_kb("🔀 트레이드 신청"), _kb("🏠 메뉴로","메뉴")])

    # 다른 학생 시간표: "시간표 25-006"
    m_o = re.search(r"(\d{2})-?(\d{3})", utt)
    if m_o and ("시간표" in utt or "조회" in utt):
        o_sid = f"{m_o.group(1)}-{m_o.group(2)}"
        if o_sid in STUDENTS:
            enrolled, _ = get_timetable(o_sid)
            cards = _timetable_cards(enrolled)
            cards[0]["title"] = f"📅 {STUDENTS[o_sid]['name']}({o_sid})"
            return _kc(cards, [_kb("🏠 메뉴로","메뉴")])
        return _kt(f"❌ {o_sid}은 등록되지 않은 학번이에요.", [_kb("🏠 메뉴로","메뉴")])

    # ── 트레이드 신청 ─────────────────────────────────────────
    if utt in ["🔀 트레이드 신청","트레이드 신청","신청"]:
        if not is_period_open():
            return _kt(
                "⚠️ 현재 트레이드 신청 기간이 아닙니다.\n\n"
                "관리자에게 문의하거나 신청 기간이 열릴 때까지 기다려주세요.",
                [_kb("🏠 메뉴로","메뉴")]
            )
        enrolled, _ = get_timetable(sid)
        tradeable = [e for e in enrolled
                     if e["ck"] in COURSES and len(COURSES[e["ck"]]["sections"]) >= 2]
        if not tradeable:
            return _kt("트레이드 가능한 과목이 없어요.", [_kb("🏠 메뉴로","메뉴")])
        existing = _trade_requests.get(sid, [])
        lines = ["🔀 트레이드 신청\n신청할 과목을 선택하세요:\n"]
        btns = []
        for e in tradeable:
            req = next((r for r in existing if r["course_key"]==e["ck"]), None)
            mark = " ✅" if req else ""
            lines.append(f"• {e['course']} ({e['section']}분반){mark}")
            label = (e['course'] + mark)[:14]
            btns.append(_kb(label, f"과목선택 {e['ck']} {e['section']}"))
        if existing:
            btns.append(_kb("📤 신청 제출","신청제출"))
            btns.append(_kb("❌ 신청 취소","신청취소"))
        btns.append(_kb("🏠 메뉴로","메뉴"))
        return _kt("\n".join(lines), btns[:6])

    # 과목 선택
    if utt.startswith("과목선택 "):
        parts = utt.split(" ")
        if len(parts) < 3:
            return _kt("오류가 발생했어요.", [_kb("🏠 메뉴로","메뉴")])
        ck, my_sec = parts[1], parts[2]
        cn = ck.split("(")[0]
        info = COURSES.get(ck, {})
        btns = []
        for sec in sorted(info.get("sections",{}).keys(), key=lambda x: int(x)):
            if sec == my_sec: continue
            sl = info.get("slots",{}).get(sec,[])
            sl_str = " ".join(f"{s['d']}{s['p']}교시" for s in sl[:2])
            label = (f"{sec}분반 ({sl_str})" if sl_str else f"{sec}분반")[:14]
            btns.append(_kb(label, f"분반선택 {ck} {my_sec} {sec}"))
        btns.append(_kb("↩️ 돌아가기","🔀 트레이드 신청"))
        return _kt(f"📚 {cn} (현재 {my_sec}분반)\n이동할 분반을 선택하세요:", btns[:6])

    # 분반 선택 → 저장
    if utt.startswith("분반선택 "):
        parts = utt.split(" ")
        if len(parts) < 4:
            return _kt("오류가 발생했어요.", [_kb("🏠 메뉴로","메뉴")])
        ck, from_sec, to_sec = parts[1], parts[2], parts[3]
        cn = ck.split("(")[0]
        enrolled, _ = get_timetable(sid)
        name = STUDENTS.get(sid,{}).get("name", sid)
        reqs = _trade_requests.get(sid, [])
        req = next((r for r in reqs if r["course_key"]==ck), None)
        if req:
            if to_sec not in req["to_sections"] and len(req["to_sections"]) < 3:
                req["to_sections"].append(to_sec)
        else:
            reqs.append({"sid":sid,"name":name,"course_key":ck,
                         "from_section":from_sec,"to_sections":[to_sec],
                         "enrolled":enrolled,"ts":int(time.time())})
            _trade_requests[sid] = reqs
        req = next((r for r in _trade_requests.get(sid,[]) if r["course_key"]==ck), None)
        wish_str = " / ".join(f"{i+1}지망 {s}분반" for i,s in enumerate(req["to_sections"]))
        return _kt(f"✅ {cn} 신청!\n{from_sec}분반 → {wish_str}",
                   [_kb("🔀 다른 과목 추가","🔀 트레이드 신청"),
                    _kb("📤 신청 제출","신청제출"),
                    _kb("🏠 메뉴로","메뉴")])

    # 신청 제출
    if utt == "신청제출":
        reqs = _trade_requests.get(sid, [])
        if not reqs:
            return _kt("신청된 트레이드가 없어요.", [_kb("🏠 메뉴로","메뉴")])
        lines = ["📤 신청 완료!\n"]
        for r in reqs:
            cn = r["course_key"].split("(")[0]
            wishes = " / ".join(f"{i+1}지망 {s}분반" for i,s in enumerate(r["to_sections"]))
            lines.append(f"• {cn}: {r['from_section']}분반 → {wishes}")
        lines.append("\n관리자 매칭 후 '결과 확인'에서 확인하세요.")
        return _kt("\n".join(lines), [_kb("📊 결과 확인"), _kb("🏠 메뉴로","메뉴")])

    # 신청 취소
    if utt == "신청취소":
        _trade_requests.pop(sid, None)
        return _kt("신청이 취소됐습니다.", _menu())

    # ── 결과 확인 ─────────────────────────────────────────────
    if utt in ["📊 결과 확인","결과 확인","결과"]:
        if not _last_match.get("students"):
            return _kt("아직 매칭 결과가 없어요.\n관리자가 매칭을 실행하면 여기서 확인할 수 있어요.",
                       [_kb("🏠 메뉴로","메뉴")])
        my = next((s for s in _last_match["students"] if s["sid"]==sid), None)
        if not my:
            return _kt("이번 매칭에 내 신청 내역이 없어요.", [_kb("🏠 메뉴로","메뉴")])
        lines = ["📊 내 트레이드 결과\n"]
        for t in my.get("successes",[]):
            cn = t["course_key"].split("(")[0]
            lines.append(f"✅ {cn}: {t['from']}분반 → {t['to']}분반 성공!")
        for t in my.get("failures",[]):
            cn = t["course_key"].split("(")[0]
            lines.append(f"❌ {cn}: 실패 — 교수님께 직접 문의하세요.")
        btns = [_kb("🏠 메뉴로","메뉴")]
        if my.get("failures"): btns.insert(0, _kb("📞 선생님 연락망"))
        return _kt("\n".join(lines), btns)

    # ── 선생님 연락망 ─────────────────────────────────────────
    if utt in ["📞 선생님 연락망","선생님 연락망","연락망","연락처"]:
        return _kt(
            "📞 선생님 연락망\n\n학부를 선택하거나 이름으로 검색하세요.\n\n"
            "이름 검색 방법: 검색 홍길동",
            [_kb("🏛 수리정보과학부","수리정보과학부"),
             _kb("🔭 물리지구과학부","물리지구과학부"),
             _kb("📚 인문예술학부","인문예술학부"),
             _kb("🧪 화학생물학부","화학생물학부"),
             _kb("🏠 메뉴로","메뉴")]
        )

    # 학부별 교수 목록 (캐러셀)
    if utt in ["수리정보과학부","물리지구과학부","인문예술학부","화학생물학부"]:
        profs = [p for p in PROFESSORS if p["dept"] == utt]
        from collections import defaultdict as _dd
        by_area = _dd(list)
        for p in profs: by_area[p["area"]].append(p)
        cards = []
        for area, members in by_area.items():
            desc_lines = []
            for p in members:
                name_str  = p.get("name","")  or "이름 없음"
                email_str = p.get("email","") or ""
                phone_str = p.get("phone","") or ""
                off_str   = p.get("office","") or ""
                line = f"• {name_str}"
                if off_str:   line += f" ({off_str})"
                if email_str: desc_lines.append(line + f"\n  ✉️ {email_str}")
                else:         desc_lines.append(line)
                if phone_str: desc_lines[-1] += f"\n  📞 {phone_str}"
            cards.append({"title":f"📞 {area}", "description":"\n".join(desc_lines)})
        return _kc(cards, [_kb("🔍 이름 검색","이름검색안내"),
                            _kb("↩️ 학부 목록","📞 선생님 연락망"),
                            _kb("🏠 메뉴로","메뉴")])

    # 이름 검색 안내
    if utt in ["이름검색안내","이름 검색"]:
        return _kt("🔍 선생님 이름 검색\n\n아래 형식으로 입력하세요:\n검색 홍길동\n\n성함 일부만 입력해도 검색됩니다.",
                   [_kb("↩️ 학부 목록","📞 선생님 연락망")])

    # 이름 검색 실행
    if utt.startswith("검색 "):
        name_q = utt[3:].strip()
        results = [p for p in PROFESSORS if name_q in p.get("name","")]
        if not results:
            return _kt(f"'{name_q}' 선생님을 찾을 수 없어요.\n성함 일부만 입력해도 검색됩니다.",
                       [_kb("🔍 다시 검색","이름검색안내"), _kb("🏠 메뉴로","메뉴")])
        cards = []
        for p in results[:5]:
            name_str  = p.get("name","")  or "이름 없음"
            dept_str  = f"{p.get('dept','')} / {p.get('area','')}"
            email_str = p.get("email","") or "이메일 없음"
            phone_str = p.get("phone","") or ""
            off_str   = p.get("office","") or ""
            desc = f"🏛 {dept_str}\n✉️ {email_str}"
            if phone_str: desc += f"\n📞 {phone_str}"
            if off_str:   desc += f"\n🏢 {off_str}"
            cards.append({"title": f"📞 {name_str}", "description": desc})
        return _kc(cards, [_kb("🔍 다시 검색","이름검색안내"), _kb("🏠 메뉴로","메뉴")])

    # ── 관리자 로그인 ─────────────────────────────────────────
    if utt.startswith("관리자 "):
        pw = utt[4:].strip()
        if pw == ADMIN_PW:
            _kakao_admin[uid] = True
            total = sum(len(v) for v in _trade_requests.values())
            period = "열림 ✅" if is_period_open() else "닫힘 ❌"
            return _kt(
                f"🛠 관리자 모드 활성화\n\n"
                f"신청 학생: {len(_trade_requests)}명\n"
                f"총 신청 건수: {total}건\n"
                f"신청 기간: {period}\n\n"
                f"아래 버튼에서 작업을 선택하세요.",
                _admin_menu()
            )
        _kakao_admin[uid] = False
        return _kt("❌ 비밀번호가 틀렸습니다.")

    # ── 관리자 전용 기능 ─────────────────────────────────────
    if utt == "📋 신청 현황":
        if not is_admin:
            return _kt("관리자만 사용할 수 있어요.", [_kb("🏠 메뉴로","메뉴")])
        total = sum(len(v) for v in _trade_requests.values())
        period = "열림 ✅" if is_period_open() else "닫힘 ❌"
        from collections import Counter
        sm = Counter()
        for reqs in _trade_requests.values():
            for r in reqs: sm[r["course_key"].split("(")[0]] += 1
        top = "\n".join(f"  {k}: {v}건" for k,v in sm.most_common(5))
        return _kt(
            f"📋 트레이드 신청 현황\n\n"
            f"신청 기간: {period}\n"
            f"신청 학생: {len(_trade_requests)}명\n"
            f"총 신청: {total}건\n\n"
            f"많이 신청된 과목:\n{top or '없음'}",
            _admin_menu()
        )

    if utt == "기간열기":
        if not is_admin:
            return _kt("관리자만 사용할 수 있어요.", [_kb("🏠 메뉴로","메뉴")])
        _trade_period["open"] = True
        return _kt("🟢 트레이드 신청 기간을 열었습니다.\n학생들이 지금부터 신청할 수 있어요.", _admin_menu())

    if utt == "기간닫기":
        if not is_admin:
            return _kt("관리자만 사용할 수 있어요.", [_kb("🏠 메뉴로","메뉴")])
        _trade_period["open"] = False
        return _kt("🔴 트레이드 신청 기간을 닫았습니다.", _admin_menu())

    if utt == "▶ 매칭 실행":
        if not is_admin:
            return _kt("관리자만 사용할 수 있어요.", [_kb("🏠 메뉴로","메뉴")])
        all_reqs = []
        em = {}
        for s_id, reqs in _trade_requests.items():
            for r in reqs:
                all_reqs.append({"sid":r["sid"],"sname":r["name"],
                                  "course_key":r["course_key"],
                                  "from_section":r["from_section"],
                                  "to_sections":r["to_sections"]})
                if r.get("enrolled"): em[s_id] = r["enrolled"]
        if not all_reqs:
            return _kt("신청된 트레이드가 없어요.", _admin_menu())
        try:
            chosen, cycles = solve(all_reqs, em)
            result = build_result(all_reqs, chosen, cycles)
            _last_match.clear()
            _last_match.update(result)
            total_s = result["total_success"]
            total_r = result["total_requests"]
            pct = round(total_s/total_r*100) if total_r else 0
            return _kt(
                f"✅ 매칭 완료!\n\n"
                f"성사: {total_s}/{total_r}건 ({pct}%)\n"
                f"순환 교환 그룹: {len(result['cycles'])}개\n\n"
                f"학생들이 '결과 확인'으로 확인할 수 있습니다.",
                _admin_menu()
            )
        except Exception as e:
            return _kt(f"⚠️ 오류: {str(e)[:100]}", _admin_menu())

    # ── 형설관 공실 조회 ──────────────────────────────────────
    if utt in ["공실조회", "🏫 형설관 공실", "공실", "빈강의실", "형설관"]:
        today = ["월","화","수","목","금","토","일"][_dt.datetime.now().weekday()]
        now_h = _dt.datetime.now().hour
        now_m = _dt.datetime.now().minute
        period_starts = [(8,50),(9,50),(10,50),(11,50),(13,40),(14,40),(15,40),(16,40),(17,40)]
        cur_p = None
        for i,(h,m) in enumerate(period_starts):
            t_start = h*60+m
            t_end   = t_start+50
            t_now   = now_h*60+now_m
            if t_start <= t_now <= t_end:
                cur_p = i+1
                break

        if today not in ["월","화","수","목","금"]:
            return _kt("오늘은 주말이라 수업이 없어요 😊", [_kb("🏠 메뉴로","메뉴")])

        hyung_rooms = sorted([r for r in CLASSROOMS if r.startswith("형")])
        slot_key = f"{today}{cur_p}" if cur_p else None

        if slot_key:
            vacant   = [r for r in hyung_rooms if slot_key not in CLASSROOMS.get(r,{})]
            occupied = [r for r in hyung_rooms if slot_key in CLASSROOMS.get(r,{})]
            lines = [f"🏫 형설관 공실 ({today}요일 {cur_p}교시)\n"]
            lines.append(f"✅ 빈 강의실 ({len(vacant)}개):")
            for r in vacant: lines.append(f"  • {r}")
            if occupied:
                lines.append(f"\n❌ 사용 중 ({len(occupied)}개):")
                for r in occupied[:5]:
                    course = CLASSROOMS[r].get(slot_key,'')[:12]
                    lines.append(f"  • {r}: {course}")
                if len(occupied) > 5:
                    lines.append(f"  ... 외 {len(occupied)-5}개")
        else:
            lines = [f"🏫 형설관 강의실 ({today}요일)\n지금은 쉬는 시간이에요.\n"]
            for r in hyung_rooms:
                today_classes = [(k,v) for k,v in CLASSROOMS.get(r,{}).items() if k.startswith(today)]
                if today_classes:
                    slots_str = " ".join(f"{k[1]}교시" for k,_ in sorted(today_classes))
                    lines.append(f"• {r}: {slots_str}")
                else:
                    lines.append(f"• {r}: 공실")

        return _kt("\n".join(lines), [_kb("🔄 새로고침","공실조회"), _kb("🏠 메뉴로","메뉴")])

    # ── 폴백 ──────────────────────────────────────────────────
    return _kt("죄송해요, 잘 이해하지 못했어요 😅\n아래 메뉴에서 선택해주세요.", _menu())
