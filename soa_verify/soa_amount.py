



import os, re, json, glob, subprocess, shutil
from llm_router import llm_json, review_model

def pdf_body(path):
    """pdftotext 2중추출(-layout + plain). 스캔본이면 빈 문자열."""
    txt = ""
    for args in (["pdftotext", "-layout", path, "-"], ["pdftotext", path, "-"]):
        try: txt += subprocess.run(args, capture_output=True, text=True, timeout=60).stdout + "\n"
        except Exception: pass
    return txt

def ocr_body(path, maxpages=3, dpi=200):
    """스캔본 fallback: pdftoppm dpi → tesseract."""
    import tempfile
    txt = ""
    tmp = tempfile.mkdtemp(prefix="soaocr_")
    try:
        for pg in range(1, maxpages + 1):
            stem = os.path.join(tmp, "p")
            subprocess.run(["pdftoppm", "-png", "-r", str(dpi), "-f", str(pg), "-l", str(pg), path, stem],
                           capture_output=True, timeout=90)
            pngs = glob.glob(stem + "*.png")
            if not pngs: break
            txt += subprocess.run(["tesseract", pngs[0], "-", "--psm", "6"],
                                  capture_output=True, text=True, timeout=90).stdout + "\n"
            for f in pngs:
                try: os.remove(f)
                except: pass
    except Exception: pass
    finally:
        try: os.rmdir(tmp)
        except: pass
    return txt

def body_nums(txt):
    """본문 모든 금액형 숫자(US/EU 둘 다 해석) — 환각차단 교차검증용."""
    s = set()
    for v in _body_num_list(txt):
        s.add(v)
    return s

def _money_token_values(token):
    t = re.sub(r'\s+', '', str(token or '').strip("., "))
    if not t:
        return []
    if "," in t and "." in t:

        if t.rfind(",") > t.rfind("."):
            c = t.replace(".", "").replace(",", ".")
        else:
            c = t.replace(",", "")
        cands = [c]
    elif "," in t:
        if re.search(r',\d{1,2}$', t):
            cands = [t.replace(".", "").replace(",", ".")]
        else:
            cands = [t.replace(",", "")]
    elif "." in t:
        if re.search(r'\.\d{1,2}$', t):
            cands = [t.replace(",", "")]
        else:
            cands = [t.replace(".", "")]
    else:
        cands = [t]
    vals = []
    for cand in cands:
        try:
            v = round(abs(float(cand)), 2)
            if 0.01 <= v < 1e9:
                vals.append(v)
        except Exception:
            pass
    return vals

def _body_num_list(txt):
    """본문 숫자 목록(중복 보존). 합산검증은 같은 금액 2줄도 의미가 있어 set으로 지우면 안 됨."""
    vals = []
    pat = r'\d{1,3}(?:[ \u00a0]\d{3})+[.,]\d{1,2}|\d[\d.,]*\d|\d'
    for m in re.findall(pat, txt or ""):
        vals.extend(_money_token_values(m))
    return vals

def _direct_amt(txt, amt):
    return amt is not None and any(abs(amt - n) <= 0.02 for n in body_nums(txt))

def _ocr_close(a, b):
    """OCR/LLM 소수점 누락·반올림(예: 22.38→22.3) 허용용 좁은 tolerance."""
    if a is None or b is None:
        return False
    return abs(a - b) <= max(0.02, min(0.10, abs(b) * 0.005))

def _strong_direct_context(txt, amt):
    """라인금액이 단순 파일번호/스탬프가 아니라 금액표 주변에 있는지 보수적으로 확인."""
    if amt is None:
        return False
    money_ctx = re.compile(
        r'\b(total|g/?total|grand|subtotal|amount|price|payable|invoice|unit|value|sales|account|usd|eur|sgd|jpy|krw|currency)\b'
        r'|총액|합계|총계|청구|請求|合計',
        re.I,
    )
    for line in (txt or "").splitlines():
        if _direct_amt(line, amt) and money_ctx.search(line):
            return True
    return False

