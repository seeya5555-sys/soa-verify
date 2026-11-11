



















import os, re, sys
from svms import login, select, save

VERIFY = os.environ.get("VERIFY") == "1"
SL_TP = os.environ.get("SL_TP", "05")
def default_period(today=None):
    """기본 조회 기간 (FM_DM, TO_DM) = 직전 해 1월 ~ 올해 12월 (1차 soa_review.soa_period와 같은 규칙)."""
    import datetime
    y = (today or datetime.date.today()).year
    return os.environ.get("SOA_FM_DM") or f"{y - 1}01", os.environ.get("SOA_TO_DM") or f"{y}12"


FM_DM = os.environ.get("FM_DM") or default_period()[0]
TO_DM = os.environ.get("TO_DM") or default_period()[1]
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
AOROPEX_REMARK_PREFIX = "AOR filed as OPEX"
AORINV_REMARK_PREFIX = "AOR invoice no"
OWN_PREFIXES = (OPR_REMARK_PREFIX, FX_REMARK_PREFIX, MF_REMARK_PREFIX, NRC_REMARK, "Duplicate invoice",
                AOROPEX_REMARK_PREFIX, AORINV_REMARK_PREFIX, "Cat-II should be Pre-delivery")







AORDOC_MODE = os.environ.get("SOA_STAGE2_AORDOC", "1")
AOR_MARK = re.compile(r"agreed?\s+over\s*-?\s*run\s+expense", re.I)
AOR_CODE_ANY = re.compile(r"\b([A-Z]{4}CA\d{10})\b")
COVER_REF = re.compile(r"\bRef(?:erence)?\s*(?:No\.?|#)?\s*[:.]\s*([A-Z0-9][A-Z0-9/\-.]{3,30})", re.I)
VENDOR_INV = re.compile(r"\bInvoice[ \t]*(?:#|No\.?|Number|Nr\.?)[ \t]*[:.#]?[ \t]*([A-Z0-9][A-Z0-9/\-. ]{2,30}?)(?=[ \t]{2,}|\n|$|[ \t]+(?:Date|Due|VAT|PO)\b)", re.I | re.M)


def _norm_no(v):
    return re.sub(r"[^A-Z0-9]", "", str(v or "").upper())


def aor_doc_check(line, texts, aor_lookup, vsl, vendor_inv_llm=None):
    """순수 판정. texts=첨부 본문들. returns (findings[(tag, remark)], is_aor, info)."""
    out, info = [], {}
    body = "\n".join(t for t in (texts or []) if t)
    tp = str(line.get("SOA_OPEX_TP") or "").upper()
    inv = str(line.get("INV_NO") or "").strip()
    is_aor = tp == "A"
    if tp == "O" and body.strip() and AOR_MARK.search(body):
        for code in dict.fromkeys(AOR_CODE_ANY.findall(body.upper())):
            if code[:4] != vsl:
                continue
            row = aor_lookup(code)
            if row:
                info["aor"] = code
                out.append(("F", f"{AOROPEX_REMARK_PREFIX}: AOR doc {code} attached - pls change Cat-II to AOR "
                                 f"and enter AOR Req No"))
                is_aor = True
                break
    if is_aor:

        cover_pages = [pg for pg in re.split(r"\f", body) if AOR_MARK.search(pg)]
        refs = {_norm_no(m) for pg in cover_pages for m in COVER_REF.findall(pg)}
        refs.discard("")
        bad_cover = bool(inv) and _norm_no(inv) in refs
        if bad_cover or not inv:
            vno = None
            if body.strip():

                vendor_body = "\f".join(pg for pg in re.split(r"\f", body) if not AOR_MARK.search(pg))
                vnos = [v.strip(" .") for v in VENDOR_INV.findall(vendor_body)]
                vnos = [v for v in vnos if _norm_no(v) and _norm_no(v) not in refs and not AOR_CODE_ANY.search(v.upper())]
                if not vnos and vendor_inv_llm:
                    v = vendor_inv_llm(body)
                    if _vendor_no_ok(v, body, refs):
                        vnos = [v]
                vno = vnos[0] if vnos else None
            info["vendor_inv"] = vno
            what = f"SOA {inv} = AOR cover page Ref" if bad_cover else "blank in SOA"
            tail = f"vendor invoice {vno}" if vno else "vendor invoice no. required"
            out.append(("G", f"{AORINV_REMARK_PREFIX}: {what} / {tail}"))
    return out, is_aor, info


