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
