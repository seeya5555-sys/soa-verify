





import os, re, sys, csv, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from svms import login, select, select_opt, save, download_nas
import soa_amount as SA


from svms import USER_ID as USER
PDFDIR = os.path.join(os.path.dirname(__file__), "soa_pdfs"); os.makedirs(PDFDIR, exist_ok=True)
LOG = os.path.join(os.path.dirname(__file__), "soa_review_log.csv")
TOL = 0.02


TRAVEL_EXP_CD = "080103"
try:
    TRAVEL_CFM_MAX = float(os.environ.get("SOA_TRAVEL_CFM_MAX", "2000"))
except (TypeError, ValueError):
    TRAVEL_CFM_MAX = 2000.0






import soa_groups as SG

_GROUPS_CFG = None

def groups_cfg():
    """그룹 설정 — 프로세스당 1회만 로드(run 중 설정이 바뀌어도 split-brain 없음)."""
    global _GROUPS_CFG
    if _GROUPS_CFG is None:
        _GROUPS_CFG = SG.load_groups()
    return _GROUPS_CFG

def group_keys():
    """유효 그룹 키(구 GROUPS 상수 대체 — usage 검증·순회용)."""
    return SG.group_keys(groups_cfg())



KNOWN_VESSELS = set()

def resolve_targets(group, owner):
    """그룹의 실제 검토대상 VSL_CD — My Vessel 선주분류(owner) 기반 동적.
       dynamic_owner 그룹 = 해당 owner 전체(신규선 자동편입) / explicit = 명시 리스트 ∩ 해당 owner."""
    return SG.resolve_targets(group, owner, groups_cfg())

def uncovered_vessels(owner):
    """어느 그룹에도 안 잡히는 My Vessel = 조용한 누락 방지 경고.
       dynamic_owner 그룹이 덮는 owner는 항상 커버 → explicit 미등록만 반환."""
    return SG.uncovered_vessels(owner, groups_cfg())

def explicit_union(owner_comp_id=None):
    """explicit 그룹에 명시 등록된 선박 합집합(파티션 이탈 감지용)."""
    out = set()
    for g in groups_cfg().values():
        if g["mode"] != "explicit":
            continue
        if owner_comp_id and g["owner_comp_id"] != owner_comp_id:
            continue
        out.update(g["vessels"])
    return out
NUM = re.compile(r'\d[\d,]*(?:\.\d+)?')

def fnums(s):
    out = []
    for m in NUM.findall(s or ""):
        try: out.append(round(abs(float(m.replace(",", ""))), 2))
        except: pass
    return out

def _n(x):
    try: return round(abs(float(str(x).replace(",", ""))), 2)
    except: return None

def line_amts(l):
    v = {}
    for k in ("SOA_AMT", "AMT_USD", "AMT_LOC", "AMT"):
        x = l.get(k)
        if x not in (None, ""):
            n = _n(x)
            if n is not None: v[k] = n
    return v

def usd(l):
    return _n(l.get("AMT_USD")) or 0

def get_files(fref):
    key = str(fref or "")
    with _FILE_CACHE_LOCK:
        if key in _FILE_CACHE:
            return _FILE_CACHE[key]
    r = select("PKG_CM_FILE", "SP_GET_FILE", {"REF_NO": fref, "REF_CD": fref, "FILE_TP": "COSX", "USE_YN": "Y"})
    files = r.get("P_RS", []) or []
    with _FILE_CACHE_LOCK:
        _FILE_CACHE[key] = files
    return files

def aor_doc(vsl, ref_no):
    if not ref_no: return None
    r = select("PKG_CM_REF", "SP_GET_REF_DOC",
               {"FM_DT": "20240101", "TO_DT": "20261231", "REF_TP": "COAR", "VSL_CD": vsl, "REF_NO": ref_no})
    rs = r.get("P_RS", []) or []
    return rs[0] if rs else None

VSL_IDENT = {}

