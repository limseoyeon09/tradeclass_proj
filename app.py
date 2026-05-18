"""
TradeTable — KSA 시간표 트레이드 시스템
로컬 실행 / Render 배포 모두 지원
"""
import json, re, os
from flask import Flask, request, jsonify, render_template
from dataclasses import dataclass
from itertools import groupby
from typing import Dict, List, Tuple

# ── 선택 패키지 ──────────────────────────────────────────────
try:
    import requests as _req
    from bs4 import BeautifulSoup
    HAS_NET = True
except ImportError:
    HAS_NET = False

# ── 앱 초기화 ────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

KSAIN_LOGIN  = "https://ksain.net/pages/cert/login.php"
KSAIN_TABLER = "https://ksain.net/pages/tabler/tabler.php"
KSAIN_APIKEY = "f5rtxw-89mzkp-c2qbvd-l7shnj-a4p1re"
ADMIN_PW     = os.environ.get("ADMIN_PW", "tradetable-admin-2026")

# ── 데이터 로드 ──────────────────────────────────────────────
_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "static", "data.json"), encoding="utf-8") as f:
    _DB = json.load(f)

STUDENTS  = _DB["students"]   # {sid: {name, enrolled:[{course,grade,section,key,slots}]}}
COURSES   = _DB["courses"]    # {ck: {sections:{sec:cnt}, slots:{sec:[{d,p,t}]}}}

# ── 가온누리 세션 저장소 ─────────────────────────────────────
_sessions: Dict[str, object] = {}   # sid → requests.Session

# ═══════════════════════════════════════════════════════════════
# 가온누리 연동
# ═══════════════════════════════════════════════════════════════

def _normalize_sid(raw: str) -> str:
    m = re.match(r"^(\d{2})(\d{3})$", raw.strip())
    return f"{m.group(1)}-{m.group(2)}" if m else raw.strip()


def ksain_login(username: str, password: str):
    """
    반환: (ok:bool, sid:str, name:str, error:str|None, ksain_live:bool)
    """
    sid = _normalize_sid(username)

    if not HAS_NET:
        # beautifulsoup4 / requests 없으면 엑셀 폴백
        if sid in STUDENTS:
            return True, sid, STUDENTS[sid]["name"], None, False
        return False, None, None, "등록되지 않은 학번입니다.", False

    sess = _req.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0"
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

    fail_kw = ["비밀번호", "아이디", "로그인 실패", "incorrect", "invalid", "fail"]
    if any(k in resp.text for k in fail_kw):
        return False, None, None, "아이디 또는 비밀번호가 틀렸습니다.", False

    name = STUDENTS.get(sid, {}).get("name", username)
    _sessions[sid] = sess
    return True, sid, name, None, True


def parse_tabler_html(html: str) -> List[Dict]:
    """tabler.php HTML → [{course, grade, section, ck, slots:[{d,p}]}]"""
    soup = BeautifulSoup(html, "html.parser")
    DAYS = ["월", "화", "수", "목", "금"]
    found: Dict[str, Dict] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue

        # 헤더에서 요일→컬럼 인덱스 파악
        day_col: Dict[str, int] = {}
        for ci, cell in enumerate(rows[0].find_all(["th", "td"])):
            txt = cell.get_text(strip=True)
            for d in DAYS:
                if d in txt:
                    day_col[d] = ci

        if not day_col:
            continue

        cur_period = None
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            m = re.match(r"^(\d+)", cells[0].get_text(strip=True))
            if m:
                cur_period = int(m.group(1))
            if cur_period is None:
                continue

            for day, ci in day_col.items():
                if ci >= len(cells):
                    continue
                txt = cells[ci].get_text(" ", strip=True)
                for raw_key in re.findall(r"[가-힣\w\(\)Ⅰ-Ⅹ\.]+\(\d+\)_\d+", txt):
                    pm = re.match(r"^(.+)\((\d+)\)_(\d+)$", raw_key)
                    if not pm:
                        continue
                    cname, grade, section = pm.group(1).strip(), pm.group(2), pm.group(3)
                    ck = f"{cname}({grade})"
                    if raw_key not in found:
                        found[raw_key] = {
                            "course": cname, "grade": grade,
                            "section": section, "ck": ck, "slots": [],
                        }
                    sl = {"d": day, "p": cur_period}
                    if sl not in found[raw_key]["slots"]:
                        found[raw_key]["slots"].append(sl)

    return list(found.values())