def _vendor_no_ok(v, body, refs):
    """LLM 후보 검증: 커버 Ref 아님 + 본문에 번호 경계로 실재(부분 문자열 금지). 불확실=False(번호 제시 안 함)."""
    v = str(v or "").strip()
    if len(_norm_no(v)) < 3 or _norm_no(v) in refs or AOR_CODE_ANY.search(v.upper()):
        return False
    return bool(re.search(r"(?<![A-Z0-9])" + re.escape(v) + r"(?![A-Z0-9])", body, re.I))


def vendor_inv_llm(body):
    """정규식이 업체 인보이스 번호를 못 찾을 때만 쓰는 Haiku 보조. 실패=None(판정은 코드가 함)."""
    try:
        from llm_router import llm_json
        d = llm_json("아래 문서에서 '업체(vendor)가 발행한 인보이스 번호' 하나만 찾아라. 관리사 커버페이지/Debit note 의 "
                     "Ref 번호는 제외. 없으면 null. JSON 한 줄만: {\"vendor_invoice_no\": \"...\"|null}\n\n" + body[:4000],
                     timeout=120)
        v = d.get("vendor_invoice_no")
        return str(v).strip() if v else None
    except Exception:
        return None


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
_ORDER = {"D": 0, "E": 1, "F": 2, "G": 3, "H": 4, "A": 5, "C": 6, "B": 7}
_TAGNAME = {"D": "NRC invoice", "E": "Duplicate invoice", "A": "Operational AOR", "C": "Mgmt fee over SMA", "B": "FX over-charged",
            "F": AOROPEX_REMARK_PREFIX, "G": AORINV_REMARK_PREFIX, "H": "Cat-II should be Pre-delivery"}


def _short(tag, full):
    """240자 초과 시 쓰는 축약형(핵심 숫자 유지)."""
    if tag == "A":
        m = re.search(r"[A-Z]{4}CA\d{10}", full); return f"Operational AOR {m.group(0) if m else ''}".strip()
    if tag == "B":
        m = re.search(r"(\w{3}) ([\d,.]+) -> USD ([\d,.]+).*?(\+[\d.]+%)", full)
        return f"FX over-charged {m.group(1)} {m.group(2)}->USD {m.group(3)} ({m.group(4)})" if m else "FX over-charged"
    if tag == "E":
        return full.replace("Duplicate invoice: ", "Duplicate invoice ")
    if tag == "F":
        m = re.search(r"[A-Z]{4}CA\d{10}", full); return f"AOR filed as OPEX {m.group(0) if m else ''}".strip()
    if tag == "G":
        return full.replace("AOR cover page Ref", "cover Ref").replace("vendor invoice no. required", "vendor inv no req")
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


def judge_line(line, aor_lookup, sl_tp, fx_lookup=None, mf=None, nrc=None, dup=None, aordoc=None):
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

    if aordoc is not None:
        checked.append("AORDOC")
        findings.extend(aordoc)
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


def lines_fp(lines):
    """SOA 라인 전체 지문(sha256). 자동승인 게이트가 '2차 검증 때와 승인 직전의 라인이 같은지' 대조하는 데 씀."""
    import hashlib
    return hashlib.sha256(repr(tuple(sorted(_snap(x) for x in lines))).encode()).hexdigest()


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
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")


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


def line_texts(sx, l, warns):
    """라인 첨부 PDF 본문 목록(1차 캐시 재사용, 없으면 pdftotext 1회). 실패=None(F/G 미검사, warn)."""
    import soa_review as R
    import soa_amount as SA
    seq = l.get("SX_SEQ")
    try:
        files = [f for f in R.get_files(l.get("FILE_REF_NO") or (str(sx) + str(seq))) if R._is_pdf_file(f)]
        if not files:
            return []
        out = []
        for p in R.dl(sx, seq, files):
            cached = SA.cached_texts(p)
            out.extend(cached.values())
            if cached.get("text") is None:
                out.append(SA.pdf_body(p))
        return out
    except BaseException as e:
        warns.append(f"{sx} s{seq} 첨부 본문 확보 실패 → F/G 미검사: {str(e)[:40]}")
        return None






