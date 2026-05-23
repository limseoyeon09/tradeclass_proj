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
# 카카오 오픈빌더 스킬 서버 v3
# app.py 맨 아래 if __name__ 블록 바로 위에 붙여넣기
# (기존 카카오 코드 전부 삭제 후 이것만 넣기)
# ================================================================
import datetime as _dt

_kakao_users: Dict[str, str] = {}   # kakao uid → student sid


def _ktext(msg, btns=None):
    resp = {"version": "2.0", "template": {"outputs": [{"simpleText": {"text": msg}}]}}
    if btns:
        resp["template"]["quickReplies"] = btns
    return jsonify(resp)


def _kcarousel(cards, btns=None):
    """카드 여러 장 — 가로로 밀어서 보기"""
    resp = {
        "version": "2.0",
        "template": {
            "outputs": [{
                "carousel": {
                    "type": "basicCard",
                    "items": cards
                }
            }]
        }
    }
    if btns:
        resp["template"]["quickReplies"] = btns
    return jsonify(resp)


def _kbtn(label, msg=None):
    return {"label": label, "action": "message", "messageText": msg or label}


def _menu():
    return [
        _kbtn("📅 내 시간표"),
        _kbtn("🔀 트레이드 신청"),
        _kbtn("📊 결과 확인"),
        _kbtn("📞 선생님 연락망"),
    ]


def _today_name():
    """오늘 요일 한글"""
    return ["월", "화", "수", "목", "금", "토", "일"][_dt.datetime.now().weekday()]


def _make_timetable(enrolled):
    """
    카카오 캐러셀 카드 형식으로 시간표 반환.
    오늘 요일 카드를 첫 번째로, 나머지 요일을 순서대로.
    """
    DAYS = ['월', '화', '수', '목', '금']
    today = _today_name()

    # 슬롯맵 구성
    sc = {}   # (요일, 교시) → {course, section}
    for e in enrolled:
        for sl in e.get('slots', []):
            sc[(sl['d'], sl['p'])] = {
                'course': e['course'],
                'section': e['section']
            }

    # 요일 순서 — 오늘 먼저
    if today in DAYS:
        day_order = [today] + [d for d in DAYS if d != today]
    else:
        day_order = DAYS

    cards = []
    for d in day_order:
        day_classes = []
        for p in range(1, 10):
            info = sc.get((d, p))
            if info:
                day_classes.append(f"{p}교시: {info['course']} ({info['section']}분반)")

        label = f"{'오늘 ' if d == today else ''}{d}요일"
        if day_classes:
            body = "\n".join(day_classes)
        else:
            body = "수업 없음"

        cards.append({
            "title": f"📅 {label}",
            "description": body
        })

    return cards


def _make_timetable_text(enrolled):
    """전체 수강 과목 목록 (텍스트)"""
    lines = [f"📚 전체 수강 과목 ({len(enrolled)}개)\n"]
    for e in enrolled:
        slots = " ".join(f"{s['d']}{s['p']}" for s in e.get('slots', [])[:3])
        lines.append(f"• {e['course']} {e['section']}분반")
        if slots:
            lines.append(f"  ({slots}교시)")
    return "\n".join(lines)