def my_vessels():
    """My Vessel 재조회 + 선주분류(가변이라 매번). returns {vsl: owner_comp_id}. VSL_IDENT도 채움(추가호출 0)."""
    u = select("PKG_CM_USER", "SP_GET_USER_INFO", {"USER_ID": USER})
    vsls = [r.get("VSL_CD") for r in (u.get("P_RS_VSL") or []) if r.get("VSL_CD")]
    owner = {}
    for v in vsls:
        try:
            d = select("PKG_CM_CD", "SP_GET_VSL", {"VSL_CD": v}).get("P_RS", []) or []
            row = d[0] if d else {}
            owner[v] = row.get("OW_COMP_ID")
            toks = [str(row.get(k)).strip() for k in ("IMO_NO", "NAME") if str(row.get(k) or "").strip()]
            VSL_IDENT[v] = toks
        except (Exception, SystemExit):
            owner[v] = None
    return owner

def confirm_line(sx, seq):
    return save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS",
                {"SX_CD": sx, "SX_SEQ": seq, "CFM_YN": "Y", "RJT_YN": "N",
                 "RJT_RMK": None, "STATUS2": "N", "STATUS_RMK2": None})

_FILE_CACHE = {}
_FILE_CACHE_LOCK = threading.Lock()
_PREFETCH_STATS = {"files": 0, "hit": 0, "downloaded": 0, "failed": 0, "seconds": 0.0}
_PREFETCH_STATS_LOCK = threading.Lock()

def _fref(sx, seq, line):
    return line.get("FILE_REF_NO") or (str(sx) + str(seq))

def _pdf_path(sx, seq, fl):
    fn = fl.get("FILE_NM") or "f"
    return os.path.join(PDFDIR, re.sub(r'[^\w.\-]', '_', f"{sx}_{seq}_{fn}")[:120])

def _is_pdf_file(fl):
    return ((fl.get("FILE_EXT") or "").lower() in ("", "pdf") and
            (fl.get("FILE_NM") or fl.get("SAVE_NM")))

