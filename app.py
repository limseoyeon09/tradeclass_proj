"""
TradeTable v4 — KSA 시간표 트레이드 시스템
가온누리 tabler.php?stuId=학번 GET 방식으로 시간표 파싱
"""
import json, re, os
from flask import Flask, request, jsonify, render_template
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Tuple

try:
    import requests as _req
    from bs4 import BeautifulSoup
    HAS_NET = True
except ImportError:
    HAS_NET = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

KSAIN_LOGIN  = "https://ksain.net/pages/cert/login.php"
KSAIN_TABLER = "https://ksain.net/pages/tabler/tabler.php"
KSAIN_APIKEY = "f5rtxw-89mzkp-c2qbvd-l7shnj-a4p1re"
ADMIN_PW     = os.environ.get("ADMIN_PW", "tradetable-admin-2026")

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "static", "data.json"), encoding="utf-8") as f:
    _DB = json.load(f)

STUDENTS = _DB["students"]
COURSES  = _DB["courses"]

# 가온누리 로그인 세션 (sid → requests.Session)
_sessions: Dict[str, object] = {}

# 추가신청 대기/승인 저장소 (메모리; 재시작 시 초기화)
_pending:  Dict[str, List[Dict]] = {}   # {sid: [{...}]}
_approved: Dict[str, List[Dict]] = {}   # {sid: [{...}]}

# ═══════════════════════════════════════════════════════════════
# 유틸
# ═══════════════════════════════════════════════════════════════