PREDEL_MODE = os.environ.get("SOA_STAGE2_PREDEL", "1")
PREDEL_TP = "P"


def needs_predel(line):
    """순수 판정: Debit 라인 EXP_CD 12xxxx 인데 Cat-II 가 O/A 면 True."""
    return (line.get("SOA_TP") == "D" and str(line.get("EXP_CD") or "").startswith("12")
            and str(line.get("SOA_OPEX_TP") or "").upper() in ("O", "A"))


PREDEL_REMARK_PREFIX = "Cat-II should be Pre-delivery"
try:
    from soa_predel_write import predel_line
except ImportError:
    predel_line = None


def predel_remark(line):
    return f"{PREDEL_REMARK_PREFIX}: EXP {line.get('EXP_CD')} is pre-delivery account - pls change Cat-II to Pre-delivery"


_PREDEL_VOLATILE = ("SOA_OPEX_TP", "COST_TP", "UPST_DI", "UPST_USER_ID", "UPST_NM")


def predel_ok(before, after):
    """기입 후 확인: 대상 라인은 Cat-II(P)만 바뀌고 나머지 필드 불변."""
    if not after or str(after.get("SOA_OPEX_TP") or "").upper() != PREDEL_TP \
            or str(after.get("COST_TP") or "").upper() != PREDEL_TP:
        return False
    keys = (set(before) | set(after)) - set(_PREDEL_VOLATILE)
    return all(before.get(k) == after.get(k) for k in keys)





SUMMARY_MODE = os.environ.get("SOA_STAGE2_SUMMARY", "1")
SUM_TAG = " | 적요: "
SUM_MARK = "적요:"
SUM_MIN = 15
SUM_STYLE_VER = "ko1:"


def _fits(t, limit=120, max_bytes=180):
    return len(t) <= limit and len(t.encode("utf-8")) <= max_bytes


def summary_remark(prev, summary, limit=120, max_bytes=180):
    """기존 리마크 + ' | 적요: 요약'. 이미 적요 요약 있음/공간 부족/요약 없음 = None."""
    prev = str(prev or "").strip()
    summary = re.sub(r"\s+", " ", str(summary or "")).strip(" .;,-")
    if not summary or SUM_MARK in prev:
        return None
    head = (prev + SUM_TAG) if prev else SUM_MARK + " "
    if not _fits(head + "x" * SUM_MIN, limit, max_bytes):
        return None
    while summary and not _fits(head + summary, limit, max_bytes):
        summary = (summary.rsplit(" ", 1)[0] if " " in summary else summary[:-1]).rstrip(" ,;-")
    if len(summary) < 4:
        return None
    return head + summary


import threading as _threading
_SUM_LOCK = _threading.Lock()


def _sum_cache_path():
    import soa_review as R
    return os.path.join(getattr(R, "PDFDIR", os.path.dirname(os.path.abspath(__file__))), "_summary_cache.json")


_NO_DESC = re.compile(r"not (?:stated|specified|described|itemi[sz]ed)|no (?:service|work|item|line item|description)|"
                      r"unspecified|unitemi[sz]ed|only amounts|cannot be determined|not identified", re.I)