def _download_pdf(fl, path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return "hit"
    download_nas(fl.get("FOLDER_PATH"), fl.get("FILE_NM") or "f", fl.get("SAVE_NM"), path)
    return "downloaded"

def prefetch_line_pdfs(sx, lines):
    """SOA 라인 첨부 PDF를 선행 다운로드한다. 읽기 전용 I/O만 병렬화하고 판정/write는 건드리지 않음."""
    if os.environ.get("SOA_PREFETCH", "1") != "1":
        return {"enabled": False}
    try:
        max_workers = max(1, min(8, int(os.environ.get("SOA_PREFETCH_WORKERS", "4"))))
    except (TypeError, ValueError):
        max_workers = 4
    tasks = {}
    meta_failed = 0
    for l in lines:
        seq = l.get("SX_SEQ")
        try:
            for fl in get_files(_fref(sx, seq, l)):
                if not _is_pdf_file(fl):
                    continue
                p = _pdf_path(sx, seq, fl)
                tasks.setdefault(p, fl)
        except (Exception, SystemExit):
            meta_failed += 1
    if not tasks:
        with _PREFETCH_STATS_LOCK:
            _PREFETCH_STATS["failed"] += meta_failed
        return {"enabled": True, "files": 0, "hit": 0, "downloaded": 0, "failed": meta_failed, "seconds": 0.0}

    t0 = time.time()
    stats = {"enabled": True, "files": len(tasks), "hit": 0, "downloaded": 0, "failed": meta_failed, "seconds": 0.0}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as ex:
        futs = {ex.submit(_download_pdf, fl, p): p for p, fl in tasks.items()}
        for fut in as_completed(futs):
            try:
                stats[fut.result()] += 1
            except Exception:
                stats["failed"] += 1
    stats["seconds"] = round(time.time() - t0, 2)
    with _PREFETCH_STATS_LOCK:
        for k in ("files", "hit", "downloaded", "failed"):
            _PREFETCH_STATS[k] += stats[k]
        _PREFETCH_STATS["seconds"] += stats["seconds"]
    return stats


_AMT_RJT_RE = re.compile(r"match|mach|금액|amount|diff|invoice|cost\s*no|불일치", re.I)
def _is_amount_reject(rmk):
    return bool(_AMT_RJT_RE.search(rmk or ""))

def dl(sx, seq, files):
    paths = []
    for fl in files:
        if not _is_pdf_file(fl): continue
        p = _pdf_path(sx, seq, fl)
        try:
            _download_pdf(fl, p)
            paths.append(p)
        except Exception: pass
    return paths

def verify_opex(sx, seq, vsl, l, thorough):
    """returns (decision, reason). decision: confirm|flag|scan."""
    files = get_files(l.get("FILE_REF_NO") or (str(sx) + str(seq)))
    if not files:
        return "flag", "첨부 없음"


    if (l.get("EXP_CD") or "") == TRAVEL_EXP_CD and usd(l) < TRAVEL_CFM_MAX:
        _tp = dl(sx, seq, files)
        if _tp and re.search(r'N2F|Expense\s*Report', SA.pdf_body(_tp[0])[:3000], re.I):
            return "confirm", f"감독출장비 자동확인(N2F, USD {usd(l)}<{TRAVEL_CFM_MAX:.0f})"
    amts = line_amts(l)
    soa_amt = l.get("SOA_AMT"); cur = l.get("SOA_CUR_CD") or "USD"
    fnnums = []
    for fl in files:
        fnnums += fnums(fl.get("FILE_NM", "")) + fnums(fl.get("SAVE_NM", ""))
    fn_match = any(abs(a - n) <= TOL for a in amts.values() for n in fnnums)


    pdfs = dl(sx, seq, files)
    if not pdfs:
        return ("confirm", f"파일명매칭(다운실패폴백) {soa_amt}{cur}") if fn_match else ("flag", "PDF 다운 실패")
    best = None
    for p in pdfs:
        r = SA.resolve(p, vsl, soa_amt, cur, usd_amt=usd(l), ident=VSL_IDENT.get(vsl))
        if r["status"] == "clean":
            return "confirm", r["reason"]
        best = r

    rs = SA.resolve_sum(pdfs, vsl, soa_amt, cur)
    if rs and rs["status"] == "clean":
        return "confirm", rs["reason"]
    if rs and rs["status"] == "flag":
        best = rs
    if best and best["status"] == "scan":
        return "scan", best["reason"]
    return "flag", (best or {}).get("reason", "본문 미확인")

def verify_aor(sx, seq, vsl, l):
    files = get_files(l.get("FILE_REF_NO") or (str(sx) + str(seq)))
    ref = l.get("REF_NO")
    adoc = aor_doc(vsl, ref)
    subj = (adoc or {}).get("SUBJ")
    amts = line_amts(l)

    aor_cost = None
    for k in ("COST", "AMT", "AMT_USD"):
        v = (adoc or {}).get(k)
        if v not in (None, ""):
            try: aor_cost = round(abs(float(str(v).replace(",", ""))), 2); break
            except: pass
    if aor_cost is not None and any(abs(a - aor_cost) <= TOL for a in amts.values()):
        return "confirm", f"AOR 승인액 일치 {aor_cost}", subj
    fnnums = []
    for fl in files:
        fnnums += fnums(fl.get("FILE_NM", "")) + fnums(fl.get("SAVE_NM", ""))
    fn_match = any(abs(a - n) <= TOL for a in amts.values() for n in fnnums)
    if fn_match:
        return "confirm", f"AOR DN 파일명매칭 (subj={'O' if subj else 'X'})", subj

    pdfs = dl(sx, seq, files)
    for p in pdfs:
        r = SA.resolve(p, vsl, l.get("SOA_AMT"), l.get("SOA_CUR_CD") or "USD", usd_amt=usd(l), ident=VSL_IDENT.get(vsl))
        if r["status"] == "clean":
            return "confirm", f"AOR DN 본문확인 {r['reason']}", subj
    return "flag", "AOR DN 금액 미확인", subj

def _empty_frag():
    return {"confirm": [], "flag": [], "scan": [], "aor_nodoc": [],
            "code09": [], "crew_confirm": [], "err": []}

def _merge_frag(out, frag):
    for k, rows in frag.items():
        if k in out:
            out[k].extend(rows)

def _review_soa_row(r, group, owner, skrt_set, thorough_group):
    """한 SOA 헤더의 read-only 검토. write/audit 파일 기록은 caller가 순차 수행."""
    frag = _empty_frag()
    log_rows = []
    sx = r.get("SX_CD"); vsl = (sx or "")[:4]; sl_tp = r.get("SL_TP")
    if not sx:
        return None, frag, log_rows
    try:

        if sl_tp == "04" and vsl in skrt_set:
            return None, frag, log_rows
        if sl_tp not in ("04", "05"):
            return None, frag, log_rows

        thorough = True
        info = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx})
        all_lines = info.get("P_RS_D", []) or []




        resub_lines = [l for l in all_lines
                       if l.get("RJT_YN") == "Y" and l.get("STATUS2") == "Y"
                       and (l.get("STATUS_RMK2") or "").strip()
                       and l.get("CFM_YN") != "Y"
                       and _is_amount_reject(l.get("RJT_RMK"))]
        total_reject = sum(1 for l in all_lines if l.get("RJT_YN") == "Y")
        unresub_reject = total_reject - len(resub_lines)

        lines = [l for l in all_lines if l.get("CFM_YN") != "Y" and l.get("RJT_YN") != "Y"] + resub_lines
        prefetch_line_pdfs(sx, lines)


        already_cfm_seqs = [l.get("SX_SEQ") for l in all_lines
                            if l.get("CFM_YN") == "Y" and l.get("RJT_YN") != "Y"
                            and l.get("SX_SEQ") is not None]
        sxr = {"sx": sx, "vsl": vsl, "dept": sl_tp, "owner": owner.get(vsl),
               "pending": len(lines), "confirm": 0, "flag": 0, "scan": 0, "err": 0,
               "reject": unresub_reject, "resub": len(resub_lines), "resub_ok": 0,
               "confirm_seqs": [], "already_cfm_seqs": already_cfm_seqs,
               "crew_only": (sl_tp == "04")}
        for l in lines:
            seq = l.get("SX_SEQ")
            is09 = (l.get("EXP_CD") or "").startswith("09")
            soa_tp = l.get("SOA_TP"); opex_tp = l.get("SOA_OPEX_TP")
            rec = {"sx": sx, "seq": seq, "vsl": vsl, "dept": sl_tp,
                   "exp": l.get("EXP_NM"), "amt": l.get("SOA_AMT"), "cur": l.get("SOA_CUR_CD"),
                   "usd": l.get("AMT_USD"), "is09": is09}
            try:
                if soa_tp == "A":
                    decision, reason = "confirm", "Asset"
                elif sl_tp == "04":
                    if opex_tp == "A":
                        decision, reason, subj = verify_aor(sx, seq, vsl, l)
                        rec["aor_subj"] = subj
                        if not subj: frag["aor_nodoc"].append(rec)
                    else:

                        decision, reason = "confirm", "Crew OPEX 무조건확인(룰)"
                elif opex_tp == "A":
                    decision, reason, subj = verify_aor(sx, seq, vsl, l)
                    rec["aor_subj"] = subj
                    if not subj: frag["aor_nodoc"].append(rec)
                else:
                    decision, reason = verify_opex(sx, seq, vsl, l, thorough)
            except Exception as e:
                frag["err"].append({**rec, "reason": str(e)[:120]}); sxr["err"] += 1; continue
            rec["reason"] = reason
            is_resub = (l.get("RJT_YN") == "Y")
            if is_resub:
                rec["resub"] = True
            if is09 and decision == "confirm":
                frag["code09"].append(rec)
            if decision == "confirm":
                bucket = "crew_confirm" if sl_tp == "04" else "confirm"
                frag[bucket].append(rec); sxr["confirm"] += 1; sxr["confirm_seqs"].append(seq)
                if is_resub:
                    sxr["resub_ok"] += 1
                log_rows.append([sx, seq, "RESUB-OK(dry)" if is_resub else "CONFIRM(dry)", reason])
            elif decision == "scan":
                frag["scan"].append(rec); sxr["scan"] += 1
                log_rows.append([sx, seq, "SCAN", reason])
            else:
                frag["flag"].append(rec); sxr["flag"] += 1
                log_rows.append([sx, seq, "FLAG", reason])







        has_confirmed = (len(sxr["confirm_seqs"]) >= 1) or (len(already_cfm_seqs) >= 1)
        sxr["approvable"] = (sxr["flag"] == 0 and sxr["scan"] == 0 and sxr["err"] == 0
                             and unresub_reject == 0 and has_confirmed)
        return sxr, frag, log_rows
    except Exception as e:
        rec = {"sx": sx, "vsl": vsl, "dept": sl_tp, "reason": str(e)[:120]}
        frag["err"].append(rec)
        sxr = {"sx": sx, "vsl": vsl, "dept": sl_tp, "owner": owner.get(vsl),
               "pending": 0, "confirm": 0, "flag": 0, "scan": 0, "err": 1,
               "reject": 0, "resub": 0, "resub_ok": 0, "confirm_seqs": [],
               "already_cfm_seqs": [], "crew_only": (sl_tp == "04"), "approvable": False}
        log_rows.append([sx, "", "ERR", str(e)[:120]])
        return sxr, frag, log_rows