@app.route("/kakao", methods=["POST"])
def kakao_skill():
    d   = request.get_json(force=True)
    utt = d.get("userRequest", {}).get("utterance", "").strip()
    uid = d.get("userRequest", {}).get("user", {}).get("id", "")
    sid = _kakao_users.get(uid)

    # ── 학번 미등록 ──────────────────────────────────────────────
    if not sid:
        m = re.match(r"^(\d{2})-?(\d{3})$", utt)
        if m:
            new_sid = f"{m.group(1)}-{m.group(2)}"
            if new_sid in STUDENTS:
                _kakao_users[uid] = new_sid
                name = STUDENTS[new_sid]["name"]
                return _ktext(
                    f"✅ 인증 완료!\n{name}님 환영해요 👋\n\n무엇을 도와드릴까요?",
                    _menu()
                )
            return _ktext(
                f"❌ {new_sid}은 등록되지 않은 학번이에요.\n"
                f"다시 입력해주세요.\n예) 25-096"
            )
        return _ktext(
            "안녕하세요! TradeTable입니다 🟡\n\n"
            "한과영 학번을 입력해주세요.\n예) 25-096"
        )

    # ── 메뉴 ─────────────────────────────────────────────────────
    if utt in ["메뉴", "처음으로", "홈", "시작"]:
        name = STUDENTS.get(sid, {}).get("name", "")
        return _ktext(f"{name}님, 무엇을 도와드릴까요?", _menu())

    # ── 내 시간표 (캐러셀 카드) ──────────────────────────────────
    if utt in ["📅 내 시간표", "내 시간표", "시간표"]:
        enrolled, _ = get_timetable(sid)
        if not enrolled:
            return _ktext("시간표 정보가 없어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        cards = _make_timetable(enrolled)
        # 전체 과목 카드 추가
        cards.append({
            "title": "📚 전체 수강 과목",
            "description": "\n".join(
                f"• {e['course']} {e['section']}분반" for e in enrolled
            )
        })
        return _kcarousel(cards, [
            _kbtn("🔀 트레이드 신청"),
            _kbtn("🏠 메뉴로", "메뉴"),
        ])

    # ── 다른 학생 시간표 조회 ────────────────────────────────────
    m_other = re.search(r"(\d{2})-?(\d{3})", utt)
    if m_other and ("시간표" in utt or "조회" in utt):
        other_sid = f"{m_other.group(1)}-{m_other.group(2)}"
        if other_sid in STUDENTS:
            enrolled, _ = get_timetable(other_sid)
            other_name = STUDENTS[other_sid]["name"]
            cards = _make_timetable(enrolled)
            cards.append({
                "title": "📚 전체 수강 과목",
                "description": "\n".join(
                    f"• {e['course']} {e['section']}분반" for e in enrolled
                )
            })
            # 첫 카드 제목에 이름 추가
            if cards:
                cards[0]["title"] = f"📅 {other_name}({other_sid})"
            return _kcarousel(cards, [_kbtn("🏠 메뉴로", "메뉴")])
        return _ktext(f"❌ {other_sid}은 등록되지 않은 학번이에요.", [_kbtn("🏠 메뉴로", "메뉴")])

    # ── 트레이드 신청 ─────────────────────────────────────────────
    if utt in ["🔀 트레이드 신청", "트레이드 신청", "신청"]:
        if not is_period_open():
            return _ktext(
                "⚠️ 현재 트레이드 신청 기간이 아닙니다.\n"
                "신청 기간이 열리면 다시 시도해주세요.",
                [_kbtn("🏠 메뉴로", "메뉴")]
            )
        enrolled, _ = get_timetable(sid)
        tradeable = [e for e in enrolled if e["ck"] in COURSES and len(COURSES[e["ck"]]["sections"]) >= 2]
        if not tradeable:
            return _ktext("트레이드 가능한 과목이 없어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        existing = _trade_requests.get(sid, [])
        lines = ["🔀 트레이드 신청\n신청할 과목을 선택하세요:\n"]
        btns = []
        for e in tradeable:
            req = next((r for r in existing if r["course_key"] == e["ck"]), None)
            mark = " ✅" if req else ""
            lines.append(f"• {e['course']} ({e['section']}분반){mark}")
            btns.append(_kbtn(f"{e['course']}{mark}", f"과목선택 {e['ck']} {e['section']}"))
        if existing:
            btns.append(_kbtn("📤 신청 제출", "신청제출"))
            btns.append(_kbtn("❌ 신청 취소", "신청취소"))
        btns.append(_kbtn("🏠 메뉴로", "메뉴"))
        return _ktext("\n".join(lines), btns[:6])

    # ── 과목 선택 → 분반 목록 ─────────────────────────────────────
    if utt.startswith("과목선택 "):
        parts = utt.split(" ")
        if len(parts) < 3:
            return _ktext("오류가 발생했어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        ck, my_sec = parts[1], parts[2]
        cn = ck.split("(")[0]
        info = COURSES.get(ck, {})
        secs = info.get("sections", {})
        slots_map = info.get("slots", {})
        btns = []
        for sec in sorted(secs.keys(), key=lambda x: int(x)):
            if sec == my_sec:
                continue
            sl = slots_map.get(sec, [])
            sl_str = " ".join(f"{s['d']}{s['p']}교시" for s in sl[:2])
            label = f"{sec}분반 ({sl_str})" if sl_str else f"{sec}분반"
            btns.append(_kbtn(label, f"분반선택 {ck} {my_sec} {sec}"))
        btns.append(_kbtn("↩️ 돌아가기", "🔀 트레이드 신청"))
        return _ktext(f"📚 {cn} (현재 {my_sec}분반)\n이동할 분반을 선택하세요:", btns[:6])

    # ── 분반 선택 → 저장 ──────────────────────────────────────────
    if utt.startswith("분반선택 "):
        parts = utt.split(" ")
        if len(parts) < 4:
            return _ktext("오류가 발생했어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        ck, from_sec, to_sec = parts[1], parts[2], parts[3]
        cn = ck.split("(")[0]
        enrolled, _ = get_timetable(sid)
        name = STUDENTS.get(sid, {}).get("name", sid)
        reqs = _trade_requests.get(sid, [])
        req = next((r for r in reqs if r["course_key"] == ck), None)
        if req:
            if to_sec not in req["to_sections"] and len(req["to_sections"]) < 3:
                req["to_sections"].append(to_sec)
        else:
            reqs.append({"sid": sid, "name": name, "course_key": ck,
                         "from_section": from_sec, "to_sections": [to_sec],
                         "enrolled": enrolled, "ts": int(time.time())})
            _trade_requests[sid] = reqs
        req = next((r for r in _trade_requests.get(sid, []) if r["course_key"] == ck), None)
        wish_str = " / ".join(f"{i+1}지망 {s}분반" for i, s in enumerate(req["to_sections"]))
        return _ktext(
            f"✅ {cn} 신청!\n{from_sec}분반 → {wish_str}",
            [_kbtn("🔀 다른 과목 추가", "🔀 트레이드 신청"),
             _kbtn("📤 신청 제출", "신청제출"),
             _kbtn("🏠 메뉴로", "메뉴")]
        )

    # ── 신청 제출 ──────────────────────────────────────────────────
    if utt == "신청제출":
        reqs = _trade_requests.get(sid, [])
        if not reqs:
            return _ktext("신청된 트레이드가 없어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        lines = ["📤 신청 완료!\n"]
        for r in reqs:
            cn = r["course_key"].split("(")[0]
            wishes = " / ".join(f"{i+1}지망 {s}분반" for i, s in enumerate(r["to_sections"]))
            lines.append(f"• {cn}: {r['from_section']}분반 → {wishes}")
        lines.append("\n관리자 매칭 후 '결과 확인'에서 확인하세요.")
        return _ktext("\n".join(lines), [_kbtn("📊 결과 확인"), _kbtn("🏠 메뉴로", "메뉴")])

    # ── 신청 취소 ──────────────────────────────────────────────────
    if utt == "신청취소":
        _trade_requests.pop(sid, None)
        return _ktext("신청이 취소됐습니다.", _menu())

    # ── 결과 확인 ─────────────────────────────────────────────────
    if utt in ["📊 결과 확인", "결과 확인", "결과"]:
        if not _last_match:
            return _ktext("아직 매칭 결과가 없어요.\n관리자가 매칭을 실행하면 여기서 확인할 수 있어요.",
                          [_kbtn("🏠 메뉴로", "메뉴")])
        my = next((s for s in _last_match.get("students", []) if s["sid"] == sid), None)
        if not my:
            return _ktext("이번 매칭에 내 신청 내역이 없어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        lines = ["📊 내 트레이드 결과\n"]
        for t in my.get("successes", []):
            cn = t["course_key"].split("(")[0]
            lines.append(f"✅ {cn}: {t['from']}분반 → {t['to']}분반 성공!")
        for t in my.get("failures", []):
            cn = t["course_key"].split("(")[0]
            lines.append(f"❌ {cn}: 실패\n   교수님께 직접 분반 변경을 문의하세요.")
        btns = [_kbtn("🏠 메뉴로", "메뉴")]
        if my.get("failures"):
            btns.insert(0, _kbtn("📞 선생님 연락망"))
        return _ktext("\n".join(lines), btns)

    # ── 선생님 연락망 ─────────────────────────────────────────────
    if utt in ["📞 선생님 연락망", "선생님 연락망", "연락망", "연락처"]:
        return _ktext(
            "📞 선생님 연락망\n\n학부를 선택하거나 이름으로 검색하세요.\n\n이름 검색: 검색 홍길동",
            [_kbtn("🏛 수리정보과학부", "수리정보과학부"),
             _kbtn("🔭 물리지구과학부", "물리지구과학부"),
             _kbtn("📚 인문예술학부",   "인문예술학부"),
             _kbtn("🧪 화학생물학부",   "화학생물학부"),
             _kbtn("🏠 메뉴로",         "메뉴")]
        )

    # ── 학부별 교수 조회 (캐러셀) ────────────────────────────────
    if utt in ["수리정보과학부", "물리지구과학부", "인문예술학부", "화학생물학부"]:
        profs = [p for p in PROFESSORS if p["dept"] == utt]
        from collections import defaultdict as _dd
        by_area = _dd(list)
        for p in profs:
            by_area[p["area"]].append(p)

        cards = []
        for area, members in by_area.items():
            lines = []
            for p in members:
                name_str = p.get("name", "이름 없음")
                email_str = p.get("email", "")
                phone_str = p.get("phone", "")
                office_str = p.get("office", "")
                entry = f"• {name_str}"
                if email_str: entry += f"\n  ✉️ {email_str}"
                if phone_str: entry += f"\n  📞 {phone_str}"
                if office_str: entry += f"\n  🏢 {office_str}"
                lines.append(entry)
            cards.append({
                "title": f"📞 {utt} — {area}",
                "description": "\n".join(lines) if lines else "정보 없음"
            })

        return _kcarousel(cards, [
            _kbtn("🔍 이름 검색", "이름검색안내"),
            _kbtn("↩️ 학부 목록", "📞 선생님 연락망"),
            _kbtn("🏠 메뉴로", "메뉴"),
        ])

    # ── 이름 검색 안내 ────────────────────────────────────────────
    if utt == "이름검색안내":
        return _ktext("🔍 선생님 이름 검색\n\n아래 형식으로 입력하세요:\n검색 홍길동",
                      [_kbtn("↩️ 학부 목록", "📞 선생님 연락망")])

    # ── 이름 검색 실행 ────────────────────────────────────────────
    if utt.startswith("검색 "):
        name_q = utt[3:].strip()
        results = [p for p in PROFESSORS if name_q in p.get("name", "")]
        if not results:
            return _ktext(f"'{name_q}' 선생님을 찾을 수 없어요.",
                          [_kbtn("🔍 다시 검색", "이름검색안내"), _kbtn("🏠 메뉴로", "메뉴")])
        cards = []
        for p in results[:5]:
            lines = []
            if p.get("dept"):  lines.append(f"🏛 {p['dept']} / {p.get('area','')}")
            if p.get("email"):  lines.append(f"✉️ {p['email']}")
            if p.get("phone"):  lines.append(f"📞 {p['phone']}")
            if p.get("office"): lines.append(f"🏢 {p['office']}")
            cards.append({
                "title": f"📞 {p.get('name','이름없음')}",
                "description": "\n".join(lines) if lines else "정보 없음"
            })
        return _kcarousel(cards, [_kbtn("🔍 다시 검색", "이름검색안내"), _kbtn("🏠 메뉴로", "메뉴")])

    # ── 관리자 ────────────────────────────────────────────────────
    if utt.startswith("관리자 "):
        pw = utt.replace("관리자 ", "").strip()
        if pw == ADMIN_PW:
            _kakao_users[uid + "_admin"] = "1"
            total = sum(len(v) for v in _trade_requests.values())
            period = "열림 ✅" if is_period_open() else "닫힘 ❌"
            return _ktext(
                f"🛠 관리자 메뉴\n\n"
                f"신청 학생: {len(_trade_requests)}명\n"
                f"총 신청: {total}건\n"
                f"신청 기간: {period}",
                [_kbtn("▶ 매칭 실행", "관리자매칭")]
            )
        return _ktext("비밀번호가 틀렸습니다.")

    if utt == "관리자매칭":
        global _last_match
        all_reqs = []
        em = {}
        for s_id, reqs in _trade_requests.items():
            for r in reqs:
                all_reqs.append({"sid": r["sid"], "sname": r["name"],
                                  "course_key": r["course_key"],
                                  "from_section": r["from_section"],
                                  "to_sections": r["to_sections"]})
                if r.get("enrolled"): em[s_id] = r["enrolled"]
        if not all_reqs:
            return _ktext("신청된 트레이드가 없어요.", [_kbtn("🏠 메뉴로", "메뉴")])
        try:
            chosen, cycles = solve(all_reqs, em)
            _last_match = build_result(all_reqs, chosen, cycles)
            total_s = _last_match["total_success"]
            total_r = _last_match["total_requests"]
            pct = round(total_s / total_r * 100) if total_r else 0
            return _ktext(
                f"✅ 매칭 완료!\n"
                f"{total_s}/{total_r}건 성사 ({pct}%)\n"
                f"순환 그룹: {len(_last_match['cycles'])}개",
                [_kbtn("🏠 메뉴로", "메뉴")]
            )
        except Exception as e:
            return _ktext(f"오류: {e}", [_kbtn("🏠 메뉴로", "메뉴")])

    # ── 폴백 ─────────────────────────────────────────────────────
    return _ktext("죄송해요, 잘 이해하지 못했어요 😅\n아래 메뉴에서 선택해주세요.", _menu())
