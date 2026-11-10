#!/usr/bin/env python3
# SOA stage 2 check (per vessel). CLI: python3 -m soa_verify stage2 --vessel ABCD [--save]
#   python3 soa_stage2.py <VSL_CD>            (DRY: 판정만, 0 write)
#   VERIFY=1 python3 soa_stage2.py <VSL_CD>   (걸린 라인 RJT_YN=Y + 리마크 기입)
#   env: FM_DM/TO_DM(yyyyMM), SL_TP(05 Technical 기본 / 04 Crew), CAP(write 가드)
# 체크 항목
#   A. Operational AOR: Tech(05)/Crew(04) SOA 라인의 AOR Req No(REF_NO)를 SVMS 비용청구 AOR 원장(SP_GET_AOR)에서
#      조회해 DEPT_CD=O(Operational)이면 리젝 체크 + "Operational AOR" 리마크. (Operational 비용은 SOA-Operational로 가야 함)
#   B. 외화 환율 과청구: Local Currency(SOA_CUR_CD)≠USD 라인. 기준 USD = Local Amount × 기준환율(SVMS 환율표 교차,
#      없으면 하나은행 당일 미화환산율). 청구 USD(AMT_USD) ≥ 기준 × 1.20이면 리젝 + "FX over-charged" 리마크. 낮은 건 무관.
# 🔒 스코프: 라인 리젝 체크·리마크까지만. Approval(STATUS=C)·출금 Slip·리젝 제출(STATUS=R)·관리사 메일 경로 없음.
# 🔒 fail-closed: AOR·환율 조회 실패/미발견 1건이라도 있으면 전체 기입 0(보고만), 종료코드 1.
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


FX_OVER = 1.20          # 청구 USD ≥ 기준 USD × 1.20 → 리젝(20% 이상 높을 때만, 낮은 건 무관)
FX_REMARK_PREFIX = "FX over-charged"


def fx_remark(cur, amt, usd, rate, src):
    ref = amt * rate
    return (f"{FX_REMARK_PREFIX}: {cur} {amt:,.2f} -> USD {usd:,.2f} (ref rate {rate:.4f} {src}, "
            f"ref USD {ref:,.2f}, +{(usd / ref - 1) * 100:.1f}%)")


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def judge_line(line, aor_lookup, sl_tp, fx_lookup=None):
    """라인 1개 판정(순수). aor_lookup(aor_cd)->row|None / fx_lookup(cur)->(usd_per_unit, src)|None.
    returns (verdict, key, note, remark): verdict ∈ skip|pass|reject|already|resub|error"""
    findings, errors, checked = [], [], []
    ref = str(line.get("REF_NO") or "").strip().upper()
    # A. Operational AOR
    if is_aor_ref(ref):
        checked.append(ref)
        try:
            row = aor_lookup(ref)
            if not row:
                errors.append(f"{ref} AOR 원장 미발견")
            elif str(row.get("DEPT_CD") or "").strip().upper() == "O":
                findings.append(("A", operational_remark(ref, sl_tp)))
        except BaseException as e:   # svms가 SystemExit 낼 수 있음
            errors.append(f"{ref} AOR 조회 실패: {str(e)[:60]}")
    # B. 외화 환율 과청구 (Local Currency≠USD, 양수 금액만)
    cur = str(line.get("SOA_CUR_CD") or "").strip().upper()
    amt, usd = _num(line.get("SOA_AMT")), _num(line.get("AMT_USD"))
    if fx_lookup and cur and cur != "USD" and amt and usd and amt > 0 and usd > 0:
        checked.append(cur)
        try:
            fx = fx_lookup(cur)
        except BaseException as e:
            fx = None; errors.append(f"{cur} 환율 조회 실패: {str(e)[:60]}")
        if fx is None:
            if not errors or not errors[-1].startswith(cur):
                errors.append(f"{cur} 기준환율 없음(SVMS·은행 모두)")
        else:
            rate, src = fx
            from decimal import Decimal as D
            if D(str(usd)) >= D(str(amt)) * D(repr(rate)) * D("1.20"):   # 정확한 ≥120% (float 경계오차 배제)
                findings.append(("B", fx_remark(cur, amt, usd, rate, src)))
    key = " ".join(checked) or None
    if not checked:
        return "skip", None, "대상 아님", None
    if errors:
        return "error", key, "; ".join(errors), None
    if not findings:
        return "pass", key, "정상", None
    tags = "+".join(t for t, _ in findings)
    rmk = " | ".join(r for _, r in findings)[:240]
    prev = str(line.get("RJT_RMK") or "")
    if line.get("RJT_YN") == "Y" and (prev.startswith(OPR_REMARK_PREFIX) or prev.startswith(FX_REMARK_PREFIX)):
        return "already", key, "이미 2차 리젝", None
    if line.get("STATUS2") == "Y" or (line.get("STATUS_RMK2") or "").strip():
        return "resub", key, f"[{tags}] 관리사 재상신/S·M 리마크 있음(보존, 사람 확인)", None
    if line.get("RJT_YN") == "Y" or prev.strip():
        return "already", key, f"[{tags}] 기존 리젝/리마크 있음(보존, 사람 확인)", None
    return "reject", key, tags, rmk