def review(group):
    login()
    with _PREFETCH_STATS_LOCK:
        for k in _PREFETCH_STATS:
            _PREFETCH_STATS[k] = 0.0 if k == "seconds" else 0
    owner = my_vessels()
    if not owner:
        raise RuntimeError("My Vessel 조회 실패(빈 결과) — SVMS 오류 의심, SOA 검토 중단")
    targets = resolve_targets(group, owner)
    skrt_set = {v for v, o in owner.items() if o == "001"}
    silver_set = {v for v, o in owner.items() if o == "037"}
    part_union = explicit_union("037")

    drift = uncovered_vessels(owner)
    partition_missing = sorted(v for v in part_union if v not in silver_set)
    new_skrt = sorted(v for v in skrt_set if v not in KNOWN_VESSELS)
    thorough_group = SG.is_thorough_group(group, groups_cfg())

    hdr = select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                 {"VSL_CD": "M", "FM_DM": "202601", "TO_DM": "202612", "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
    rows = [r for r in (hdr.get("P_RS", []) or []) if (r.get("SX_CD") or "")[:4] in targets and r.get("STATUS") == "S"]



    out = {"group": group, "drift": drift, "partition_missing": partition_missing,
           "new_skrt": new_skrt, "targets": sorted(targets),
           "skrt_in_group": sorted(v for v in targets if v in skrt_set),
           "confirm": [], "flag": [], "scan": [],
           "aor_nodoc": [], "code09": [], "crew_confirm": [], "oper_notify": [], "err": [], "soas": []}
    logf = open(LOG, "a", newline=""); w = csv.writer(logf)
    run_id = str(int(time.time()))

    jobs = []
    for r in rows:
        sx = r.get("SX_CD") or ""
        vsl = sx[:4]
        sl_tp = r.get("SL_TP")
        if sl_tp == "04" and vsl in skrt_set:
            continue
        if sl_tp not in ("04", "05"):
            continue
        jobs.append(r)
    try:
        parallel = max(1, min(3, int(os.environ.get("SOA_REVIEW_PARALLEL", "3"))))
    except (TypeError, ValueError):
        parallel = 3

    if parallel > 1 and len(jobs) > 1:
        out["parallel_review"] = {"workers": parallel, "jobs": len(jobs)}
        with ThreadPoolExecutor(max_workers=min(parallel, len(jobs))) as ex:
            results = list(ex.map(lambda rr: _review_soa_row(rr, group, owner, skrt_set, thorough_group), jobs))
    else:
        out["parallel_review"] = {"workers": 1, "jobs": len(jobs)}
        results = [_review_soa_row(r, group, owner, skrt_set, thorough_group) for r in jobs]

    for sxr, frag, log_rows in results:
        _merge_frag(out, frag)
        if sxr:
            out["soas"].append(sxr)
        for sx, seq, action, reason in log_rows:
            w.writerow([run_id, group, sx, seq, action, reason])
        logf.flush()

    if not thorough_group:


        opr = select_opt("PKG_CO_SLIP_EX", "SP_GET_SOA_OPR",
                         {"VSL_CD": "M", "FM_DM": "202601", "TO_DM": "202612", "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
        for r in ((opr or {}).get("P_RS", []) or []):
            if (r.get("SX_CD") or "")[:4] in targets and r.get("STATUS") == "S":
                out["oper_notify"].append({"sx": r.get("SX_CD"), "subj": (r.get("SUBJ") or "")[:40]})
    logf.close()
    with _PREFETCH_STATS_LOCK:
        out["prefetch"] = dict(_PREFETCH_STATS)
    return out

def render(o):
    print(f"=== SOA 정기검토 [{o['group']}] 검토(read-only) ===")
    if o.get("new_skrt"):
        print(f"🆕 신규 장금선(SKRT) 자동 편입·자동승인됨 — 형 확인 요망: {o['new_skrt']}")




    if o.get("partition_missing"):
        print(f"⚠️ 파티션 선박이 My Vessel서 사라짐(확인 필요): {o['partition_missing']}")
    print(f"SOA {len(o['soas'])}건 | ✅확인 Tech {len(o['confirm'])} + Crew {len(o['crew_confirm'])} "
          f"/ ✋flag {len(o['flag'])} / 📷scan(vision) {len(o['scan'])} / 🚨err {len(o['err'])}")
    pr = o.get("parallel_review") or {}
    if pr.get("jobs"):
        print(f"SOA 병렬검토(read-only): workers {pr.get('workers',1)} · jobs {pr.get('jobs',0)}")
    pf = o.get("prefetch") or {}
    if pf.get("files"):
        print(f"첨부 prefetch: 파일 {pf.get('files')}개 · cache {pf.get('hit',0)} · 신규다운 {pf.get('downloaded',0)} "
              f"· 실패 {pf.get('failed',0)} · {pf.get('seconds',0):.2f}s")
    for k, lab in [("flag", "✋ 사람확인(금액 미확인)"), ("scan", "📷 스캔본 vision 필요"),
                   ("aor_nodoc", "🔗 AOR doc 못찾음(RMK 유지)"), ("err", "🚨 에러")]:
        if o[k]:
            print(f"\n--- {lab} ({len(o[k])}) ---")
            for r in o[k][:40]:
                print(f"  · {r['sx']} s{r.get('seq')} [{r.get('cur')}{r.get('amt')}] {r.get('exp','')} — {r.get('reason','')}")
    if o["code09"]:
        print(f"\n--- 🔔 09코드(Dry Dock/수리정산) {len(o['code09'])}건 통지 ---")
        for r in sorted(o["code09"], key=lambda x: -(x.get("usd") or 0))[:20]:
            print(f"  · {r['sx']} s{r['seq']} {r.get('exp','')} USD={r.get('usd')}")
    if o["oper_notify"]:
        print(f"\n--- 📋 Operational Submitted {len(o['oper_notify'])}건 (담당 감독 직접검토) ---")
        for r in o["oper_notify"]: print(f"  · {r['sx']} {r['subj']}")
    print(f"\n⚠️ Approval(파란버튼)·Email 미실행 — 담당 감독 아침 컨펌.")

if __name__ == "__main__":
    g = sys.argv[1] if len(sys.argv) > 1 else None
    _gk = group_keys()
    if g not in _gk:
        print(f"usage: [CONFIRM=1] python3 soa_review.py {'|'.join(_gk)}"); sys.exit(1)
    o = review(g)
    if "--json" in sys.argv:
        print(json.dumps(o, ensure_ascii=False, indent=1))
    else:
        render(o)
