



















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

AOR_NO = re.compile(r"^[A-Z]{4}CA\d{10}$")
OPR_REMARK_PREFIX = "Operational AOR"
DEPT_LABEL = {"05": "Technical", "04": "Crew"}


def is_aor_ref(ref_no):
    return bool(AOR_NO.match(str(ref_no or "").strip().upper()))


def operational_remark(aor_cd, sl_tp):
    dept = DEPT_LABEL.get(sl_tp, "Tech/Crew")
    return (f"{OPR_REMARK_PREFIX}: {aor_cd} (Dept: Operational) - not chargeable to {dept} SOA. "
            f"Pls move to Operational SOA.")[:240]


MF_EXP_CD = "080101"
MF_TOL = 1.0
MF_REMARK_PREFIX = "Mgmt fee over SMA"
NRC_REMARK = "NRC invoice"
NRC_PAT = re.compile(r"national\s+response\s+corp|nrc\s+opa\s*'?\s*90|opa\s*'?\s*90\s+service\s+invoice", re.I)
MF_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "soa_mgmt_fee.json")


def mf_remark(charged, exp_usd, desc):
    return f"{MF_REMARK_PREFIX}: charged USD {charged:,.2f} vs contract USD {exp_usd:,.2f} ({desc})"


def load_mgmt_fee(path=None):
    """{VESSEL NAME: {basis: daily|monthly, rate, manager}} — 없거나 깨지면 {} (C 비활성, 보고)."""
    import json
    try:
        with open(path or os.environ.get("SOA_MGMT_FEE_DB") or MF_DB) as f:
            return {k.strip().upper(): v for k, v in (json.load(f).get("vessels") or {}).items()}
    except (OSError, ValueError):
        return {}


def mf_expected(rec, sl_dm):
    """계약 월액(USD)과 설명. daily = 일단가 × 해당 SOA월(SL_DM) 일수."""
    import calendar
    if rec.get("basis") == "daily":
        days = calendar.monthrange(int(sl_dm[:4]), int(sl_dm[4:6]))[1]
        return rec["rate"] * days, f"{rec['rate']:g}/day x {days}d, {rec.get('manager', '')}".strip(", ")
    return rec["rate"], f"monthly {rec['rate']:,.2f}, {rec.get('manager', '')}".strip(", ")


def is_nrc_text(txt):
    return bool(NRC_PAT.search(txt or ""))


FX_OVER = 1.20
FX_REMARK_PREFIX = "FX over-charged"
OWN_PREFIXES = (OPR_REMARK_PREFIX, FX_REMARK_PREFIX, MF_REMARK_PREFIX, NRC_REMARK, "Duplicate invoice")


def fx_remark(cur, amt, usd, rate, src):
    ref = amt * rate
    return (f"{FX_REMARK_PREFIX}: {cur} {amt:,.2f} -> USD {usd:,.2f} (ref rate {rate:.4f} {src}, "
            f"ref USD {ref:,.2f}, +{(usd / ref - 1) * 100:.1f}%)")


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


REMARK_MAX = 240
_ORDER = {"D": 0, "E": 1, "A": 2, "C": 3, "B": 4}
_TAGNAME = {"D": "NRC invoice", "E": "Duplicate invoice", "A": "Operational AOR", "C": "Mgmt fee over SMA", "B": "FX over-charged"}


def _short(tag, full):
    """240자 초과 시 쓰는 축약형(핵심 숫자 유지)."""
    if tag == "A":
        m = re.search(r"[A-Z]{4}CA\d{10}", full); return f"Operational AOR {m.group(0) if m else ''}".strip()
    if tag == "B":
        m = re.search(r"(\w{3}) ([\d,.]+) -> USD ([\d,.]+).*?(\+[\d.]+%)", full)
        return f"FX over-charged {m.group(1)} {m.group(2)}->USD {m.group(3)} ({m.group(4)})" if m else "FX over-charged"
    if tag == "E":
        return full.replace("Duplicate invoice: ", "Duplicate invoice ")
    if tag == "C":
        m = re.search(r"charged USD ([\d,.]+) vs contract USD ([\d,.]+)", full)
        return f"Mgmt fee over SMA {m.group(1)} vs {m.group(2)}" if m else "Mgmt fee over SMA"
    return full