SNAP_KEYS = ("CFM_YN", "RJT_YN", "RJT_RMK", "STATUS2", "STATUS_RMK2", "REF_NO", "SOA_AMT", "SOA_CUR_CD", "AMT_USD")


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


FX_COMP_ID = "001"   # 장금 환율표(원화 기준, 통화 대부분 보유). 실버(037) 표는 SGD 외 비어 있음(11-10 실측)
HANA_URL = "https://www.kebhana.com/cms/rate/wpfxd651_01i_01.do"


def _today():
    import datetime
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime("%Y%m%d")


def svms_usd_rate(cur, dt):
    """SVMS 환율표: 1 cur = ? USD (= EX_RATE/EX_UNIT ÷ EX_RATE_USD, 원화 교차). 없거나 0이면 None."""
    r = (select("PKG_CO", "SP_GET_EX_RATE", {"COMP_ID": FX_COMP_ID, "CUR_CD": cur, "CUR_DT": dt}).get("P_RS") or [{}])[0]
    if str(r.get("CUR_CD") or "").upper() != cur:
        return None
    ex, unit, usd = _num(r.get("EX_RATE")), _num(r.get("EX_UNIT")), _num(r.get("EX_RATE_USD"))
    if not (ex and unit and usd) or ex <= 0 or unit <= 0 or usd <= 0:
        return None
    return ex / unit / usd


def parse_hana(html, expect_dt=None):
    """하나은행 고시환율표 → {CUR: 미화환산율(1 cur = ? USD)}. 행: '국가명 CUR [(100)] … 매매기준율 환가료율 미화환산율'."""
    import html as H
    txt = H.unescape(re.sub(r"<[^>]+>", " ", html))
    if expect_dt:   # 당일성: 응답 '기준일 : YYYY년MM월DD일'이 요청일과 같아야 함(다르면 빈 결과 = 조회실패 처리)
        m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", txt)
        if not m or f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}" != expect_dt:
            return {}
    base = {}
    for m in re.finditer(r"(?<![A-Za-z])([A-Z]{3})(?:\s*\((\d+)\))?((?:\s+[\d,]+\.\d+){6,12})", txt):
        nums = [float(x.replace(",", "")) for x in m.group(3).split()]
        unit = int(m.group(2)) if m.group(2) else 1
        if nums[-3] > 0:
            base[m.group(1)] = nums[-3] / unit          # 매매기준율(원) per 1 unit
    usd = base.get("USD")
    if not usd:   # 교차 기준(USD 매매기준율) 없으면 전체 무효 → 조회실패(반올림 미화환산율로 대체하지 않음)
        return {}
    return {c: b / usd for c, b in base.items()}


