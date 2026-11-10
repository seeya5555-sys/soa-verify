#!/usr/bin/env python3
# SOA stage 2 check (per vessel). CLI: python3 -m soa_verify stage2 --vessel ABCD [--save]
#   python3 soa_stage2.py <VSL_CD>            (DRY: 판정만, 0 write)
#   VERIFY=1 python3 soa_stage2.py <VSL_CD>   (걸린 라인 RJT_YN=Y + 리마크 기입)
#   env: FM_DM/TO_DM(yyyyMM), SL_TP(05 Technical 기본 / 04 Crew), CAP(write 가드)
# 체크 항목
#   A. Operational AOR: Tech(05)/Crew(04) SOA 라인의 AOR Req No(REF_NO)를 SVMS 비용청구 AOR 원장(SP_GET_AOR)에서
#      조회해 DEPT_CD=O(Operational)이면 리젝 체크 + "Operational AOR" 리마크. (Operational 비용은 SOA-Operational로 가야 함)
# 🔒 스코프: 라인 리젝 체크·리마크까지만. Approval(STATUS=C)·출금 Slip·리젝 제출(STATUS=R)·관리사 메일 경로 없음.
# 🔒 fail-closed: AOR 조회 실패/미발견 = 쓰기 없이 보고만(사람 확인), 종료코드 1.
import os, re, sys
from svms import login, select, save

VERIFY = os.environ.get("VERIFY") == "1"
SL_TP = os.environ.get("SL_TP", "05")
FM_DM = os.environ.get("FM_DM", "202601")
TO_DM = os.environ.get("TO_DM", "202612")
try:
    CAP = int(os.environ.get("CAP", "300"))
except (TypeError, ValueError):
    CAP = 300

AOR_NO = re.compile(r"^[A-Z]{4}CA\d{10}$")      # 예: SVMLCA2609010001 (선박코드+CA+yyMMdd+seq4)
OPR_REMARK_PREFIX = "Operational AOR"
DEPT_LABEL = {"05": "Technical", "04": "Crew"}


def is_aor_ref(ref_no):
    return bool(AOR_NO.match(str(ref_no or "").strip().upper()))


def operational_remark(aor_cd, sl_tp):
    dept = DEPT_LABEL.get(sl_tp, "Tech/Crew")
    return (f"{OPR_REMARK_PREFIX}: {aor_cd} (Dept: Operational) - not chargeable to {dept} SOA. "
            f"Pls move to Operational SOA.")[:240]


def judge_line(line, aor_lookup, sl_tp):
    """라인 1개 판정(순수). aor_lookup(aor_cd)->row|None, 예외는 호출자 전파 안 함.
    returns (verdict, aor_cd, note): verdict ∈ skip|pass|reject|already|resub|error"""
    ref = str(line.get("REF_NO") or "").strip().upper()
    if not is_aor_ref(ref):
        return "skip", None, "AOR 아님"
    try:
        row = aor_lookup(ref)
    except BaseException as e:   # svms가 SystemExit 낼 수 있음
        return "error", ref, f"AOR 조회 실패: {str(e)[:60]}"
    if not row:
        return "error", ref, "AOR 원장 미발견"
    dept = str(row.get("DEPT_CD") or "").strip().upper()
    if dept != "O":
        return "pass", ref, f"Dept={dept or '?'}"
    if line.get("RJT_YN") == "Y" and str(line.get("RJT_RMK") or "").startswith(OPR_REMARK_PREFIX):
        return "already", ref, "이미 Operational AOR 리젝"
    if line.get("STATUS2") == "Y" or (line.get("STATUS_RMK2") or "").strip():
        return "resub", ref, "관리사 재상신/S·M 리마크 있음(보존, 사람 확인)"
    if line.get("RJT_YN") == "Y" or (line.get("RJT_RMK") or "").strip():
        return "already", ref, "기존 리젝/리마크 있음(보존, 사람 확인)"
    return "reject", ref, "Dept=Operational"


SNAP_KEYS = ("CFM_YN", "RJT_YN", "RJT_RMK", "STATUS2", "STATUS_RMK2", "REF_NO", "SOA_AMT")


def _snap(l):
    return tuple(str(l.get(k) or "") for k in SNAP_KEYS)


def _fetch_line(vsl, sx, seq, need_submitted=True):
    """헤더 STATUS=S(옵션) 재확인 + 라인 재조회. 실패/불일치 = None."""
    try:
        if need_submitted:
            hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                         {"VSL_CD": vsl, "FM_DM": FM_DM, "TO_DM": TO_DM, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
            h = next((x for x in (hdr.get("P_RS") or []) if x.get("SX_CD") == sx), None)
            if not h or h.get("STATUS") != "S":
                return None
        ls = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []
        return next((x for x in ls if str(x.get("SX_SEQ")) == str(seq)), None)
    except BaseException:
        return None


def reject_line(sx, seq, rmk):
    return save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS",
                {"SX_CD": sx, "SX_SEQ": seq, "CFM_YN": "N", "RJT_YN": "Y",
                 "RJT_RMK": rmk[:240], "STATUS2": "N", "STATUS_RMK2": None})