DUP_REMARK_PREFIX = "Duplicate invoice"
ENGINE_PREFIXES = ("Cost no match",)


def dup_remark(sx, seq, inv):
    if seq is None:
        return f"{DUP_REMARK_PREFIX}: {inv} = Fund Request {sx}"
    return f"{DUP_REMARK_PREFIX}: {inv} = {sx} s{seq}"


def _engine_remark(prev):
    return prev.startswith(ENGINE_PREFIXES) or prev.startswith(OWN_PREFIXES)


def append_remark(prev, new):
    """기존 엔진 리마크 + ' | ' + 새 사유. 240자 넘으면 축약형 → 사유명. 그래도 넘으면 None(사람 확인)."""
    fs = sorted(new, key=lambda x: _ORDER.get(x[0], 9))
    for parts in ([r for _, r in fs], [_short(t, r) for t, r in fs], [_TAGNAME[t] for t, _ in fs]):
        out = prev + " | " + " | ".join(parts)
        if len(out) <= REMARK_MAX:
            return out
    return None


def combine_remarks(findings):
    """리젝 사유 전부를 ' | '로 이어 한 리마크로(형 11-10: 사유 여러 개면 동시 표기). 240자 넘으면 축약형으로."""
    fs = sorted(findings, key=lambda x: _ORDER.get(x[0], 9))
    full = " | ".join(r for _, r in fs)
    if len(full) <= REMARK_MAX:
        return full
    short = " | ".join(_short(t, r) for t, r in fs)
    if len(short) <= REMARK_MAX:
        return short

    return " | ".join(_TAGNAME[t] for t, _ in fs)


def judge_line(line, aor_lookup, sl_tp, fx_lookup=None, mf=None, nrc=None, dup=None):
    """라인 1개 판정(순수). aor_lookup(aor_cd)->row|None / fx_lookup(cur)->(usd_per_unit, src)|None.
    returns (verdict, key, note, remark): verdict ∈ skip|pass|reject|already|resub|error"""
    findings, errors, checked = [], [], []
    ref = str(line.get("REF_NO") or "").strip().upper()

    if is_aor_ref(ref):
        checked.append(ref)
        try:
            row = aor_lookup(ref)
            if not row:
                errors.append(f"{ref} AOR 원장 미발견")
            elif str(row.get("DEPT_CD") or "").strip().upper() == "O":
                findings.append(("A", operational_remark(ref, sl_tp)))
        except BaseException as e:
            errors.append(f"{ref} AOR 조회 실패: {str(e)[:60]}")

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
            if D(str(usd)) >= D(str(amt)) * D(repr(rate)) * D("1.20"):
                findings.append(("B", fx_remark(cur, amt, usd, rate, src)))

    if mf is not None and str(line.get("EXP_CD") or "") == MF_EXP_CD:
        checked.append("MF")
        exp_usd, charged, desc = mf
        if charged > exp_usd + MF_TOL:
            findings.append(("C", mf_remark(charged, exp_usd, desc)))

    if nrc is not None:
        checked.append("NRC")
        if nrc:
            findings.append(("D", NRC_REMARK))

    if dup is not None:
        checked.append("DUP")
        if dup:
            findings.append(("E", dup_remark(*dup)))
    key = " ".join(checked) or None
    if not checked:
        return "skip", None, "대상 아님", None
    if errors:
        return "error", key, "; ".join(errors), None
    if not findings:
        return "pass", key, "정상", None
    tags = "+".join(t for t, _ in findings)
    rmk = combine_remarks(findings)
    prev = str(line.get("RJT_RMK") or "").strip()
    if line.get("STATUS2") == "Y" or (line.get("STATUS_RMK2") or "").strip():
        return "resub", key, f"[{tags}] 관리사 재상신/S·M 리마크 있음(보존, 사람 확인)", None
    if not prev and line.get("RJT_YN") != "Y":
        return "reject", key, tags, rmk
    if prev and _engine_remark(prev):

        new = [(t, r) for t, r in findings if _TAGNAME[t] not in prev]
        if not new:
            return "already", key, "이미 같은 사유 기입됨", None
        merged = append_remark(prev, new)
        if merged is None:
            return "already", key, f"[{tags}] 기존 리마크 + 새 사유가 240자 초과(보존, 사람 확인)", None
        return "reject", key, f"{tags} (기존 리마크에 덧붙임)", merged
    return "already", key, f"[{tags}] 사람/기타 리마크 있음(보존, 사람 확인)", None