def _norm(raw: str) -> str:
    m = re.match(r"^(\d{2})(\d{3})$", raw.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else raw.strip()

# ═══════════════════════════════════════════════════════════════
# 가온누리 로그인
# ═══════════════════════════════════════════════════════════════

def ksain_login(username: str, password: str):
    """반환: (ok, sid, name, warning, ksain_live)"""
    sid = _norm(username)

    if not HAS_NET:
        if sid in STUDENTS:
            return True, sid, STUDENTS[sid]["name"], "requests/bs4 없음 — 오프라인 모드", False
        return False, None, None, "등록되지 않은 학번입니다.", False

    sess = _req.Session()
    sess.headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    try:
        resp = sess.post(
            KSAIN_LOGIN,
            data={"id": username, "pw": password, "api_key": KSAIN_APIKEY},
            timeout=8, allow_redirects=True,
        )
    except Exception as e:
        # 네트워크 오류 → 엑셀 폴백
        if sid in STUDENTS:
            return True, sid, STUDENTS[sid]["name"], f"가온누리 연결 실패({e}) — 오프라인 모드", False
        return False, None, None, f"가온누리 연결 실패: {e}", False

    fail_kw = ["비밀번호", "아이디", "로그인 실패", "incorrect", "invalid"]
    if any(k in resp.text for k in fail_kw):
        if sid in STUDENTS:
            return True, sid, STUDENTS[sid]["name"], "가온누리 인증 불가 — 오프라인 모드", False
        return False, None, None, "아이디 또는 비밀번호가 틀렸습니다.", False

    name = STUDENTS.get(sid, {}).get("name", username)
    _sessions[sid] = sess
    return True, sid, name, None, True

# ═══════════════════════════════════════════════════════════════
# 가온누리 시간표 파싱
# tabler.php?stuId=25-096  (GET)
# ═══════════════════════════════════════════════════════════════

# 과목 패턴: 과목명(학년)_분반  또는  과목명(EC)(학년)_분반
_COURSE_RE = re.compile(
    r"([가-힣\w]+(?:\(EC\))?)\((\d+)\)_(\d+)"
)

def _parse_html(html: str) -> List[Dict]:
    """
    tabler.php HTML 파싱
    구조: <table> 교시×요일 그리드
    셀 텍스트: "과목명(학년)_분반 교원명"
    rowspan으로 연속 교시 표현
    """
    soup = BeautifulSoup(html, "html.parser")
    DAYS = ["월", "화", "수", "목", "금"]
    found: Dict[str, Dict] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # 헤더에서 요일→컬럼 인덱스
        day_col: Dict[str, int] = {}
        for ci, cell in enumerate(rows[0].find_all(["th", "td"])):
            txt = cell.get_text(strip=True)
            for d in DAYS:
                if d in txt:
                    day_col[d] = ci

        if not day_col:
            continue

        n_cols = max(day_col.values()) + 2  # 교시 열 + 요일 열
        # rowspan 처리를 위한 가상 그리드: (row_idx, col_idx) → (period, text)
        occupied: Dict[Tuple[int, int], Tuple[int, str]] = {}

        cur_period = None
        for ri, row in enumerate(rows[1:], start=1):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue

            # 첫 셀에서 교시 추출
            first = cells[0].get_text(strip=True)
            pm = re.match(r"^(\d+)", first)
            if pm:
                cur_period = int(pm.group(1))
            if cur_period is None:
                continue

            # occupied 를 피해가며 실제 셀→컬럼 매핑
            col_ptr = 0
            cell_ptr = 0
            while cell_ptr < len(cells):
                # 이미 rowspan으로 점유된 칸 건너뜀
                while (ri, col_ptr) in occupied:
                    col_ptr += 1

                if col_ptr >= n_cols + 5:
                    break

                cell = cells[cell_ptr]
                try:
                    rs = int(cell.get("rowspan", 1))
                    cs = int(cell.get("colspan", 1))
                except (ValueError, TypeError):
                    rs, cs = 1, 1

                txt = cell.get_text(" ", strip=True)

                # rowspan/colspan 범위 점유 표시
                for r_off in range(rs):
                    for c_off in range(cs):
                        occupied[(ri + r_off, col_ptr + c_off)] = (cur_period, txt)

                # 과목 추출
                day = None
                for d, dc in day_col.items():
                    if dc == col_ptr:
                        day = d
                        break

                if day:
                    for mo in _COURSE_RE.finditer(txt):
                        cname   = mo.group(1).strip()
                        grade   = mo.group(2)
                        section = mo.group(3)
                        ck      = f"{cname}({grade})"
                        key     = f"{ck}_{section}"

                        # 교원명: 패턴 이후 첫 단어
                        after   = txt[mo.end():].strip()
                        teacher = re.match(r"^([가-힣A-Za-z]+)", after)
                        teacher = teacher.group(1) if teacher else ""

                        if key not in found:
                            found[key] = {
                                "course":  cname,
                                "grade":   grade,
                                "section": section,
                                "ck":      ck,
                                "teacher": teacher,
                                "slots":   [],
                            }
                        sl = {"d": day, "p": cur_period}
                        if sl not in found[key]["slots"]:
                            found[key]["slots"].append(sl)

                col_ptr  += cs
                cell_ptr += 1

    return list(found.values())


def fetch_timetable(sid: str) -> Tuple[List[Dict], str, str]:
    """
    가온누리 tabler.php?stuId=<sid> 로 시간표 조회
    반환: (enrolled, warning, source)
    """
    enrolled: List[Dict] = []
    warn = ""
    source = "excel"

    sess = _sessions.get(sid)
    if sess:
        try:
            url  = f"{KSAIN_TABLER}?stuId={sid}"
            resp = sess.get(url, timeout=8)
            if resp.status_code == 200:
                items = _parse_html(resp.text)
                if items:
                    enrolled = items
                    source   = "ksain"
                else:
                    warn = "가온누리 파싱 결과 없음 — 엑셀 데이터 사용"
            else:
                warn = f"가온누리 응답 오류({resp.status_code}) — 엑셀 데이터 사용"
        except Exception as e:
            warn = f"가온누리 오류({e}) — 엑셀 데이터 사용"

    # 엑셀 폴백
    if not enrolled:
        s = STUDENTS.get(sid, {})
        for e in s.get("enrolled", []):
            ck = f"{e['course']}({e['grade']})"
            enrolled.append({
                "course":  e["course"],
                "grade":   e["grade"],
                "section": e["section"],
                "ck":      ck,
                "teacher": e.get("teacher", ""),
                "slots":   e.get("slots", []),
            })
        if not sess:
            warn = "가온누리 미연결 — 엑셀 데이터로 표시합니다."

    # 승인된 추가과목 병합
    for ex in _approved.get(sid, []):
        if not any(e["ck"] == ex["ck"] and e["section"] == ex["section"]
                   for e in enrolled):
            enrolled.append(ex)

    return enrolled, warn, source

# ═══════════════════════════════════════════════════════════════
# 트레이드 알고리즘
# ═══════════════════════════════════════════════════════════════

@dataclass
class _Cand:
    sid: str; sname: str; ck: str
    fr: str; to: str; pri: int


def _conflict(a: List, b: List) -> bool:
    return bool({(s["d"], s["p"]) for s in a} & {(s["d"], s["p"]) for s in b})


def solve(reqs: List[Dict], enrolled_map: Dict[str, List]) -> Tuple[List, List]:
    cands = [
        _Cand(r["sid"], r["sname"], r["course_key"],
              r["from_section"], to, i)
        for r in reqs
        for i, to in enumerate(r["to_sections"])
    ]

    def gk(c): return (c.sid, c.ck)
    groups = [
        sorted(list(g), key=lambda x: x.pri)
        for _, g in groupby(sorted(cands, key=gk), key=gk)
    ]

    state = {
        f"{ck}|{sec}": cnt
        for ck, ci in COURSES.items()
        for sec, cnt in ci["sections"].items()
    }

    best: List = []
    bs = [-1]

    def sc(ch): return sum(3 - c.pri for c in ch)

    def apply(c, st):
        kf, kt = f"{c.ck}|{c.fr}", f"{c.ck}|{c.to}"
        if st.get(kf, 0) <= 0: return False
        st[kf] -= 1; st[kt] = st.get(kt, 0) + 1
        return True

    def revert(c, st):
        st[f"{c.ck}|{c.fr}"] += 1; st[f"{c.ck}|{c.to}"] -= 1

    def balanced(st):
        return all(st.get(f"{ck}|{sec}", 0) == cnt
                   for ck, ci in COURSES.items()
                   for sec, cnt in ci["sections"].items())

    def ok(c):
        to_sl = COURSES.get(c.ck, {}).get("slots", {}).get(c.to, [])
        if not to_sl: return True
        other = [sl for e in enrolled_map.get(c.sid, [])
                 if e.get("ck", "") != c.ck
                 for sl in e.get("slots", [])]
        return not _conflict(to_sl, other)

    def bt(idx, ch, st):
        if sc(ch) + (len(groups) - idx) * 3 <= bs[0]: return
        if idx == len(groups):
            if balanced(st):
                s = sc(ch)
                if s > bs[0]: bs[0] = s; best.clear(); best.extend(ch)
            return
        for c in groups[idx]:
            if apply(c, st):
                if ok(c): ch.append(c); bt(idx + 1, ch, st); ch.pop()
                revert(c, st)
        bt(idx + 1, ch, st)

    bt(0, [], state)

    from collections import defaultdict
    by_ck: Dict = defaultdict(list)
    for c in best: by_ck[c.ck].append(c)
    cycles = []
    for ck, ts in by_ck.items():
        g = {t.fr: t for t in ts}; vis: set = set()
        for start in list(g.keys()):
            if start in vis: continue
            path, cur, seen = [], start, set()
            while cur in g and cur not in seen:
                seen.add(cur); t = g[cur]
                path.append({"sid": t.sid, "name": t.sname,
                              "from": t.fr, "to": t.to, "pri": t.pri})
                cur = t.to
            if cur == start and len(path) >= 2:
                for p in path: vis.add(p["from"])
                cycles.append({"course": ck, "members": path})
    return best, cycles


def build_result(reqs, chosen, cycles):
    cm = {(c.sid, c.ck): c for c in chosen}
    per: Dict = {}
    for r in reqs:
        sid = r["sid"]
        if sid not in per:
            per[sid] = {"sid": sid, "name": r["sname"],
                        "successes": [], "failures": []}
        c = cm.get((sid, r["course_key"]))
        if c:
            to_slots = COURSES.get(r["course_key"], {}).get("slots", {}).get(c.to, [])
            per[sid]["successes"].append({
                "course_key": r["course_key"], "from": r["from_section"],
                "to": c.to, "priority": c.pri,
                "to_list": r["to_sections"], "to_slots": to_slots,
            })
        else:
            per[sid]["failures"].append({
                "course_key": r["course_key"], "from": r["from_section"],
                "to_list": r["to_sections"],
            })
    return {"total_success": len(chosen), "total_requests": len(reqs),
            "students": list(per.values()), "cycles": cycles}

# ═══════════════════════════════════════════════════════════════
# 테스트 케이스
# ═══════════════════════════════════════════════════════════════

_E06 = [
    {"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]},
    {"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},
    {"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},
    {"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},
    {"course":"철학","grade":"2","section":"2","ck":"철학(2)","slots":[{"d":"월","p":6},{"d":"월","p":7}]},
    {"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]},
]
_E96 = [
    {"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]},
    {"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},
    {"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},
    {"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},
    {"course":"철학","grade":"2","section":"3","ck":"철학(2)","slots":[{"d":"금","p":3},{"d":"금","p":4}]},
    {"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]},
]

TEST_CASES = [
    {"id":"tc1","title":"같은 분반끼리 교환 시도","description":"두 학생 모두 영어Ⅲ 4분반 → 교환 불가","tag":"edge","expected":"성사 0건",
     "requests":[{"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]},
                 {"sid":"25-027","sname":"김준아","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]}],
     "enrolled_by_sid":{"25-006":_E06,"25-027":_E96}},
    {"id":"tc2","title":"✅ 2인 직접 교환 — 체육3","description":"구정준(5→9) ↔ 임서연(9→5)","tag":"success","expected":"성사 2건",
     "requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},
                 {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","3"]}],
     "enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc3","title":"✅ 3인 순환 교환 — 체육3","description":"A(1→2), B(2→3), C(3→1) 순환","tag":"success","expected":"성사 3건",
     "requests":[{"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["2"]},
                 {"sid":"26-001","sname":"강재원","course_key":"체육3(2)","from_section":"2","to_sections":["3"]},
                 {"sid":"26-006","sname":"김도윤","course_key":"체육3(2)","from_section":"3","to_sections":["1"]}],
     "enrolled_by_sid":{
         "26-008":[{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}],
         "26-001":[{"course":"체육3","grade":"2","section":"2","ck":"체육3(2)","slots":[{"d":"월","p":8}]}],
         "26-006":[{"course":"체육3","grade":"2","section":"3","ck":"체육3(2)","slots":[{"d":"화","p":4}]}]}},
    {"id":"tc4","title":"🚫 시간표 충돌","description":"체육3 3분반(화4교시) → 영어Ⅲ 4분반(화4교시)와 충돌","tag":"conflict","expected":"성사 0건",
     "requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["3"]},
                 {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["3"]}],
     "enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc5","title":"◐ 복수 과목 부분 성사","description":"체육3 교환 성공 / 영어Ⅲ 상대 없어 실패","tag":"partial","expected":"성사 2건 / 실패 1건",
     "requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9"]},
                 {"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5"]},
                 {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5"]}],
     "enrolled_by_sid":{"25-006":_E06,"25-096":_E96}},
    {"id":"tc6","title":"🎯 1지망 우선 최적 배정","description":"3명 체육3, 지망 가중치 최대화","tag":"priority","expected":"성사 3건",
     "requests":[{"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},
                 {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","1"]},
                 {"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["9","5"]}],
     "enrolled_by_sid":{
         "25-006":[{"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]}],
         "25-096":[{"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]}],
         "26-008":[{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}]}},
]

# ═══════════════════════════════════════════════════════════════
# Flask 라우트
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index(): return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "학번과 비밀번호를 입력하세요."}), 400

    # 관리자
    if password == ADMIN_PW:
        sid  = _norm(username)
        name = STUDENTS.get(sid, {}).get("name", "관리자")
        return jsonify({"ok": True, "sid": sid, "name": name,
                        "ksain_live": False, "admin": True})

    ok, sid, name, warn, live = ksain_login(username, password)
    if not ok:
        return jsonify({"ok": False, "error": warn}), 401
    return jsonify({"ok": True, "sid": sid, "name": name,
                    "ksain_live": live, "admin": False, "warning": warn})


@app.route("/api/timetable/<sid>")
def api_timetable(sid):
    enrolled, warn, source = fetch_timetable(sid)
    return jsonify({"enrolled": enrolled, "warning": warn, "source": source})


@app.route("/api/courses")
def api_courses():
    return jsonify({k: v for k, v in COURSES.items() if len(v["sections"]) >= 2})


@app.route("/api/trade", methods=["POST"])
def api_trade():
    data        = request.get_json(force=True)
    reqs        = data.get("requests", [])
    enr_list    = data.get("enrolled", [])
    if not reqs:
        return jsonify({"error": "신청 없음"}), 400
    try:
        enrolled_map: Dict[str, List] = {}
        for e in enr_list:
            sid = e.get("_sid") or (reqs[0]["sid"] if reqs else "")
            enrolled_map.setdefault(sid, []).append(e)
        chosen, cycles = solve(reqs, enrolled_map)
        return jsonify({"ok": True, "result": build_result(reqs, chosen, cycles)})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── 과목 추가신청 ────────────────────────────────────────────

@app.route("/api/course-request", methods=["POST"])
def api_course_request():
    d = request.get_json(force=True)
    sid     = d.get("sid", "").strip()
    course  = d.get("course", "").strip()
    grade   = d.get("grade", "?").strip()
    section = d.get("section", "").strip()
    teacher = d.get("teacher", "").strip()
    slots   = d.get("slots", [])
    if not all([sid, course, section, slots]):
        return jsonify({"ok": False, "error": "필수 항목 누락"}), 400
    ck = f"{course}({grade})"
    _pending.setdefault(sid, []).append({
        "course": course, "grade": grade, "section": section,
        "teacher": teacher, "ck": ck, "slots": slots, "status": "pending",
    })
    return jsonify({"ok": True, "message": "신청 완료. 관리자 승인 후 즉시 반영됩니다."})


@app.route("/api/admin/pending-requests")
def api_pending():
    result = []
    for sid, items in _pending.items():
        name = STUDENTS.get(sid, {}).get("name", "?")
        for i, item in enumerate(items):
            result.append({"sid": sid, "name": name, "idx": i, **item})
    return jsonify(result)


@app.route("/api/admin/approve-request", methods=["POST"])
def api_approve():
    d   = request.get_json(force=True)
    sid = d.get("sid", "")
    idx = d.get("idx", -1)
    lst = _pending.get(sid, [])
    if idx < 0 or idx >= len(lst):
        return jsonify({"ok": False, "error": "항목 없음"}), 404
    entry = dict(lst.pop(idx))
    entry.pop("status", None)
    _approved.setdefault(sid, []).append(entry)
    return jsonify({"ok": True,
                    "message": f"{STUDENTS.get(sid,{}).get('name',sid)}의 {entry['course']} 승인 완료"})


@app.route("/api/admin/reject-request", methods=["POST"])
def api_reject():
    d   = request.get_json(force=True)
    sid = d.get("sid", "")
    idx = d.get("idx", -1)
    lst = _pending.get(sid, [])
    if idx < 0 or idx >= len(lst):
        return jsonify({"ok": False, "error": "항목 없음"}), 404
    lst.pop(idx)
    return jsonify({"ok": True})


# ── 관리자 테스트 ────────────────────────────────────────────

@app.route("/api/admin/test-cases")
def api_test_cases():
    return jsonify([{"id": t["id"], "title": t["title"],
                     "description": t["description"], "tag": t["tag"],
                     "expected": t["expected"]} for t in TEST_CASES])


@app.route("/api/admin/run-test/<tc_id>", methods=["POST"])
def api_run_test(tc_id):
    tc = next((t for t in TEST_CASES if t["id"] == tc_id), None)
    if not tc: return jsonify({"error": "없음"}), 404
    try:
        chosen, cycles = solve(tc["requests"], tc["enrolled_by_sid"])
        return jsonify({"ok": True,
                        "result": build_result(tc["requests"], chosen, cycles),
                        "test_case": {"id": tc["id"], "title": tc["title"],
                                      "expected": tc["expected"], "tag": tc["tag"]}})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/admin/run-all-tests", methods=["POST"])
def api_run_all():
    results = []
    for tc in TEST_CASES:
        try:
            chosen, cycles = solve(tc["requests"], tc["enrolled_by_sid"])
            results.append({"id": tc["id"], "title": tc["title"], "tag": tc["tag"],
                             "expected": tc["expected"],
                             "result": build_result(tc["requests"], chosen, cycles),
                             "error": None})
        except Exception as e:
            results.append({"id": tc["id"], "title": tc["title"], "tag": tc["tag"],
                             "expected": tc["expected"], "result": None, "error": str(e)})
    return jsonify({"ok": True, "tests": results})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "students": len(STUDENTS), "courses": len(COURSES)})


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