def hana_rates(dt):
    import urllib.request, urllib.parse
    d = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    body = urllib.parse.urlencode({"ajax": "true", "curCd": "", "tmpInqStrDt": d, "pbldDvCd": "3", "pbldSqn": "",
                                   "hid_key_data": "", "inqStrDt": dt, "inqKindCd": "1", "hid_enc_data": "",
                                   "requestTarget": "searchContentDiv"}).encode()
    req = urllib.request.Request(HANA_URL, data=body, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=20).read()
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return parse_hana(raw.decode(enc), dt)
        except UnicodeDecodeError:
            continue
    return parse_hana(raw.decode("utf-8", "replace"), dt)


def make_fx_lookup(dt=None):
    """cur -> (usd_per_unit, src) | None. SVMS 우선, 없으면 하나은행 당일 미화환산율(1회만 조회)."""
    dt = dt or _today(); cache = {}; hana = {}

    def lookup(cur):
        if cur not in cache:
            rate = svms_usd_rate(cur, dt); src = "SVMS"
            if rate is None:
                if "_loaded" not in hana:
                    try:
                        hana.update(hana_rates(dt))
                    except Exception as e:
                        print(f"    ⚠️ 하나은행 환율 조회 실패: {str(e)[:60]}")
                    hana["_loaded"] = 1
                rate = hana.get(cur); src = "Hana"
            cache[cur] = (rate, src) if rate else None
        return cache[cur]
    return lookup


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

    fx_lookup = make_fx_lookup()

    print(f"=== SOA 2차 검증 [{vsl}] {'VERIFY(리젝체크+리마크 실기입)' if VERIFY else 'DRY(0 write)'} ===")
    print(f"   SL_TP={SL_TP}({DEPT_LABEL[SL_TP]}) · SL_DM {FM_DM}~{TO_DM} · STATUS=S · 항목: A.Operational AOR · B.외화 환율 과청구(≥120%)\n")
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
            verdict, aor, note, rmk = judge_line(l, aor_lookup, SL_TP, fx_lookup)
            if verdict == "skip":
                continue
            n["aor"] += 1; n[verdict] += 1
            if verdict == "pass":
                continue
            tag = {"reject": "🟥리젝대상", "already": "↩기리젝", "resub": "↩재상신", "error": "🚨확인필요"}[verdict]
            print(f"    s{l.get('SX_SEQ'):<4} [{l.get('SOA_CUR_CD')}{l.get('SOA_AMT')}] {aor} → {tag} ({note})")
            if verdict == "reject":
                todo.append((sx, l.get("SX_SEQ"), rmk, _snap(l)))
    print()
    # ② write: preflight 오류가 1건이라도 있으면 전체 쓰기 0 (fail-closed)
    if VERIFY and n["error"]:
        print(f"🚨 확인필요 {n['error']}건 — 전체 기입 중단(0 write). 원인 해소 후 재실행.")
    elif VERIFY and todo:
        for sx, seq, rmk, snap in todo:
            if n["written"] + n["wfail"] >= CAP:
                print(f"    ⏸ CAP({CAP}) 도달 — 기입 중단"); break
            # TOCTOU 가드: 기입 직전 헤더 STATUS=S·라인 상태 불변 재확인
            cur = _fetch_line(vsl, sx, seq)
            if cur is None or _snap(cur) != snap:
                print(f"    ↩ {sx} s{seq} 상태 변경됨(헤더≠S 또는 라인 수정) — 스킵"); continue
            try:
                reject_line(sx, seq, rmk)
            except BaseException as e:
                print(f"    🚨 {sx} s{seq} 기입 예외: {str(e)[:80]} — 이후 기입 중단"); n["wfail"] += 1; break
            after = _fetch_line(vsl, sx, seq, need_submitted=False)   # 응답 대신 재조회로 확정
            if after and after.get("RJT_YN") == "Y" and (after.get("RJT_RMK") or "") == rmk:
                n["written"] += 1; print(f"    ✍ {sx} s{seq} 리젝체크+리마크 기입 확인")
            else:
                n["wfail"] += 1; print(f"    🚨 {sx} s{seq} 기입 미확인(재조회 불일치) — 이후 기입 중단"); break

    print(f"=== 합계: 검사 라인 {n['aor']} | 정상 {n['pass']} | 리젝대상 {n['reject']} "
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