SNAP_KEYS = ("CFM_YN", "RJT_YN", "RJT_RMK", "STATUS2", "STATUS_RMK2", "REF_NO", "SOA_AMT", "SOA_CUR_CD", "AMT_USD",
             "EXP_CD", "FILE_REF_NO", "FILE_NO", "SX_SEQ")


def _snap(l):
    return tuple(str(l.get(k) or "") for k in SNAP_KEYS)


def _soa_fp(hdr_row, lines):
    """SOA 전체 지문: SL_DM·STATUS + 모든 라인 스냅샷(정렬). C(라인 합계)·추가/삭제 라인 변화까지 감지."""
    return (str(hdr_row.get("SL_DM") or ""), str(hdr_row.get("STATUS") or ""),
            tuple(sorted(_snap(x) for x in lines)))


def _fp_excl(lines, seq):
    return tuple(sorted(_snap(x) for x in lines if str(x.get("SX_SEQ")) != str(seq)))


def _files_fp(sx, l):
    import soa_review as R
    fs = R.get_files(l.get("FILE_REF_NO") or (str(sx) + str(l.get("SX_SEQ"))))
    return tuple(sorted(str(f.get("SAVE_NM") or f.get("FILE_NM") or "") for f in fs))


def _soa_now(vsl, sx):
    """(헤더행, 라인들) 재조회. 실패 = None."""
    try:
        hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                     {"VSL_CD": vsl, "FM_DM": FM_DM, "TO_DM": TO_DM, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
        h = next((x for x in (hdr.get("P_RS") or []) if x.get("SX_CD") == sx), None)
        if not h:
            return None
        return h, select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []
    except BaseException:
        return None


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


FX_COMP_ID = "001"
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
    if expect_dt:
        m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", txt)
        if not m or f"{m.group(1)}{int(m.group(2)):02d}{int(m.group(3)):02d}" != expect_dt:
            return {}
    base = {}
    for m in re.finditer(r"(?<![A-Za-z])([A-Z]{3})(?:\s*\((\d+)\))?((?:\s+[\d,]+\.\d+){6,12})", txt):
        nums = [float(x.replace(",", "")) for x in m.group(3).split()]
        unit = int(m.group(2)) if m.group(2) else 1
        if nums[-3] > 0:
            base[m.group(1)] = nums[-3] / unit
    usd = base.get("USD")
    if not usd:
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


DUP_EXCLUDE_EXP = ("080101", "0601")
try:
    DUP_LOOKBACK_M = int(os.environ.get("SOA_DUP_LOOKBACK_MONTHS", "24"))
except (TypeError, ValueError):
    DUP_LOOKBACK_M = 24


def norm_inv(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _dup_excluded(exp_cd):
    extra = tuple(x.strip() for x in os.environ.get("SOA_DUP_EXCLUDE_EXP", "").split(",") if x.strip())
    return str(exp_cd or "").startswith(DUP_EXCLUDE_EXP + extra)


def _amt_key(v):
    from decimal import Decimal, InvalidOperation
    try:
        return format(Decimal(str(v).replace(",", "")).normalize(), "f")
    except (InvalidOperation, ValueError):
        return None


def dup_key(l):
    """(정규화 번호, 통화, 금액) — Debit·번호 있음·제외코드 아님·금액 있음(0 제외). 아니면 None."""
    inv = norm_inv(l.get("INV_NO"))
    amt = _amt_key(l.get("SOA_AMT")) if l.get("SOA_AMT") not in (None, "") else None
    if l.get("SOA_TP") != "D" or not inv or amt in (None, "0") or _dup_excluded(l.get("EXP_CD")):
        return None
    return (inv, str(l.get("SOA_CUR_CD") or "").upper(), amt)


def _ym_shift(ym, months):
    y, m = int(ym[:4]), int(ym[4:6]) - 1 + months
    return f"{y + m // 12:04d}{m % 12 + 1:02d}"


def build_dup_index(vsl, min_dm, max_dm):
    """선박의 SOA 라인 이력 {dup_key: [(sx, seq, inv, sl_dm)]} — Draft 제외, 대상 기간 전체 ±LOOKBACK개월(SOA별 필터는 find_dup)."""
    fm, to = _ym_shift(min_dm, -DUP_LOOKBACK_M), _ym_shift(max_dm, DUP_LOOKBACK_M)
    hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA", {"VSL_CD": vsl, "FM_DM": fm, "TO_DM": to,
                                                  "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"}).get("P_RS", []) or []
    idx = {}
    for h in hdr:
        sx = h.get("SX_CD") or ""
        if sx[:4] != vsl or h.get("STATUS") == "D":
            continue
        for l in select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []:
            k = dup_key(l)
            if k:
                idx.setdefault(k, []).append((sx, l.get("SX_SEQ"), str(l.get("INV_NO") or "").strip(), str(h.get("SL_DM") or "")))
    return idx


