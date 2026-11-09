












import os, re, json, subprocess
import soa_amount as SA

BACKSTOP = getattr(SA, "BACKSTOP_USD", 5000.0)
_ROSTER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_cache", "roster.json")

def _load_roster():
    try:
        with open(_ROSTER_PATH) as f:
            d = json.load(f)
    except Exception:
        return []
    out = []
    for v in d.get("vessels", []):
        nm = (v.get("name") or "").strip().lower()
        imo = re.sub(r"\D", "", str(v.get("imo") or ""))
        if nm or imo:
            out.append((nm, imo))
    return out

_ROSTER = _load_roster()
_CREDIT = re.compile(r"credit\s*note|credit|refund|\bcr\b|\(\s*[\d,]+(?:\.\d{2})?\s*\)|[-−]\s*[\d,]+\.\d{2}", re.I)

def layout_body(path):
    """-layout 단독 추출(행 보존). same-line 판정 전용. plain concat 금지."""
    try:
        return subprocess.run(["pdftotext", "-layout", path, "-"],
                              capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return ""

def _amt_regex(la):
    """la(abs float) 포맷형 표기 매칭 — 1234.56 / 1,234.56 / 599,99(EU). 앞뒤 숫자 경계."""
    a = round(la, 2)
    variants = {f"{a:.2f}", f"{a:,.2f}", f"{a:.2f}".replace(".", ",")}
    body = "|".join(re.escape(v) for v in variants)
    return re.compile(r"(?<![\d.,])(" + body + r")(?![\d])")

def _ident_toks(ident):
    return [str(t).strip().lower() for t in (ident or []) if str(t).strip()]

def _self_hit(text, self_toks):
    tl = text.lower()
    return any(t and len(t) >= 4 and t in tl for t in self_toks)

def _others_in(text, self_toks):
    """text 안 self가 아닌 roster 선박 집합. 선명=단어경계 매칭(부분문자열 오탐 축소), IMO=7자리 정확토큰.
       ⚠️ roster 미등록 타선박은 원리상 탐지 불가 → single 판정은 imo7 카운트로 보수 방어(decide 참조)."""
    tl = text.lower()
    selfset = set(self_toks)
    imos_in = set(re.findall(r"(?<!\d)\d{7}(?!\d)", text))
    found = set()
    for nm, imo in _ROSTER:
        if nm and nm in selfset:
            continue
        if nm and len(nm) >= 5 and re.search(r"(?<![a-z])" + re.escape(nm) + r"(?![a-z])", tl):
            found.add(nm)
        elif imo and imo in imos_in:
            found.add(imo)
    return found

def decide(path, line_amt_signed, usd_amt, cur, ident):
    """returns (action, reason). action: 'skip_clean'(LLM생략 clean) | 'llm'(기존 경로).
       fail-closed: 불확실하면 'llm'."""
    la = SA.f(line_amt_signed)
    if la is None:
        return "llm", "라인금액 파싱불가"
    ua = SA.f(usd_amt)
    high_value = (ua is None) or (ua >= BACKSTOP)
    if isinstance(line_amt_signed, (int, float)) and line_amt_signed < 0:
        return "llm", "credit(음수 라인)"
    body = layout_body(path)
    if not body.strip():
        return "llm", "본문 추출불가(스캔/OCR) — 게이트 off"
    pat = _amt_regex(la)
    lines_with_amt = [ln for ln in body.splitlines() if pat.search(ln)]
    if not lines_with_amt:
        return "llm", f"포맷형 금액 {la} 본문 미실재"
    if any(_CREDIT.search(ln) for ln in lines_with_amt):
        return "llm", "credit/괄호/마이너스 표기 인접 — 게이트 off"
    self_toks = _ident_toks(ident)
    self_imo = next((t for t in self_toks if t.isdigit() and len(t) >= 7), None)
    imo7 = set(re.findall(r"(?<!\d)\d{7}(?!\d)", body))
    self_name_present = _self_hit(body, self_toks)
    self_imo_present = bool(self_imo) and self_imo in imo7
    others_doc = _others_in(body, self_toks)
    single = (len(others_doc) == 0 and self_name_present and len(imo7) <= 1)

    if high_value:
        if single and self_imo_present:
            return "skip_clean", f"단일선박 direct(고액·IMO확인) {la}{cur}"
        return "llm", "고액/ua불명 — 단일선박+IMO 미충족(보수 폴백)"

    if single:
        return "skip_clean", f"단일선박 direct {la}{cur}"

    for ln in lines_with_amt:
        if _self_hit(ln, self_toks) and not _others_in(ln, self_toks):
            return "skip_clean", f"배분표 same-line exclusive {la}{cur}"
    return "llm", "배분표 same-line 자기전용 미확인/타선박 혼재"