def summarize_texts(texts, exp_nm="", room=90):
    """첨부 본문 → 영어 한 줄 적요(품목·작업). 같은 본문은 캐시(재호출 0). 실패=None."""
    import hashlib, json
    body = "\n".join(t for t in (texts or []) if t).strip()
    if len(body) < 30:
        return None
    key = SUM_STYLE_VER + hashlib.sha256(body[:6000].encode("utf-8", "replace")).hexdigest()
    path = _sum_cache_path()
    with _SUM_LOCK:
        try:
            cache = json.load(open(path))
        except (OSError, ValueError):
            cache = {}
    if key in cache:
        v = cache[key]
        return None if (not v or _NO_DESC.search(v)) else v
    try:
        from llm_router import llm_json
        d = llm_json("Summarize what this marine invoice/document charges for, as ONE short phrase for an accounting "
                     f"remark (max {room} characters). Name the actual items/services/work. " + "Write it in the owner's Korean-English mixed style (like a ship superintendent's daily note): keep technical "
                     "terms, equipment/part names, model numbers and standard marine terms in English (e.g. Air freight, Gas analyzer, "
                     "MARPOL Oil Record Book, Starlink); write only the connecting/descriptive parts in Korean (e.g. 수리, 교체, 포함, "
                     "월 사용료, 운송, 통관). Example: 'Air freight 선용품 (Incheon→Dubai), customs clearance 포함'. " "No amounts, currency, invoice numbers, dates or vessel name. If the document does not describe "
                     "the items/services, return null (do not describe the document itself). Account hint: "
                     f"{exp_nm}. Reply JSON only: {{\"summary\": \"...\" | null}}\n\n" + body[:3500], timeout=120)
        if not isinstance(d, dict) or d.get("_llm_error") or "summary" not in d:
            return None
        v = d.get("summary")
        v = str(v).strip() if v else ""
        if v and _NO_DESC.search(v):
            v = ""
    except Exception:
        return None
    with _SUM_LOCK:
        try:
            try:
                cache = json.load(open(path))
            except (OSError, ValueError):
                cache = {}
            cache[key] = v
            with open(path + ".tmp", "w") as f:
                json.dump(cache, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    return v


SUM_VISION = os.environ.get("SOA_STAGE2_SUMMARY_VISION", "1") == "1"


def vision_summary(path, exp_nm="", room=90, maxpages=2):
    """PDF 첫 페이지들을 이미지로 읽어 적요 1줄. 텍스트 요약이 비었을 때만 호출. 캐시(파일 sha256). 실패/설명없음=None."""
    import hashlib, json, tempfile, glob, subprocess, shutil
    import soa_amount as SA
    try:
        key = "v:" + SUM_STYLE_VER + hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        return None
    cpath = _sum_cache_path()
    with _SUM_LOCK:
        try:
            cache = json.load(open(cpath))
        except (OSError, ValueError):
            cache = {}
    if key in cache:
        v = cache[key]
        return None if (not v or _NO_DESC.search(v)) else v
    tmp = tempfile.mkdtemp(prefix="soasum_")
    v = None
    try:
        stem = os.path.join(tmp, "page")
        subprocess.run(["pdftoppm", "-png", "-r", "150", "-f", "1", "-l", str(maxpages), path, stem],
                       capture_output=True, timeout=120)
        pngs = sorted(glob.glob(stem + "*.png"))
        if not pngs:
            return None
        ask = ("Read the attached invoice page image(s). Summarize what is charged for as ONE short phrase for an "
               f"accounting remark (max {room} characters): the actual items/services/work. " + "Write it in the owner's Korean-English mixed style (like a ship superintendent's daily note): keep technical "
                     "terms, equipment/part names, model numbers and standard marine terms in English (e.g. Air freight, Gas analyzer, "
                     "MARPOL Oil Record Book, Starlink); write only the connecting/descriptive parts in Korean (e.g. 수리, 교체, 포함, "
                     "월 사용료, 운송, 통관). Example: 'Air freight 선용품 (Incheon→Dubai), customs clearance 포함'. " "No amounts, currency, invoice "
               f"numbers, dates or vessel name. Account hint: {exp_nm}. If no items/services are described, return null. "
               'Reply JSON only: {"summary": "..." | null}')
        model = os.environ.get("SOA_SUMMARY_VISION_MODEL", "").strip() or \
            (SA.review_model() if SA._selected_review_is_openai() else "haiku")
        if SA._is_openai_model_name(model):
            model = model if model.startswith("openai/") else "openai/" + model
            cmd = ["openclaw", "infer", "model", "run", "--json", "--model", model, "--prompt", ask]
            for pth in pngs:
                cmd += ["--file", pth]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return None
            d = SA._infer_json_obj(r.stdout)
        else:
            names = ", ".join(os.path.basename(x) for x in pngs)
            r = subprocess.run(["claude", "-p", f"Use the Read tool on these images in {tmp}: {names}. " + ask,
                                "--model", model, "--add-dir", tmp, "--allowedTools", "Read"],
                               capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                return None
            d = SA._json_obj(r.stdout)
        if not isinstance(d, dict) or "summary" not in d:
            return None
        v = d.get("summary")
        v = str(v).strip() if v else ""
        if v and _NO_DESC.search(v):
            v = ""
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with _SUM_LOCK:
        try:
            try:
                cache = json.load(open(cpath))
            except (OSError, ValueError):
                cache = {}
            cache[key] = v
            with open(cpath + ".tmp", "w") as f:
                json.dump(cache, f)
            os.replace(cpath + ".tmp", cpath)
        except OSError:
            pass
    return v or None


def line_pdf_paths(sx, l):
    import soa_review as R
    try:
        files = [f for f in R.get_files(l.get("FILE_REF_NO") or (str(sx) + str(l.get("SX_SEQ")))) if R._is_pdf_file(f)]
        return R.dl(sx, l.get("SX_SEQ"), files) if files else []
    except BaseException:
        return []


def hdr_same(a, b):
    """헤더 재저장 후 확인: UPST_* 외 헤더 필드 불변."""
    if not a or not b:
        return False
    keys = (set(a) | set(b)) - {"UPST_DI", "UPST_USER_ID", "UPST_NM", "STATUS_DT"}
    return all(a.get(k) == b.get(k) for k in keys)


def reject_line(sx, seq, rmk):
    return save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS",
                {"SX_CD": sx, "SX_SEQ": seq, "CFM_YN": "N", "RJT_YN": "Y",
                 "RJT_RMK": rmk[:240], "STATUS2": "N", "STATUS_RMK2": None})


def _write_result(res):
    """SOA_STAGE2_RESULT_JSON 지정 시 기계용 결과 기록(그룹 자동승인 게이트용). 기록 실패는 본 흐름에 영향 없음."""
    path = os.environ.get("SOA_STAGE2_RESULT_JSON")
    if not path:
        return
    try:
        import json
        with open(path, "w") as f:
            json.dump(res, f)
    except OSError as e:
        print(f"    🚨 결과파일 기록 실패: {e}")


def run(vsl):
    if SL_TP not in DEPT_LABEL:
        print(f"🚨 SL_TP={SL_TP} 불가(04/05만)"); return 2
    _ym = r"20\d{2}(0[1-9]|1[0-2])"
    if not (re.fullmatch(_ym, str(FM_DM)) and re.fullmatch(_ym, str(TO_DM)) and FM_DM <= TO_DM):
        print(f"🚨 조회 기간 오류: {FM_DM}~{TO_DM} (yyyyMM, 시작<=끝)"); return 2
    login()
    if SL_TP == "04":
        import soa_review as R
        _own = R.my_vessels() or {}
        if _own.get(vsl) == "001":
            print(f"⏭ {vsl}: 장금 owner Crew SOA = 2차 검증 대상 아님"); return 0
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
    print(f"   SL_TP={SL_TP}({DEPT_LABEL[SL_TP]}) · SL_DM {FM_DM}~{TO_DM} · STATUS=S · 항목: A.Operational AOR · B.외화 환율(≥120%) · C.관리비 SMA · D.NRC · E.중복 인보이스 · H.12xxxx→Pre-delivery · I.적요요약\n")
    hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                 {"VSL_CD": vsl, "FM_DM": FM_DM, "TO_DM": TO_DM, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
    rows = [r for r in (hdr.get("P_RS", []) or [])
            if (r.get("SX_CD") or "")[:4] == vsl and r.get("SL_TP") == SL_TP
            and r.get("STATUS") == "S" and FM_DM <= (r.get("SL_DM") or "") <= TO_DM]

    res = {"vsl": vsl, "sl_tp": SL_TP, "checked": [], "findings": {}, "fp": {}, "warns": warns, "errors": 0, "wfail": 0, "complete": False}
    if not rows:
        print(f"검토대상 없음 (SL_TP={SL_TP} Submitted {FM_DM}~{TO_DM}).")
        res["complete"] = True; _write_result(res); return 0

    n = {"aor": 0, "pass": 0, "reject": 0, "already": 0, "resub": 0, "error": 0, "written": 0, "wfail": 0}
    todo = []
    predel = []
    dup_idx = None

    for r in sorted(rows, key=lambda x: x.get("SL_DM") or ""):
        sx = r.get("SX_CD")
        lines = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D", []) or []
        print(f"── {sx} ({r.get('SL_DM')}) | 라인 {len(lines)} ──")
        soa_fp = _soa_fp(r, lines)
        res["fp"][sx] = lines_fp(lines)
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
        for l in lines:
            nrc = nrc_scan(sx, l, warns) if NRC_SCAN else None
            dup = find_dup(dup_idx, sx, l, str(r.get("SL_DM") or "")) if dup_idx else None


            aordoc = None
            if needs_predel(l) and PREDEL_MODE == "1" and predel_line is None:
                aordoc_pre = [("H", predel_remark(l))]
            else:
                aordoc_pre = None
            if needs_predel(l) and PREDEL_MODE in ("1", "shadow") and predel_line is not None:
                predel.append((sx, l.get("SX_SEQ")))
                res.setdefault("predel", {}).setdefault(sx, []).append(str(l.get("SX_SEQ")))
                print(f"    🔁 s{l.get('SX_SEQ'):<4} [{l.get('EXP_CD')}] Cat-II {l.get('SOA_OPEX_TP')} → Pre-delivery(P)"
                      + (" (섀도·미반영)" if PREDEL_MODE == "shadow" else ""))
            if AORDOC_MODE in ("1", "shadow") and str(l.get("SOA_OPEX_TP") or "").upper() in ("A", "O") \
                    and l.get("SOA_TP") != "A" and not (needs_predel(l) and PREDEL_MODE != "0"):
                texts = line_texts(sx, l, warns)
                if texts is not None or str(l.get("SOA_OPEX_TP") or "").upper() == "A":
                    try:
                        fg, _is_aor, _info = aor_doc_check(l, texts, aor_lookup, vsl, vendor_inv_llm)
                    except BaseException as e:
                        fg = []; warns.append(f"{sx} s{l.get('SX_SEQ')} F/G 검사 예외: {str(e)[:40]}")
                    if fg and AORDOC_MODE == "shadow":
                        print(f"    👁 s{l.get('SX_SEQ'):<4} [{l.get('SOA_CUR_CD')}{l.get('SOA_AMT')}] F/G(섀도·미반영): "
                              + " | ".join(r for _, r in fg))
                        res.setdefault("aordoc_shadow", []).append(
                            {"sx": sx, "seq": str(l.get("SX_SEQ")), "remarks": [r for _, r in fg]})
                    elif fg:
                        aordoc = fg
            if aordoc_pre:
                aordoc = (aordoc or []) + aordoc_pre
            verdict, aor, note, rmk = judge_line(l, aor_lookup, SL_TP, fx_lookup, mf, nrc, dup, aordoc)
            if verdict == "skip":
                continue
            n["aor"] += 1; n[verdict] += 1
            if verdict == "pass":
                continue
            res["findings"].setdefault(sx, []).append(str(l.get("SX_SEQ")))
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
        res["checked"].append(sx)
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


    n["predel"] = n["pdfail"] = 0
    if VERIFY and PREDEL_MODE == "1" and predel and not n["error"] and not n["wfail"]:
        for sx, seq in predel:
            now = _soa_now(vsl, sx)
            cur = next((x for x in (now[1] if now else []) if str(x.get("SX_SEQ")) == str(seq)), None)
            if not now or now[0].get("STATUS") != "S" or not cur or not needs_predel(cur):
                print(f"    ↩ {sx} s{seq} 상태 변경됨/이미 수정 — Cat-II 변경 스킵"); continue
            try:
                predel_line(now[0], cur)
            except BaseException as e:
                print(f"    🚨 {sx} s{seq} Cat-II 변경 예외: {str(e)[:80]} — 이후 중단"); n["pdfail"] += 1; break
            post = _soa_now(vsl, sx)
            after = next((x for x in (post[1] if post else []) if str(x.get("SX_SEQ")) == str(seq)), None)
            if post and post[0].get("STATUS") == "S" and hdr_same(now[0], post[0]) and predel_ok(cur, after) \
                    and _fp_excl(post[1], seq) == _fp_excl(now[1], seq):
                n["predel"] += 1; print(f"    ✍ {sx} s{seq} Cat-II → Pre-delivery 변경 확인")
            else:
                n["pdfail"] += 1; print(f"    🚨 {sx} s{seq} Cat-II 변경 미확인(재조회 불일치) — 이후 중단"); break
    elif VERIFY and predel and PREDEL_MODE == "1":
        print(f"    ⏸ 확인필요/기입실패가 있어 Cat-II 변경 {len(predel)}건 보류")


    n["sum"] = n["sumfail"] = 0
    if SUMMARY_MODE == "1" and not n["error"] and not n["wfail"] and not n["pdfail"]:
        from concurrent.futures import ThreadPoolExecutor
        for r in sorted(rows, key=lambda x: x.get("SL_DM") or ""):
            sx = r.get("SX_CD")
            cur = _soa_now(vsl, sx)
            if not cur or cur[0].get("STATUS") != "S":
                continue
            cand = [l for l in cur[1] if l.get("SOA_TP") == "D" and SUM_MARK not in str(l.get("RMK") or "")
                    and SUM_MARK not in str(l.get("SUBJ") or "")]
            if not cand:
                continue

            def _one(l):
                rm = str(l.get("RMK") or "").strip()
                hd = rm + SUM_TAG if rm else SUM_MARK + " "
                room = min(120 - len(hd), (180 - len(hd.encode("utf-8"))) // 2)
                if room < SUM_MIN:
                    return l, None
                t = line_texts(sx, l, warns)
                sm = summarize_texts(t, l.get("EXP_NM") or "", room) if t else None
                if not sm and SUM_VISION:
                    for pth in line_pdf_paths(sx, l)[:2]:
                        sm = vision_summary(pth, l.get("EXP_NM") or "", room)
                        if sm:
                            break
                return l, sm
            with ThreadPoolExecutor(max_workers=4) as ex:
                got = list(ex.map(_one, cand))
            upd, exp = {}, {}
            for l, sm in got:
                new_r = summary_remark(l.get("RMK"), sm)
                new_s = summary_remark(l.get("SUBJ"), sm)
                if new_r:
                    upd[str(l.get("SX_SEQ"))] = {"RMK": new_r, **({"SUBJ": new_s} if new_s else {})}
                    exp[str(l.get("SX_SEQ"))] = {"SUBJ": l.get("SUBJ"), "RMK": l.get("RMK")}
                    print(f"    📝 s{l.get('SX_SEQ'):<4} {new_r}")
            res.setdefault("summary", {})[sx] = len(upd)
            if not upd or not VERIFY:
                continue
            import svms as _sv
            ok, msg = _sv.save_line_remarks(sx, upd, exp)
            if ok:
                n["sum"] += len(upd); print(f"    ✍ {sx} 적요 요약 {len(upd)}라인 기입 확인")
            else:
                n["sumfail"] += 1; print(f"    🚨 {sx} 적요 요약 기입 실패/미확인: {msg} — 이후 중단"); break

    print(f"=== 합계: 검사 라인 {n['aor']} | 정상 {n['pass']} | 리젝대상 {n['reject']} "
          f"| 기리젝 {n['already']} | 재상신 {n['resub']} | 확인필요 {n['error']} ===")
    if VERIFY:
        print(f"    실기입: 리젝체크 {n['written']} · 실패 {n['wfail']} · Cat-II→Pre-delivery {n['predel']} · 실패 {n['pdfail']} · 적요요약 {n['sum']} · 실패 {n['sumfail']}")
    else:
        print("    (DRY — 0 write. VERIFY=1로 실기입)")
    for w in warns:
        print(f"    ⚠️ {w}")
    print("    ⚠️ Approval·출금 Slip·리젝 제출·관리사 메일 = 미실행")
    res.update(errors=n["error"], wfail=n["wfail"] + n["pdfail"] + n["sumfail"], complete=True); _write_result(res)
    return 1 if (n["error"] or n["wfail"] or n["pdfail"] or n["sumfail"]) else 0

if __name__ == "__main__":
    if len(sys.argv) < 2 or not re.match(r"^[A-Z]{4}$", sys.argv[1].strip().upper()):
        print("사용: [VERIFY=1] python3 soa_stage2.py <VSL_CD>"); sys.exit(2)
    sys.exit(run(sys.argv[1].strip().upper()) or 0)