_TOTAL_CTX_RE = re.compile(
    r'\b(grand\s+total|total\s+due|amount\s+due|payable|balance\s+due|invoice\s+total|invoice\s+amount|total\s+amount|total)\b'
    r'|총액|합계|총계|청구금액|請求|合計',
    re.I,
)

def _total_context_present(txt):
    return any(_TOTAL_CTX_RE.search(line or "") for line in (txt or "").splitlines())

def _direct_total_context(txt, amt):
    """라인금액이 invoice payable/total 계열 라인에 직접 표시되는지 확인."""
    if amt is None:
        return False
    for line in (txt or "").splitlines():
        if _direct_amt(line, amt) and _TOTAL_CTX_RE.search(line):
            return True
    return False

def _money_major_cents(amt):
    if amt is None:
        return None
    cents_total = int(round(abs(float(amt)) * 100))
    return str(cents_total // 100), cents_total % 100

def _ocr_digit_artifact(expected, found):
    """OCR conflict signal only: never by itself converts a line to clean."""
    a = _money_major_cents(expected)
    b = _money_major_cents(found)
    if not a or not b or a[1] != b[1]:
        return False
    exp, got = a[0], b[0]
    if exp == got:
        return False
    if len(exp) == len(got) and sum(x != y for x, y in zip(exp, got)) == 1:
        return True
    return len(exp) > len(got) and len(exp) - len(got) <= 2 and exp.endswith(got)

def _money_values_strict(txt):
    """Money-like numeric tokens without the broad US/EU expansion used for body search."""
    vals = []
    pat = r'\d{1,3}(?:[ \u00a0]\d{3})+[.,]\d{1,2}|\d[\d.,]*\d|\d'
    for m in re.findall(pat, txt or ""):
        vals.extend(_money_token_values(m))
    return vals

def _ocr_conflict_amounts(txt, expected):
    """Amounts that make OCR text untrustworthy enough to require image re-read."""
    if expected is None:
        return []
    def plausible(n):
        return expected * 0.2 <= n <= expected * 2.0
    expected_has_total_line = _direct_total_context(txt, expected)
    conflicts = []
    for line in (txt or "").splitlines():
        line_has_expected = _direct_amt(line, expected)
        line_is_total = bool(_TOTAL_CTX_RE.search(line or ""))
        for n in _money_values_strict(line):
            if abs(n - expected) <= 0.02 or not _ocr_digit_artifact(expected, n):
                continue
            if expected_has_total_line:



                if not line_is_total or line_has_expected:
                    continue
            elif line_has_expected and not plausible(n):
                continue
            if n not in conflicts:
                conflicts.append(n)
    return conflicts[:8]

def _json_obj(text):
    if not text:
        return None
    for m in reversed(list(re.finditer(r"\{.*?\}", text, re.S))):
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None

def _infer_json_obj(text):
    """Parse OpenClaw --json output or a raw model JSON object."""
    try:
        d = json.loads(text or "")
        outs = d.get("outputs") or []
        if outs and isinstance(outs[0], dict):
            return _json_obj(outs[0].get("text") or "")
    except Exception:
        pass
    return _json_obj(text)

def _is_openai_model_name(model):
    m = (model or "").lower()
    return m.startswith("openai/") or m.startswith("gpt")

def _selected_review_is_openai():
    try:
        return _is_openai_model_name(review_model())
    except Exception:
        return _is_openai_model_name(os.environ.get("SOA_REVIEW_MODEL", ""))

def _high_value_competition_note_enabled():
    return os.environ.get("SOA_HIGH_VALUE_COMPETITION_NOTE") == "1"

_ALLOC_CTX_RE = re.compile(
    r'\b(alloc(?:ated|ation)?|alloc(?:ated)?\s+share|vessel\s+share|charge(?:d)?\s*to)\b'
    r'|배분|분담|선박별|본선\s*청구|당선\s*청구',
    re.I,
)

def _amount_lines(txt, amt):
    return [line for line in (txt or "").splitlines() if _direct_amt(line, amt)]

def _line_has_ident(line, tokens):
    hay = (line or "").lower()
    for t in tokens or []:
        tok = str(t or "").strip().lower()
        if not tok:
            continue
        if tok.isdigit():
            if re.search(rf'(?<!\d){re.escape(tok)}(?!\d)', hay):
                return True
        elif re.search(rf'(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])', hay, re.I):
            return True
    return False

def _gpt_overflag_guard(txt, expected, found, vessel=None, ident=None):
    """Return a reason when GPT's competing amount should not veto direct evidence.

    GPT mini has false-flagged lines where the actual allocated/vessel amount is
    visible, but another invoice total/unit amount is also present. This guard is
    deliberately narrow: it only rescues direct+strong matches with positive
    allocation/share/charged-to evidence tied to this vessel's exact identity.
    Allocation wording alone can point to another vessel; vessel identity alone
    can appear beside unit prices. OCR digit-artifacts are handled earlier by
    the OCR hires/vision conflict path, not here.
    """
    if expected is None or found is None:
        return None
    exp_lines = _amount_lines(txt, expected)
    found_lines = _amount_lines(txt, found)
    if not exp_lines or not found_lines:
        return None
    tokens = list(ident or [])
    if not tokens:
        return None
    for line in exp_lines:
        if _ALLOC_CTX_RE.search(line) and _line_has_ident(line, tokens):
            return "라인금액에 배분+선박신원 문맥 있음"
    return None

def _vision_prompt(vessel, line_cur, conflicts=None, image_names=None, claude_read_tool=False):
    imgs = ", ".join(image_names or []) or "attached page images"
    conflict_txt = ", ".join(str(c) for c in (conflicts or [])[:5]) or "none"
    opener = (
        "Open and visually inspect these PDF page image files with the Read tool: "
        if claude_read_tool else
        "Visually inspect the attached PDF page image files: "
    )
    return (
        opener + f"{imgs}\n"
        "Do not rely on OCR text. Read the visibly payable/total/allocated amount from the document image.\n"
        f"Vessel/context: {vessel}\n"
        f"Currency/context: {line_cur}\n"
        f"OCR conflict amounts to ignore unless actually visible: {conflict_txt}\n"
        "Confirm only when a payable/total/allocated amount for this vessel is visually clear. "
        "Also say whether the vessel name/recipient/context is visually confirmed. "
        "If ambiguous, false. Do not invent or copy any expected amount from the prompt.\n"
        'Return JSON only: {"confirmed": true|false, "amount": <number|null>, '
        '"vessel_confirmed": true|false, "confidence": 0~1, "evidence": "short visual evidence"}'
    )

def vision_amount_confirm(path, vessel, line_amt, line_cur, conflicts=None, maxpages=2):
    """Render PDF pages and ask a vision reader to confirm the exact billed/allocation amount.

    This is a pre-review evidence extractor for OCR-conflict scans. It is fail-closed:
    unavailable vision, non-JSON, low confidence, or unclear image returns confirmed=False.
    """
    if os.environ.get("SOA_ENABLE_VISION_AMOUNT", "1") == "0":
        return {"confirmed": False, "reason": "vision disabled"}
    la = f(line_amt)
    if la is None:
        return {"confirmed": False, "reason": "bad line amount"}
    tmp = None
    try:
        import tempfile
        tmp = tempfile.mkdtemp(prefix="soavision_")
        stem = os.path.join(tmp, "page")
        subprocess.run(
            ["pdftoppm", "-png", "-r", "180", "-f", "1", "-l", str(maxpages), path, stem],
            capture_output=True,
            timeout=120,
        )
        pngs = sorted(glob.glob(stem + "*.png"))
        if not pngs:
            return {"confirmed": False, "reason": "render failed"}
        model = os.environ.get("SOA_VISION_MODEL", "").strip()
        if not model:
            model = review_model() if _selected_review_is_openai() else "sonnet"
        if _is_openai_model_name(model):
            if not model.startswith("openai/"):
                model = "openai/" + model
            prompt = _vision_prompt(vessel, line_cur, conflicts, [os.path.basename(p) for p in pngs],
                                    claude_read_tool=False)
            cmd = ["openclaw", "infer", "model", "run", "--json", "--model", model, "--prompt", prompt]
            for p in pngs:
                cmd.extend(["--file", p])
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            d = _infer_json_obj(r.stdout)
        else:
            prompt = _vision_prompt(vessel, line_cur, conflicts, [os.path.basename(p) for p in pngs],
                                    claude_read_tool=True)
            r = subprocess.run(
                ["claude", "-p", prompt, "--model", model, "--add-dir", tmp, "--allowedTools", "Read"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            d = _json_obj(r.stdout)
        if not isinstance(d, dict):
            return {"confirmed": False, "reason": "vision non-json"}
        amount = f(d.get("amount"))
        try:
            conf = float(d.get("confidence", 0) or 0)
        except Exception:
            conf = 0.0
        confirmed = (
            (d.get("confirmed") is True or str(d.get("confirmed")).lower() == "true")
            and amount is not None
            and abs(amount - la) <= 0.02
            and conf >= 0.75
        )
        return {
            "confirmed": confirmed,
            "amount": amount,
            "vessel_confirmed": d.get("vessel_confirmed") is True or str(d.get("vessel_confirmed")).lower() == "true",
            "confidence": conf,
            "evidence": str(d.get("evidence") or "")[:160],
        }
    except Exception as e:
        return {"confirmed": False, "reason": f"vision error: {str(e)[:80]}"}
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

def _ident_in_text(ident, txt):
    hay = (txt or "").lower()
    for t in (ident or []):
        tok = str(t).strip().lower()
        if not tok:
            continue
        if tok.isdigit():
            if re.search(rf'(?<!\d){re.escape(tok)}(?!\d)', hay):
                return True
        elif re.search(rf'(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])', hay, re.I):
            return True
    return False

def _high_value_ident_missing(usd_amt, ident, body):
    ua = f(usd_amt)
    if ua is None or ua < BACKSTOP_USD:
        return False
    return not _ident_in_text(ident, body)

def _high_value_vision_ident_missing(usd_amt, vision_result):
    ua = f(usd_amt)
    return ua is not None and ua >= BACKSTOP_USD and not bool((vision_result or {}).get("vessel_confirmed"))

def _small_sum_match(nums, target, max_terms=2):
    """OCR이 총액 셀만 놓친 경우용: 소수 개 line-item 합이 라인금액과 맞는지 확인."""
    if target is None:
        return False
    vals = sorted(n for n in nums if 0.01 <= n <= target and abs(n - target) > 0.02)
    vals = vals[:120]
    def rec(start, left, depth):
        if abs(left) <= 0.02:
            return True
        if depth == 0 or left < -0.02:
            return False
        for i in range(start, len(vals)):
            v = vals[i]
            if v - left > 0.02:
                break
            if rec(i + 1, round(left - v, 2), depth - 1):
                return True
        return False
    return rec(0, round(target, 2), max_terms)

def haiku_alloc(txt, vessel, line_amt, line_cur):
    """이 선박 배분/청구액이 라인금액과 맞는지 의미판독. JSON 한 줄."""
    prompt = (
        "너는 선박 SOA 비용검증기다. 첨부 인보이스/DN 본문에서 이 선박에 '실제 청구된(또는 배분된)' 금액을 찾아라.\n"
        f"선박: {vessel}\n"
        f"SVMS 라인 금액: {line_amt} {line_cur}\n"
        "공유 인보이스면 선박별 배분액(스탬프/내역표 행)을, 외화면 환산전 금액을, 수기메모면 그 숫자를 본다.\n"
        f"먼저 라인 금액({line_amt})이 본문에 그대로 적혀 있는지 확인하라. 적혀 있고 그게 이 선박 청구액이면 matches=true.\n"
        "라인 금액이 문서에 '정당하게' 존재하면 matches=true. 총액만 있고 배분 불명/품목·금액 자체가 안 맞으면 false.\n"
        "⚠️ matches=false로 거부하려면 반드시 found_amount에 '진짜 맞다고 보는 금액'(본문에 실재하는 숫자)을 적어라. "
        "근거 숫자를 못 대면서 거부하지 마라 — 대체 금액을 못 찾으면 라인 금액을 반증한 게 아니다(그땐 matches=true 또는 found_amount=라인값).\n"
        '설명 금지, JSON 한 줄만: {"matches": true|false, "found_amount": <숫자|null>, '
        '"basis": "total|allocation|converted|handwritten|none", "confidence": 0~1, "evidence": "근거 짧게"}\n\n'
        + (txt or "")[:4000])
    return llm_json(prompt, timeout=120)

def f(x):
    try: return round(abs(float(str(x).replace(",", ""))), 2)
    except: return None

try:
    BACKSTOP_USD = float(os.environ.get("SOA_BACKSTOP_USD", "5000"))
except (TypeError, ValueError):
    BACKSTOP_USD = 5000.0

def resolve(path, vessel, line_amt, line_cur, usd_amt=None, ident=None):
    """returns dict: status=clean|flag|scan, basis, reason. 라인금액이 인보이스로 뒷받침되면 clean.
    usd_amt: 라인 USD환산액(고액 신원가드용, 옵션). ident: 선박 신원토큰[IMO, 선명](고액 direct-clean 방어, 옵션)."""
    la = f(line_amt)
    ua_raw = f(usd_amt)
    ua = ua_raw
    if (ua is None or ua <= 0) and la is not None:
        ua = la



    _ds, _dss = os.environ.get("SOA_DIRECT_SKIP"), os.environ.get("SOA_DIRECT_SKIP_SHADOW")
    if _ds == "1" or _dss == "1":
        try:
            import soa_direct_gate as _G
            _act, _rsn = _G.decide(path, line_amt, usd_amt, line_cur, ident)
        except Exception as _e:
            _act, _rsn = "llm", f"gate예외:{str(_e)[:50]}"
        if _dss == "1":
            try:
                def _san(x): return str(x).replace(",", " ").replace("\n", " ").replace("\r", " ")
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "direct_skip_shadow.csv"), "a") as _sf:
                    _sf.write(f"{_san(vessel)},{_san(line_amt)},{_san(line_cur)},{_act},{_san(_rsn)}\n")
            except Exception: pass


        if _ds == "1" and _act == "skip_clean" and ua_raw is not None and 0 < ua_raw < BACKSTOP_USD:
            return dict(status="clean", basis="direct-skip", reason="[direct-skip] " + str(_rsn), src="gate")
    body = pdf_body(path)
    src = "text"
    nums = body_nums(body)


    if (not body.strip() or len(body.strip()) < 15 or
            (not _direct_amt(body, la) and (len(body.strip()) < 500 or len(nums) < 8))):
        body = ocr_body(path); src = "ocr"
    if not body.strip():
        return dict(status="scan", reason="본문 추출불가(스캔+OCR실패) — 사람 vision", src=src)
    nums = body_nums(body)
    if src == "ocr" and la is not None and not _direct_amt(body, la) and _total_context_present(body):
        hi_body = ocr_body(path, dpi=300)
        if hi_body.strip() and _direct_amt(hi_body, la):
            body = hi_body
            src = "ocr-hires"
            nums = body_nums(body)

    direct = _direct_amt(body, la)
    strong_direct = direct and _strong_direct_context(body, la)
    ocr_conflicts = _ocr_conflict_amounts(body, la) if src == "ocr" and direct and strong_direct else []
    if ocr_conflicts:
        hi_body = ocr_body(path, dpi=300)
        hi_direct = _direct_amt(hi_body, la)
        hi_strong = hi_direct and _strong_direct_context(hi_body, la)
        hi_conflicts = _ocr_conflict_amounts(hi_body, la) if hi_direct and hi_strong else []
        if hi_body.strip() and hi_direct and hi_strong and not hi_conflicts:
            body = hi_body
            src = "ocr-hires"
            nums = body_nums(body)
            direct = hi_direct
            strong_direct = hi_strong
            ocr_conflicts = []
        else:
            vr = vision_amount_confirm(path, vessel, line_amt, line_cur, ocr_conflicts)
            if vr.get("confirmed"):
                if _high_value_vision_ident_missing(ua, vr):
                    return dict(status="flag", basis="ident",
                                reason=f"원본 vision 금액확인 but 고액(${ua})·선박신원 vision 미확인 — 사람확인",
                                src="vision", found=vr.get("amount"))
                return dict(status="clean", basis="vision-amount",
                            reason=f"원본 vision 재판독으로 라인 {la} 직접확인: {vr.get('evidence')}",
                            src="vision", found=vr.get("amount"))
            return dict(status="flag", basis="vision-unconfirmed",
                        reason=f"OCR 금액충돌 {ocr_conflicts} — 원본 vision 재판독 미확정({vr.get('reason') or vr.get('evidence') or 'unclear'})",
                        src="vision", found=vr.get("amount"))

    hk = haiku_alloc(body, vessel, line_amt, line_cur)
    llm_error = bool(hk.get("_llm_error"))
    hamt = f(hk.get("found_amount")); basis = hk.get("basis", "")

    m = hk.get("matches")
    matches = (m is True) or (isinstance(m, str) and m.strip().lower() == "true")
    matches_false = (m is False) or (isinstance(m, str) and m.strip().lower() == "false")
    try: conf = float(hk.get("confidence", 0))
    except Exception: conf = 0.0

    h_real = hamt is not None and any(abs(hamt - n) <= 0.5 for n in nums)


    h_competes = (hamt is not None and abs(hamt - la) > 0.02 and h_real) if la is not None else False
    haiku_strong_reject = matches_false and conf >= 0.6 and h_competes
    gpt_guard_reason = None
    if haiku_strong_reject and direct and strong_direct and _selected_review_is_openai():
        gpt_guard_reason = _gpt_overflag_guard(body, la, hamt, vessel=vessel, ident=ident)
        if gpt_guard_reason and ua is not None and ua < BACKSTOP_USD:
            haiku_strong_reject = False
    if (haiku_strong_reject and gpt_guard_reason and _high_value_competition_note_enabled()
            and ua is not None and ua >= BACKSTOP_USD and not _high_value_ident_missing(ua, ident, body)):
        return dict(status="clean", basis="high-value-competition",
                    reason=f"고액금액 경쟁 확인 요: 라인 {la} 본문 직접확인 but GPT 경쟁금액 {hamt} ({basis}) conf={conf}",
                    src=src, found=hamt, review_remark="고액금액 경쟁 확인 요")
    if direct and llm_error and not strong_direct:
        return dict(status="flag", basis="llm-error",
                    reason=f"라인 {la} 본문직접매칭 but LLM 실행실패/비JSON — 사람확인", src=src)
    if direct and not haiku_strong_reject:





        if _high_value_ident_missing(ua, ident, body):
            return dict(status="flag", basis="ident",
                        reason=f"라인 {la} 본문직접매칭 but 고액(${ua})·선박신원(IMO/선명) 본문 미확인 — 사람확인", src=src)
        reason = f"라인 {la} 본문 직접확인"
        if gpt_guard_reason:
            reason += f" (GPT overflag guard: {gpt_guard_reason})"
        return dict(status="clean", basis="in-body", reason=reason, src=src)
    if (src == "ocr" and matches and hamt is not None and la is not None and _ocr_close(hamt, la)
            and conf >= 0.7 and basis != "none" and _small_sum_match(_body_num_list(body), la)):
        return dict(status="clean", basis=basis,
                    reason=f"LLM 배분확인 {hamt} ({basis}) + OCR line-item 합산근거 conf={conf}", src=src,
                    found=hamt)
    if matches and h_real and conf >= 0.6 and basis != "none":
        return dict(status="clean", basis=basis, reason=f"LLM 배분확인 {hamt} ({basis}) conf={conf}", src=src,
                    found=hamt)



    if src.startswith("ocr") and la is not None and not direct:
        vr = vision_amount_confirm(path, vessel, line_amt, line_cur, [])
        if vr.get("confirmed"):
            if _high_value_vision_ident_missing(ua, vr):
                return dict(status="flag", basis="ident",
                            reason=f"스캔 원본 vision 금액확인 but 고액(${ua})·선박신원 미확인 — 사람확인",
                            src="vision", found=vr.get("amount"))
            return dict(status="clean", basis="vision-amount",
                        reason=f"스캔 원본 vision 재판독으로 라인 {la} 직접확인: {vr.get('evidence')}",
                        src="vision", found=vr.get("amount"))

    found_txt = f"{hamt}" if h_real else (f"{hamt}(AI추정·본문없음)" if hamt is not None else "None")
    return dict(status="flag", basis=basis,
                reason=f"라인 {la} 인보이스 미확인 (LLM matches={matches} found={found_txt} basis={basis} conf={conf})",
                src=src, body_top=sorted([n for n in nums if la and n >= la * 0.5])[:10])

def haiku_invoice_total(txt, vessel, line_cur):
    """이 인보이스 1장의 '청구 총액(grand total)' 단일 숫자만 뽑는다. 합산검증용. 숫자|null."""
    prompt = (
        "너는 인보이스 판독기다. 아래 인보이스 본문에서 이 문서의 '최종 청구 총액(grand total / total amount due)' "
        "단 하나의 숫자만 찾아라. 라인아이템·소계·세금 말고 최종 합계. 통화 무시, 숫자만.\n"
        f"(참고 통화: {line_cur})\n"
        '설명 금지, JSON 한 줄만: {"total": <숫자|null>, "confidence": 0~1}\n\n'
        + (txt or "")[:4000])
    try:
        d = llm_json(prompt, timeout=120)
        t = f(d.get("total"))
        raw = d.get("total")

        if isinstance(raw, (int, float)) and raw <= 0:
            return None, 0.0
        if t is not None and t <= 0:
            return None, 0.0
        return t, float(d.get("confidence", 0) or 0)
    except Exception:
        return None, 0.0


_CREDIT_RE = re.compile(
    r'credit\s*note|credit\s*memo|refund|환불|반품|환입|마이너스'
    r'|\(\s*[\d,]+(?:\.\d+)?\s*\)'
    r'|-\s*\d{1,3}(?:,\d{3})+'
    r'|-\s*\d+\.\d{2}\b', re.I)

def resolve_sum(paths, vessel, line_amt, line_cur):
    """다중 인보이스 합산검증(총합 미기재 분할청구) — **물리적으로 분리된 첨부 2장↑** 만 대응.
    각 파일의 '청구 총액'을 뽑아 합이 라인금액과 정확일치하면 clean.
    보수적: 각 총액 ①Haiku추출(양수만) ②본문 실재(±0.02, 환각차단) ③conf≥0.6 ④합 정확일치(tol≤10)만 clean.
            하나라도 불충족·credit/음수·판독불가=None(→사람).
    ※ 단일 PDF 안 다중 인보이스는 '라인아이템 합=총액' 회계항등식 false-positive 위험 + 보통 스캔이라
       의도적으로 미지원(사람). 스캔본도 OCR 불완전 → 자연히 None → 사람."""
    la = f(line_amt)
    if la is None or not paths or len(paths) < 2:
        return None
    if len(paths) > 6:
        return None
    totals = []
    for p in paths:
        body = pdf_body(p)
        if len(body.strip()) < 15:
            body = ocr_body(p)
        if not body.strip() or _CREDIT_RE.search(body):
            return None
        t, conf = haiku_invoice_total(body, vessel, line_cur)
        if t is None or conf < 0.6 or not any(abs(t - n) <= 0.02 for n in body_nums(body)):
            return None
        totals.append(t)
    s = round(sum(totals), 2)
    tol = max(1.0, min(la * 0.0005, 10.0))
    if abs(s - la) <= tol:
        return dict(status="clean", basis="multi-sum",
                    reason=f"다중인보이스 {len(paths)}장 합산 {totals}={s} ≈ 라인 {la}")
    return dict(status="flag", basis="multi-sum",
                reason=f"다중합산 {totals}={s} ≠ 라인 {la} — 사람")

if __name__ == "__main__":
    import sys
    print(json.dumps(resolve(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "USD"),
                     ensure_ascii=False, indent=2))