def run(vsl):
    if SL_TP not in DEPT_LABEL:
        print(f"🚨 SL_TP={SL_TP} 불가(04/05만)"); return 2
    login()
    cache = {}

    def aor_lookup(aor_cd):
        if aor_cd not in cache:
            rs = select("PKG_CO_OPEX", "SP_GET_AOR", {"AOR_CD": aor_cd}).get("P_RS", []) or []
            cache[aor_cd] = next((r for r in rs if r.get("AOR_CD") == aor_cd), None)
        return cache[aor_cd]

    print(f"=== SOA 2차 검증 [{vsl}] {'VERIFY(리젝체크+리마크 실기입)' if VERIFY else 'DRY(0 write)'} ===")
    print(f"   SL_TP={SL_TP}({DEPT_LABEL[SL_TP]}) · SL_DM {FM_DM}~{TO_DM} · STATUS=S · 항목: A.Operational AOR\n")
    hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                 {"VSL_CD": vsl, "FM_DM": FM_DM, "TO_DM": TO_DM, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
    rows = [r for r in (hdr.get("P_RS", []) or [])
            if (r.get("SX_CD") or "")[:4] == vsl and r.get("SL_TP") == SL_TP
            and r.get("STATUS") == "S" and FM_DM <= (r.get("SL_DM") or "") <= TO_DM]
    if not rows:
        print(f"검토대상 없음 (SL_TP={SL_TP} Submitted {FM_DM}~{TO_DM})."); return 0

    n = {"aor": 0, "pass": 0, "reject": 0, "already": 0, "resub": 0, "error": 0, "written": 0, "wfail": 0}
    todo = []   # (sx, seq, aor, snapshot)
    # ① preflight: 전 라인 판정(쓰기 0)
    for r in sorted(rows, key=lambda x: x.get("SL_DM") or ""):
        sx = r.get("SX_CD")
        lines = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []
        print(f"── {sx} ({r.get('SL_DM')}) | 라인 {len(lines)} ──")
        for l in lines:
            verdict, aor, note = judge_line(l, aor_lookup, SL_TP)
            if verdict == "skip":
                continue
            n["aor"] += 1; n[verdict] += 1
            if verdict == "pass":
                continue
            tag = {"reject": "🟥리젝대상", "already": "↩기리젝", "resub": "↩재상신", "error": "🚨확인필요"}[verdict]
            print(f"    s{l.get('SX_SEQ'):<4} [{l.get('SOA_CUR_CD')}{l.get('SOA_AMT')}] {aor} → {tag} ({note})")
            if verdict == "reject":
                todo.append((sx, l.get("SX_SEQ"), aor, _snap(l)))
    print()
    # ② write: preflight 오류가 1건이라도 있으면 전체 쓰기 0 (fail-closed)
    if VERIFY and n["error"]:
        print(f"🚨 확인필요 {n['error']}건 — 전체 기입 중단(0 write). 원인 해소 후 재실행.")
    elif VERIFY and todo:
        for sx, seq, aor, snap in todo:
            if n["written"] + n["wfail"] >= CAP:
                print(f"    ⏸ CAP({CAP}) 도달 — 기입 중단"); break
            # TOCTOU 가드: 기입 직전 헤더 STATUS=S·라인 상태 불변 재확인
            cur = _fetch_line(vsl, sx, seq)
            if cur is None or _snap(cur) != snap:
                print(f"    ↩ {sx} s{seq} 상태 변경됨(헤더≠S 또는 라인 수정) — 스킵"); continue
            rmk = operational_remark(aor, SL_TP)
            try:
                reject_line(sx, seq, rmk)
            except BaseException as e:
                print(f"    🚨 {sx} s{seq} 기입 예외: {str(e)[:80]} — 이후 기입 중단"); n["wfail"] += 1; break
            after = _fetch_line(vsl, sx, seq, need_submitted=False)   # 응답 대신 재조회로 확정
            if after and after.get("RJT_YN") == "Y" and (after.get("RJT_RMK") or "") == rmk:
                n["written"] += 1; print(f"    ✍ {sx} s{seq} 리젝체크+리마크 기입 확인")
            else:
                n["wfail"] += 1; print(f"    🚨 {sx} s{seq} 기입 미확인(재조회 불일치) — 이후 기입 중단"); break

    print(f"=== 합계: AOR 라인 {n['aor']} | 정상 {n['pass']} | Operational 리젝대상 {n['reject']} "
          f"| 기리젝 {n['already']} | 재상신 {n['resub']} | 확인필요 {n['error']} ===")
    if VERIFY:
        print(f"    실기입: 리젝체크 {n['written']} · 실패 {n['wfail']}")
    else:
        print("    (DRY — 0 write. VERIFY=1로 실기입)")
    print("    ⚠️ Approval·출금 Slip·리젝 제출·관리사 메일 = 미실행")
    return 1 if (n["error"] or n["wfail"]) else 0

if __name__ == "__main__":
    if len(sys.argv) < 2 or not re.match(r"^[A-Z]{4}$", sys.argv[1].strip().upper()):
        print("사용: [VERIFY=1] python3 soa_stage2.py <VSL_CD>"); sys.exit(2)
    sys.exit(run(sys.argv[1].strip().upper()) or 0)