def build_fund_index(vsl, min_dm, max_dm):
    """선박 비용청구(Fund Request) {(번호, 통화, 금액): [row]} — Draft·Reject 제외, 대상 기간 ±LOOKBACK개월."""
    fm, to = _ym_shift(min_dm, -DUP_LOOKBACK_M), _ym_shift(max_dm, DUP_LOOKBACK_M)
    rows = select("PKG_CO_OPEX", "SP_GET_OPEX", {"FM_DT": fm + "01", "TO_DT": to + "31", "VSL_CD": vsl}).get("P_RS", []) or []
    idx = {}
    for r in rows:
        if str(r.get("STATUS") or "") in ("D", "R"):
            continue
        inv = norm_inv(r.get("INV_NO"))
        amt = _amt_key(r.get("AMT")) if r.get("AMT") not in (None, "") else None
        if inv and amt not in (None, "0"):
            idx.setdefault((inv, str(r.get("CUR_CD") or "").upper(), amt), []).append(r)
    return idx


def _ref(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def find_fund_dup(fidx, l, sl_dm=None):
    """SOA 라인이 같은 선박 비용청구로 이미 지급된 인보이스면 (OPEX_CD, None, inv). 아니면 False/None.
    정상 정산 예외: ①라인 REF_NO가 그 비용청구의 AOR(REF_NO/AOR_CD)과 같음 ②AOR 라인인데 REF_NO 미기재(구데이터·판단불가).
    ※ 'REF_NO에 비용청구 번호(CO)' 예외는 11-10 형 결정으로 삭제 — 예전 무작위 입력 관행, 현재 규칙상 AOR 번호만 유효."""
    k = dup_key(l)
    if k is None or not fidx:
        return None
    lref = _ref(l.get("REF_NO"))
    if not lref and l.get("SOA_OPEX_TP") == "A":
        return False
    for r in fidx.get(k, []):
        fdm = str(r.get("REQ_DT") or r.get("OPEX_DT") or r.get("INV_DT") or "")[:6]
        if sl_dm and (len(fdm) < 6 or _months_apart(fdm, sl_dm) > DUP_LOOKBACK_M):
            continue
        fa = _ref(r.get("REF_NO") or r.get("AOR_CD"))
        if lref and fa and lref == fa:
            continue
        return (str(r.get("OPEX_CD") or ""), None, str(r.get("INV_NO") or "").strip())
    return False


def _months_apart(a, b):
    try:
        return abs((int(a[:4]) * 12 + int(a[4:6])) - (int(b[:4]) * 12 + int(b[4:6])))
    except (TypeError, ValueError):
        return 10 ** 6


def find_dup(idx, sx, l, sl_dm=None):
    """이 라인보다 '먼저 만들어진' 다른 SOA에 같은 키가 있으면 그 원본 (sx, seq, inv). 없으면 False, 대상 아니면 None.
    SOA 번호(선박+CX+yyMMdd+seq)의 생성순서로 판정 → 나중 SOA만 리젝, 원본은 안 건드림."""
    k = dup_key(l)
    if k is None:
        return None
    older = sorted(x for x in idx.get(k, []) if x[0] != sx and x[0][4:] < sx[4:]
                   and (sl_dm is None or len(x) < 4 or _months_apart(x[3], sl_dm) <= DUP_LOOKBACK_M))
    return tuple(older[0][:3]) if older else False


NRC_SCAN = os.environ.get("SOA_STAGE2_NRC", "1") == "1"


def nrc_scan(sx, l, warns):
    try:
        return _nrc_scan(sx, l, warns)
    except BaseException as e:
        warns.append(f"{sx} s{l.get('SX_SEQ')} NRC 검사 예외 → D 미검사: {str(e)[:40]}")
        return None


def _nrc_scan(sx, l, warns):
    """라인 첨부 PDF 본문에 NRC 인보이스 표식이 있으면 True. 첨부 없음 → None(대상 아님).
    다운로드 실패·판독 불가는 warn(비차단: 오기입 위험이 아닌 미탐 위험이라 보고만)."""
    import soa_review as R
    import soa_amount as SA
    seq = l.get("SX_SEQ")
    try:
        files = [f for f in R.get_files(l.get("FILE_REF_NO") or (str(sx) + str(seq))) if R._is_pdf_file(f)]
    except BaseException as e:
        warns.append(f"{sx} s{seq} 첨부 조회 실패 → D 미검사: {str(e)[:40]}"); return None
    if not files:
        return None
    paths = R.dl(sx, seq, files)
    if len(paths) < len(files):
        warns.append(f"{sx} s{seq} 첨부 {len(files) - len(paths)}건 다운로드 실패 → D 일부 미검사")
    unread = 0
    for p in paths:
        cached = SA.cached_texts(p)
        if any(is_nrc_text(t) for t in cached.values()):
            return True
        txt = cached.get("text")
        if txt is None:
            txt = SA.pdf_body(p)
        if is_nrc_text(txt):
            return True
        if len(txt.strip()) < 50 and not any(len(t.strip()) >= 50 for t in cached.values()):
            txt = SA.ocr_body(p, maxpages=1)
            if is_nrc_text(txt):
                return True
            if len(txt.strip()) < 50:
                unread += 1
    if unread:
        warns.append(f"{sx} s{seq} 첨부 {unread}건 판독 불가 → D 미검사")
    return False if paths else None


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
    vm = (select("PKG_CM_CD", "SP_GET_VSL", {"VSL_CD": vsl}).get("P_RS") or [{}])[0]
    vname = str(vm.get("NAME") or "").strip().upper()
    mf_db = load_mgmt_fee()
    mf_rec = mf_db.get(vname)
    warns = []
    if not mf_db:
        warns.append("관리비 계약표 없음 → C 미검사")
    elif not mf_rec:
        warns.append(f"관리비 계약표에 '{vname or vsl}' 없음 → C 미검사")

    print(f"=== SOA 2차 검증 [{vsl}] {'VERIFY(리젝체크+리마크 실기입)' if VERIFY else 'DRY(0 write)'} ===")
    print(f"   SL_TP={SL_TP}({DEPT_LABEL[SL_TP]}) · SL_DM {FM_DM}~{TO_DM} · STATUS=S · 항목: A.Operational AOR · B.외화 환율(≥120%) · C.관리비 SMA · D.NRC · E.중복 인보이스\n")
    hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                 {"VSL_CD": vsl, "FM_DM": FM_DM, "TO_DM": TO_DM, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
    rows = [r for r in (hdr.get("P_RS", []) or [])
            if (r.get("SX_CD") or "")[:4] == vsl and r.get("SL_TP") == SL_TP
            and r.get("STATUS") == "S" and FM_DM <= (r.get("SL_DM") or "") <= TO_DM]
    if not rows:
        print(f"검토대상 없음 (SL_TP={SL_TP} Submitted {FM_DM}~{TO_DM})."); return 0

    n = {"aor": 0, "pass": 0, "reject": 0, "already": 0, "resub": 0, "error": 0, "written": 0, "wfail": 0}
    todo = []
    dup_idx = None
    fund_idx = {}

    for r in sorted(rows, key=lambda x: x.get("SL_DM") or ""):
        sx = r.get("SX_CD")
        lines = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []
        print(f"── {sx} ({r.get('SL_DM')}) | 라인 {len(lines)} ──")
        soa_fp = _soa_fp(r, lines)
        mf = None
        fee_lines = [l for l in lines if str(l.get("EXP_CD") or "") == MF_EXP_CD]
        if mf_rec and fee_lines:
            if any(str(l.get("SOA_CUR_CD") or "").upper() not in ("USD", "") for l in fee_lines):
                warns.append(f"{sx} 관리비 비USD 청구 → C 미검사(사람 확인)")
            else:
                exp_usd, desc = mf_expected(mf_rec, r.get("SL_DM") or "")
                charged = sum(_num(l.get("AMT_USD")) or 0 for l in fee_lines)
                mf = (exp_usd, charged, desc)
                print(f"    관리비: 청구 USD {charged:,.2f} / 계약 USD {exp_usd:,.2f} ({desc})")
        if dup_idx is None:
            dms = sorted(str(x.get("SL_DM") or FM_DM) for x in rows)
            try:
                dup_idx = build_dup_index(vsl, dms[0], dms[-1])
            except BaseException as e:
                dup_idx = {}; warns.append(f"중복 인보이스 SOA 이력 조회 실패 → E(SOA) 미검사: {str(e)[:40]}")
            try:
                fund_idx = build_fund_index(vsl, dms[0], dms[-1])
            except BaseException as e:
                fund_idx = {}; warns.append(f"비용청구 조회 실패 → E(비용청구) 미검사: {str(e)[:40]}")
        for l in lines:
            nrc = nrc_scan(sx, l, warns) if NRC_SCAN else None
            dup = find_dup(dup_idx, sx, l, str(r.get("SL_DM") or "")) if dup_idx else None
            if not dup:
                fdup = find_fund_dup(fund_idx, l, str(r.get("SL_DM") or ""))
                dup = fdup if fdup else (dup if dup is not None else fdup)
            verdict, aor, note, rmk = judge_line(l, aor_lookup, SL_TP, fx_lookup, mf, nrc, dup)
            if verdict == "skip":
                continue
            n["aor"] += 1; n[verdict] += 1
            if verdict == "pass":
                continue
            tag = {"reject": "🟥리젝대상", "already": "↩기리젝", "resub": "↩재상신", "error": "🚨확인필요"}[verdict]
            print(f"    s{l.get('SX_SEQ'):<4} [{l.get('SOA_CUR_CD')}{l.get('SOA_AMT')}] {aor} → {tag} ({note})")
            if verdict == "reject":
                ffp = None
                if rmk == NRC_REMARK:
                    try:
                        ffp = _files_fp(sx, l)
                    except BaseException:
                        ffp = "ERR"
                todo.append((sx, l.get("SX_SEQ"), rmk, soa_fp, ffp, l))
    print()

    if VERIFY and n["error"]:
        print(f"🚨 확인필요 {n['error']}건 — 전체 기입 중단(0 write). 원인 해소 후 재실행.")
    elif VERIFY and todo:
        expect = {}
        for sx, seq, rmk, soa_fp, ffp, l0 in todo:
            if n["written"] + n["wfail"] >= CAP:
                print(f"    ⏸ CAP({CAP}) 도달 — 기입 중단"); break

            now = _soa_now(vsl, sx)
            stale = (now is None or now[0].get("STATUS") != "S" or _soa_fp(now[0], now[1]) != expect.get(sx, soa_fp))
            if not stale and ffp is not None:
                try:
                    stale = (ffp == "ERR" or _files_fp(sx, l0) != ffp)
                except BaseException:
                    stale = True
            if stale:
                print(f"    ↩ {sx} s{seq} 상태 변경됨(헤더≠S·라인/첨부 수정) — 스킵"); continue
            try:
                reject_line(sx, seq, rmk)
            except BaseException as e:
                print(f"    🚨 {sx} s{seq} 기입 예외: {str(e)[:80]} — 이후 기입 중단"); n["wfail"] += 1; break
            post = _soa_now(vsl, sx)
            after = next((x for x in (post[1] if post else []) if str(x.get("SX_SEQ")) == str(seq)), None)
            pre = next((x for x in now[1] if str(x.get("SX_SEQ")) == str(seq)), None)
            want = dict(pre or {}, CFM_YN="N", RJT_YN="Y", RJT_RMK=rmk, STATUS2="N", STATUS_RMK2=None)
            others_same = bool(post) and after is not None and _snap(after) == _snap(want) and \
                _fp_excl(post[1], seq) == _fp_excl(now[1], seq) and \
                _soa_fp(post[0], [])[:2] == _soa_fp(now[0], [])[:2]
            if after and after.get("RJT_YN") == "Y" and (after.get("RJT_RMK") or "") == rmk and others_same:
                expect[sx] = _soa_fp(post[0], post[1])
                n["written"] += 1; print(f"    ✍ {sx} s{seq} 리젝체크+리마크 기입 확인")
            elif after and after.get("RJT_YN") == "Y" and (after.get("RJT_RMK") or "") == rmk:
                n["written"] += 1; expect[sx] = ("CHANGED",)
                print(f"    ✍ {sx} s{seq} 기입 확인 · ⚠️ 동시 변경 감지 — 이 SOA 나머지 기입 스킵")
            else:
                n["wfail"] += 1; print(f"    🚨 {sx} s{seq} 기입 미확인(재조회 불일치) — 이후 기입 중단"); break

    print(f"=== 합계: 검사 라인 {n['aor']} | 정상 {n['pass']} | 리젝대상 {n['reject']} "
          f"| 기리젝 {n['already']} | 재상신 {n['resub']} | 확인필요 {n['error']} ===")
    if VERIFY:
        print(f"    실기입: 리젝체크 {n['written']} · 실패 {n['wfail']}")
    else:
        print("    (DRY — 0 write. VERIFY=1로 실기입)")
    for w in warns:
        print(f"    ⚠️ {w}")
    print("    ⚠️ Approval·출금 Slip·리젝 제출·관리사 메일 = 미실행")
    return 1 if (n["error"] or n["wfail"]) else 0

if __name__ == "__main__":
    if len(sys.argv) < 2 or not re.match(r"^[A-Z]{4}$", sys.argv[1].strip().upper()):
        print("사용: [VERIFY=1] python3 soa_stage2.py <VSL_CD>"); sys.exit(2)
    sys.exit(run(sys.argv[1].strip().upper()) or 0)