def get_timetable(sid: str) -> Tuple[List[Dict], str]:
    """가온누리 우선, 실패 시 엑셀 폴백"""
    sess = _sessions.get(sid)
    if sess:
        try:
            resp = sess.get(KSAIN_TABLER, timeout=8)
            items = parse_tabler_html(resp.text)
            if items:
                return items, ""
        except Exception:
            pass

    # 엑셀 폴백
    s = STUDENTS.get(sid, {})
    result = []
    for e in s.get("enrolled", []):
        ck = f"{e['course']}({e['grade']})"
        result.append({
            "course": e["course"], "grade": e["grade"],
            "section": e["section"], "ck": ck,
            "slots": e.get("slots", []),
        })
    warn = "" if sess else "가온누리 미연결 — 엑셀 데이터로 표시합니다."
    return result, warn


# ═══════════════════════════════════════════════════════════════
# 트레이드 알고리즘
# ═══════════════════════════════════════════════════════════════

@dataclass
class _Cand:
    sid: str; sname: str; ck: str
    fr: str; to: str; pri: int


def _slots_conflict(a: List, b: List) -> bool:
    sa = {(s["d"], s["p"]) for s in a}
    return bool(sa & {(s["d"], s["p"]) for s in b})


def solve(reqs: List[Dict], enrolled_map: Dict[str, List]) -> Tuple[List, List]:
    """
    reqs: [{sid, sname, course_key, from_section, to_sections}]
    enrolled_map: {sid → enrolled_list}
    반환: (chosen_list, cycles_list)
    """
    cands = [
        _Cand(r["sid"], r["sname"], r["course_key"], r["from_section"], to, i)
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
    best_score = [-1]

    def score(ch): return sum(3 - c.pri for c in ch)

    def apply(c, st):
        kf, kt = f"{c.ck}|{c.fr}", f"{c.ck}|{c.to}"
        if st.get(kf, 0) <= 0:
            return False
        st[kf] -= 1
        st[kt] = st.get(kt, 0) + 1
        return True

    def revert(c, st):
        st[f"{c.ck}|{c.fr}"] += 1
        st[f"{c.ck}|{c.to}"] -= 1

    def balanced(st):
        return all(
            st.get(f"{ck}|{sec}", 0) == cnt
            for ck, ci in COURSES.items()
            for sec, cnt in ci["sections"].items()
        )

    def no_slot_conflict(c):
        to_slots = COURSES.get(c.ck, {}).get("slots", {}).get(c.to, [])
        if not to_slots:
            return True
        other = [
            sl for e in enrolled_map.get(c.sid, [])
            if e.get("ck", f"{e.get('course')}({e.get('grade')})") != c.ck
            for sl in e.get("slots", [])
        ]
        return not _slots_conflict(to_slots, other)

    def bt(idx, ch, st):
        if score(ch) + (len(groups) - idx) * 3 <= best_score[0]:
            return
        if idx == len(groups):
            if balanced(st):
                s = score(ch)
                if s > best_score[0]:
                    best_score[0] = s
                    best.clear()
                    best.extend(ch)
            return
        for c in groups[idx]:
            if apply(c, st):
                if no_slot_conflict(c):
                    ch.append(c)
                    bt(idx + 1, ch, st)
                    ch.pop()
                revert(c, st)
        bt(idx + 1, ch, st)

    bt(0, [], state)

    # 순환 감지
    from collections import defaultdict
    by_ck: Dict[str, List] = defaultdict(list)
    for c in best:
        by_ck[c.ck].append(c)

    cycles = []
    for ck, ts in by_ck.items():
        g = {t.fr: t for t in ts}
        vis: set = set()
        for start in list(g.keys()):
            if start in vis:
                continue
            path, cur, seen = [], start, set()
            while cur in g and cur not in seen:
                seen.add(cur)
                t = g[cur]
                path.append({"sid": t.sid, "name": t.sname,
                              "from": t.fr, "to": t.to, "pri": t.pri})
                cur = t.to
            if cur == start and len(path) >= 2:
                for p in path:
                    vis.add(p["from"])
                cycles.append({"course": ck, "members": path})

    return best, cycles


def build_result(reqs, chosen, cycles):
    cm = {(c.sid, c.ck): c for c in chosen}
    per: Dict[str, Dict] = {}
    for r in reqs:
        sid = r["sid"]
        if sid not in per:
            per[sid] = {"sid": sid, "name": r["sname"],
                        "successes": [], "failures": []}
        c = cm.get((sid, r["course_key"]))
        if c:
            to_slots = COURSES.get(r["course_key"], {}).get("slots", {}).get(c.to, [])
            per[sid]["successes"].append({
                "course_key": r["course_key"],
                "from": r["from_section"], "to": c.to,
                "priority": c.pri, "to_list": r["to_sections"],
                "to_slots": to_slots,
            })
        else:
            per[sid]["failures"].append({
                "course_key": r["course_key"],
                "from": r["from_section"], "to_list": r["to_sections"],
            })
    return {
        "total_success": len(chosen),
        "total_requests": len(reqs),
        "students": list(per.values()),
        "cycles": cycles,
    }


# ═══════════════════════════════════════════════════════════════
# 관리자 테스트 케이스 (실제 2026-1 데이터 기반)
# ═══════════════════════════════════════════════════════════════

# 공통 enrolled 데이터
_E25006 = [
    {"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]},
    {"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},
    {"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},
    {"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},
    {"course":"철학","grade":"2","section":"2","ck":"철학(2)","slots":[{"d":"월","p":6},{"d":"월","p":7}]},
    {"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]},
]
_E25096 = [
    {"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]},
    {"course":"영어Ⅲ","grade":"2","section":"4","ck":"영어Ⅲ(2)","slots":[{"d":"화","p":3},{"d":"화","p":4},{"d":"목","p":5}]},
    {"course":"문학","grade":"2","section":"3","ck":"문학(2)","slots":[{"d":"화","p":5},{"d":"화","p":6}]},
    {"course":"정치와경제","grade":"2","section":"3","ck":"정치와경제(2)","slots":[{"d":"수","p":2},{"d":"수","p":3}]},
    {"course":"철학","grade":"2","section":"3","ck":"철학(2)","slots":[{"d":"금","p":3},{"d":"금","p":4}]},
    {"course":"자료구조","grade":"2","section":"2","ck":"자료구조(2)","slots":[{"d":"화","p":1},{"d":"수","p":6},{"d":"목","p":6}]},
]

TEST_CASES = [
    {
        "id": "tc1",
        "title": "같은 분반끼리 교환 시도",
        "description": "두 학생 모두 영어Ⅲ 4분반 수강 중 → 서로 교환 불가",
        "tag": "edge",
        "expected": "성사 0건",
        "requests": [
            {"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]},
            {"sid":"25-027","sname":"김준아","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5","6"]},
        ],
        "enrolled_by_sid": {"25-006": _E25006, "25-027": _E25096},
    },
    {
        "id": "tc2",
        "title": "✅ 2인 직접 교환 — 체육3",
        "description": "구정준(5분반→9분반 희망) ↔ 임서연(9분반→5분반 희망). 인원 균형 맞음",
        "tag": "success",
        "expected": "성사 2건 (2인 순환)",
        "requests": [
            {"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},
            {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","3"]},
        ],
        "enrolled_by_sid": {"25-006": _E25006, "25-096": _E25096},
    },
    {
        "id": "tc3",
        "title": "✅ 3인 순환 교환 — 체육3",
        "description": "A(1→2분반), B(2→3분반), C(3→1분반). 직접 교환 쌍 없어도 순환으로 모두 성사",
        "tag": "success",
        "expected": "성사 3건 (1→2→3→1 순환)",
        "requests": [
            {"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["2"]},
            {"sid":"26-001","sname":"강재원","course_key":"체육3(2)","from_section":"2","to_sections":["3"]},
            {"sid":"26-006","sname":"김도윤","course_key":"체육3(2)","from_section":"3","to_sections":["1"]},
        ],
        "enrolled_by_sid": {
            "26-008": [{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}],
            "26-001": [{"course":"체육3","grade":"2","section":"2","ck":"체육3(2)","slots":[{"d":"월","p":8}]}],
            "26-006": [{"course":"체육3","grade":"2","section":"3","ck":"체육3(2)","slots":[{"d":"화","p":4}]}],
        },
    },
    {
        "id": "tc4",
        "title": "🚫 시간표 충돌로 이동 불가",
        "description": "체육3 5분반(화8교시)→3분반(화4교시) 희망. 그런데 영어Ⅲ 4분반이 이미 화4교시 → 충돌",
        "tag": "conflict",
        "expected": "성사 0건 (시간표 충돌)",
        "requests": [
            {"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["3"]},
            {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["3"]},
        ],
        "enrolled_by_sid": {"25-006": _E25006, "25-096": _E25096},
    },
    {
        "id": "tc5",
        "title": "◐ 복수 과목 신청 — 부분 성사",
        "description": "구정준: 체육3(5→9) + 영어Ⅲ(4→5) 신청. 임서연: 체육3(9→5). 체육3 교환 성사, 영어Ⅲ 상대 없어 실패",
        "tag": "partial",
        "expected": "성사 2건 / 실패 1건",
        "requests": [
            {"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9"]},
            {"sid":"25-006","sname":"구정준","course_key":"영어Ⅲ(2)","from_section":"4","to_sections":["5"]},
            {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5"]},
        ],
        "enrolled_by_sid": {"25-006": _E25006, "25-096": _E25096},
    },
    {
        "id": "tc6",
        "title": "🎯 1지망 우선 최적 배정",
        "description": "3명이 체육3에서 각자 원하는 분반 신청. 가능한 한 1지망 배정 우선",
        "tag": "priority",
        "expected": "성사 3건 (지망 가중치 최대화)",
        "requests": [
            {"sid":"25-006","sname":"구정준","course_key":"체육3(2)","from_section":"5","to_sections":["9","1"]},
            {"sid":"25-096","sname":"임서연","course_key":"체육3(2)","from_section":"9","to_sections":["5","1"]},
            {"sid":"26-008","sname":"김도진","course_key":"체육3(2)","from_section":"1","to_sections":["9","5"]},
        ],
        "enrolled_by_sid": {
            "25-006": [{"course":"체육3","grade":"2","section":"5","ck":"체육3(2)","slots":[{"d":"화","p":8}]}],
            "25-096": [{"course":"체육3","grade":"2","section":"9","ck":"체육3(2)","slots":[{"d":"금","p":7}]}],
            "26-008": [{"course":"체육3","grade":"2","section":"1","ck":"체육3(2)","slots":[{"d":"월","p":7}]}],
        },
    },
]


# ═══════════════════════════════════════════════════════════════
# Flask 라우트
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()

    if not username or not password:
        return jsonify({"ok": False, "error": "학번과 비밀번호를 입력하세요."}), 400

    # 관리자
    if password == ADMIN_PW:
        sid = _normalize_sid(username)
        name = STUDENTS.get(sid, {}).get("name", "관리자")
        return jsonify({"ok": True, "sid": sid, "name": name,
                        "ksain_live": False, "admin": True})

    ok, sid, name, err, live = ksain_login(username, password)
    if not ok:
        return jsonify({"ok": False, "error": err}), 401

    warn = err  # err가 None이 아니면 오프라인 경고
    return jsonify({"ok": True, "sid": sid, "name": name,
                    "ksain_live": live, "admin": False,
                    "warning": warn})


@app.route("/api/timetable/<sid>")
def api_timetable(sid):
    enrolled, warn = get_timetable(sid)
    return jsonify({"enrolled": enrolled, "warning": warn})


@app.route("/api/courses")
def api_courses():
    return jsonify({k: v for k, v in COURSES.items() if len(v["sections"]) >= 2})


@app.route("/api/trade", methods=["POST"])
def api_trade():
    data = request.get_json(force=True)
    reqs = data.get("requests", [])
    enrolled_list = data.get("enrolled", [])
    if not reqs:
        return jsonify({"error": "신청 없음"}), 400
    try:
        # enrolled_map: sid → list
        enrolled_map: Dict[str, List] = {}
        for e in enrolled_list:
            sid = e.get("_sid") or (reqs[0]["sid"] if reqs else "")
            enrolled_map.setdefault(sid, []).append(e)
        chosen, cycles = solve(reqs, enrolled_map)
        return jsonify({"ok": True, "result": build_result(reqs, chosen, cycles)})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ── 관리자 API ───────────────────────────────────────────────

@app.route("/api/admin/test-cases")
def api_test_cases():
    return jsonify([
        {"id": t["id"], "title": t["title"], "description": t["description"],
         "tag": t["tag"], "expected": t["expected"]}
        for t in TEST_CASES
    ])


@app.route("/api/admin/run-test/<tc_id>", methods=["POST"])
def api_run_test(tc_id):
    tc = next((t for t in TEST_CASES if t["id"] == tc_id), None)
    if not tc:
        return jsonify({"error": "없는 테스트"}), 404
    try:
        chosen, cycles = solve(tc["requests"], tc["enrolled_by_sid"])
        result = build_result(tc["requests"], chosen, cycles)
        return jsonify({"ok": True, "result": result,
                        "test_case": {"id": tc["id"], "title": tc["title"],
                                      "expected": tc["expected"], "tag": tc["tag"]}})
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/admin/run-all-tests", methods=["POST"])
def api_run_all_tests():
    results = []
    for tc in TEST_CASES:
        try:
            chosen, cycles = solve(tc["requests"], tc["enrolled_by_sid"])
            result = build_result(tc["requests"], chosen, cycles)
            results.append({"id": tc["id"], "title": tc["title"],
                             "tag": tc["tag"], "expected": tc["expected"],
                             "result": result, "error": None})
        except Exception as e:
            results.append({"id": tc["id"], "title": tc["title"],
                             "tag": tc["tag"], "expected": tc["expected"],
                             "result": None, "error": str(e)})
    return jsonify({"ok": True, "tests": results})


# ── 헬스체크 (배포 플랫폼용) ────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "students": len(STUDENTS),
                    "courses": len(COURSES)})


# ── 실행 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
