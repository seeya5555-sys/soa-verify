#!/usr/bin/env python3
"""soa-verify — SVMS SOA verification (stage 1: amount vs invoice, stage 2: extra checks) by vessel code.

  python3 -m soa_verify setup                         # store your SVMS id/password locally
  python3 -m soa_verify doctor                        # environment check (writes nothing)
  python3 -m soa_verify verify --vessel ABCD,EFGH     # DRY run for the given vessel codes
  python3 -m soa_verify verify --vessel ABCD --save   # write line checkbox / reject remark to SVMS

Vessels are always named explicitly with --vessel. Nothing is selected from the user's "My Vessel" list.
Scope (by design): line CONFIRM checkbox + REJECT checkbox with remark, saved to SVMS.
There is no SOA approval, no slip submission, no reject submission and no e-mail in this project.
"""
import argparse, getpass, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CACHE = os.path.join(ROOT, "_cache")
sys.path.insert(0, HERE)

import svms  # noqa: E402  (config-driven credentials)


def _write_config(user, pw):
    os.makedirs(os.path.dirname(svms.CONFIG_PATH), exist_ok=True)
    cfg = {}
    try:
        with open(svms.CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.update({"svms_user": user, "svms_pass": pw})
    fd = os.open(svms.CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(svms.CONFIG_PATH, 0o600)   # tighten even if the file already existed with wider permissions
    print(f"saved {svms.CONFIG_PATH} (permission 600). Password is never printed or uploaded.")


def fleet():
    """{vsl_cd: {"owner": OW_COMP_ID, "name": .., "imo": ..}} — SVMS vessel master (one read-only call).
    Used to validate the vessel codes you typed and to recognise other vessels' names/IMO on shared invoices."""
    out = {}
    for d in (svms.select("PKG_CM_CD", "SP_GET_VSL", {"VSL_CD": ""}).get("P_RS") or []):
        v = str(d.get("CODE") or d.get("VSL_CD") or "").strip().upper()
        if v:
            out[v] = {"owner": d.get("OW_COMP_ID"), "name": (d.get("NAME") or "").strip(),
                      "imo": str(d.get("IMO_NO") or "").strip()}
    return out


def _targets(arg, vs):
    """--vessel 'ABCD,EFGH' → validated codes. Unknown code = stop (typo guard). No other vessel is ever touched."""
    if not vs:
        raise SystemExit("SVMS vessel master lookup returned nothing — stopping (fail-closed).")
    targets = [x.strip().upper() for x in (arg or "").split(",") if x.strip()]
    bad = [t for t in targets if t not in vs]
    if not targets or bad:
        raise SystemExit("give vessel code(s) with --vessel, e.g. --vessel ABCD,EFGH"
                         + (f" (unknown code: {', '.join(bad)})" if bad else ""))
    return list(dict.fromkeys(targets))


def _write_roster(vessels):
    # Used by the engine's direct-skip gate to detect other fleet vessels on shared invoices.
    os.makedirs(CACHE, exist_ok=True)
    rows = [{"vsl_cd": k, "name": v["name"], "imo": v["imo"]} for k, v in vessels.items()]
    with open(os.path.join(CACHE, "roster.json"), "w") as f:
        json.dump({"vessels": rows}, f)


TOOLS = {   # 판정에 필요한 외부 프로그램. 없으면 본문을 못 읽어 정상 라인이 전부 리젝 후보로 나오므로 실행 전에 막는다.
    "pdftotext": "poppler (macOS: brew install poppler / Ubuntu: apt install poppler-utils)",
    "pdftoppm": "poppler (macOS: brew install poppler / Ubuntu: apt install poppler-utils)",
    "tesseract": "tesseract (macOS: brew install tesseract / Ubuntu: apt install tesseract-ocr)",
    "claude": "Claude Code CLI — install, then run `claude` once and log in",
}


def missing_tools(need=("pdftotext", "pdftoppm", "tesseract", "claude")):
    import shutil
    return [t for t in need if not shutil.which(t)]


def preflight(need):
    """Stop before any SVMS call when a required program is missing (SOA_VERIFY_SKIP_PREFLIGHT=1 to bypass, tests only)."""
    if os.environ.get("SOA_VERIFY_SKIP_PREFLIGHT") == "1":
        return
    miss = missing_tools(need)
    if miss:
        lines = [f"  - {t}: {TOOLS[t]}" for t in miss]
        raise SystemExit("required program(s) not found on PATH — nothing was run:\n" + "\n".join(lines)
                         + "\nRun `python3 -m soa_verify doctor` to re-check.")


def cmd_doctor(_a):
    """Environment check: Python, external programs, credentials, SVMS login. Writes nothing to SVMS."""
    ok = True
    v = sys.version_info
    good = v >= (3, 10); ok &= good
    print(f"[{'ok' if good else 'NG'}] Python {v.major}.{v.minor} (need 3.10+)")
    miss = set(missing_tools())
    for t, how in TOOLS.items():
        ok &= t not in miss
        print(f"[{'NG' if t in miss else 'ok'}] {t}" + (f" — install: {how}" if t in miss else ""))
    has_cred = bool(svms.USER_ID and (os.environ.get("SVMS_PASS") or svms._C.get("svms_pass")))
    ok &= has_cred
    print(f"[{'ok' if has_cred else 'NG'}] SVMS credentials" + ("" if has_cred else " — run `python3 -m soa_verify setup`"))
    if has_cred:
        try:
            svms.login()
            n = len(fleet())
            ok &= n > 0
            print(f"[{'ok' if n else 'NG'}] SVMS login · vessel master {n} vessel(s)")
        except (Exception, SystemExit) as e:
            ok = False
            print(f"[NG] SVMS login — {str(e)[:100]}")
    print("=== ready ===" if ok else "=== not ready — fix the NG items above ===")
    if not ok:
        raise SystemExit(1)


def _remark_ok(res):
    # strict: only a dict response without error counts as saved
    return isinstance(res, dict) and bool(res) and not res.get("error") \
        and not (res.get("data") is None and res.get("message"))


def cmd_verify(a):
    preflight(("pdftotext", "pdftoppm", "tesseract", "claude"))
    svms.login()
    vs = fleet()
    targets = _targets(a.vessel, vs)
    _write_roster(vs)
    os.environ.setdefault("SOA_DIRECT_SKIP", "1")
    os.environ.setdefault("SOA_PREFETCH", "1")
    import soa_review as R  # engine (imported after roster is written)
    for k in targets:
        R.VSL_IDENT[k] = [t for t in (vs[k]["imo"], vs[k]["name"]) if t]

    mode = "SAVE (writes checkbox / reject remark to SVMS)" if a.save else "DRY (no write)"
    print(f"=== soa-verify stage 1 · {mode} · dept {a.dept} · {a.fm}~{a.to} · model {__import__('llm_router').review_model()} ===")
    tot = {"confirm": 0, "flag": 0, "scan": 0, "w_cfm": 0, "w_rjt": 0}
    writes = 0
    for vsl in targets:
        owner = vs[vsl]["owner"]
        if a.dept == "04" and owner == "001":
            print(f"[{vsl}] crew SOA of owner 001 is excluded (same as upstream rule)"); continue
        hdr = svms.select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                          {"VSL_CD": vsl, "FM_DM": a.fm, "TO_DM": a.to, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
        rows = [r for r in (hdr.get("P_RS") or [])
                if (r.get("SX_CD") or "")[:4] == vsl and r.get("SL_TP") == a.dept
                and r.get("STATUS") == "S" and a.fm <= (r.get("SL_DM") or "") <= a.to]
        print(f"\n[{vsl}] {vs[vsl]['name']} · owner {owner} · submitted SOA {len(rows)}")
        for r in sorted(rows, key=lambda x: x.get("SL_DM") or ""):
            sx = r.get("SX_CD")
            all_lines = svms.select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D") or []
            resub = [l for l in all_lines
                     if l.get("RJT_YN") == "Y" and l.get("STATUS2") == "Y"
                     and (l.get("STATUS_RMK2") or "").strip() and l.get("CFM_YN") != "Y"
                     and R._is_amount_reject(l.get("RJT_RMK"))]
            pending = [l for l in all_lines if l.get("CFM_YN") != "Y" and l.get("RJT_YN") != "Y"] + resub
            print(f"  ── {sx} ({r.get('SL_DM')}) lines {len(all_lines)} · to check {len(pending)}")
            R.prefetch_line_pdfs(sx, pending)
            cfm, flags = [], []
            for l in pending:
                seq = l.get("SX_SEQ")
                try:
                    if l.get("SOA_TP") == "A":
                        d, why = "confirm", "Asset"
                    elif a.dept == "04" and l.get("SOA_OPEX_TP") != "A" and owner != "037":
                        d, why = "scan", "Crew OPEX of this owner — human review"
                    elif l.get("SOA_OPEX_TP") == "A":
                        d, why, _s = R.verify_aor(sx, seq, vsl, l)
                    else:
                        d, why = R.verify_opex(sx, seq, vsl, l, True)
                except Exception as e:
                    d, why = "flag", f"error: {str(e)[:80]}"
                tot[d] += 1
                mark = {"confirm": "CONFIRM", "flag": "REJECT?", "scan": "HUMAN"}[d]
                print(f"     s{seq:<4} [{l.get('SOA_CUR_CD')} {l.get('SOA_AMT')}] {(l.get('EXP_NM') or '')[:30]:<30} → {mark} ({why[:60]})")
                if d == "confirm":
                    cfm.append(seq)
                elif d == "flag":
                    flags.append((seq, why, l.get("RJT_YN") == "Y"))
            if not a.save:
                continue
            for seq in cfm:
                if writes >= a.cap:
                    print(f"     ⏸ write cap {a.cap} reached"); break
                res = R.confirm_line(sx, seq); writes += 1
                if _remark_ok(res): tot["w_cfm"] += 1
                else: print(f"     ! confirm failed s{seq}: {str(res)[:60]}")
            for seq, why, is_resub in flags:
                if writes >= a.cap:
                    print(f"     ⏸ write cap {a.cap} reached"); break
                if is_resub:
                    print(f"     ↩ s{seq} resubmitted by manager — left as is"); continue
                if not R._is_amount_reject(why):
                    print(f"     ↩ s{seq} non-amount reason, report only: {why[:50]}"); continue
                line = next((x for x in pending if x.get("SX_SEQ") == seq), {})
                res = svms.save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS",
                                {"SX_CD": sx, "SX_SEQ": seq, "CFM_YN": "N", "RJT_YN": "Y",
                                 "RJT_RMK": R.reject_remark(sx, seq, line, why), "STATUS2": "N", "STATUS_RMK2": None})
                writes += 1
                if _remark_ok(res): tot["w_rjt"] += 1
                else: print(f"     ! reject mark failed s{seq}: {str(res)[:60]}")
    print(f"\n=== total: confirm {tot['confirm']} · reject candidates {tot['flag']} · human review {tot['scan']} ===")
    if a.save:
        print(f"    saved: confirm checkbox {tot['w_cfm']} · reject remark {tot['w_rjt']}. Approval is left to you in SVMS.")
    else:
        print("    DRY run — nothing written. Re-run with --save to write.")


def cmd_cleanup(a):
    """리마크정리: confirm(CFM_YN=Y)된 라인에 남은 리젝 리마크만 삭제. 상태·금액·승인 무변경."""
    svms.login()
    targets = _targets(a.vessel, fleet())
    n = 0
    for vsl in targets:
        hdr = svms.select("PKG_CO_SLIP_EX", "SP_GET_SOA",
                          {"VSL_CD": vsl, "FM_DM": a.fm, "TO_DM": a.to, "SOA_OW_COMP_ID": "", "VSL_STATUS": "1"})
        for r in (hdr.get("P_RS") or []):
            if (r.get("SX_CD") or "")[:4] != vsl or r.get("STATUS") not in ("S", "D"):
                continue
            sx = r.get("SX_CD")
            for l in svms.select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx}).get("P_RS_D") or []:
                # upstream soa_remark_cleanup.clear_rmk 와 동일: 상태가 정확히 CFM=Y·RJT=N 인 라인만,
                # S/M 필드(STATUS2/STATUS_RMK2)는 현재값 그대로 echo → RJT_RMK 외 무변경.
                if l.get("CFM_YN") == "Y" and l.get("RJT_YN") == "N" and (l.get("RJT_RMK") or "").strip():
                    if a.save:
                        res = svms.save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS",
                                        {"SX_CD": sx, "SX_SEQ": l.get("SX_SEQ"), "CFM_YN": "Y", "RJT_YN": "N",
                                         "RJT_RMK": None, "STATUS2": l.get("STATUS2"), "STATUS_RMK2": l.get("STATUS_RMK2")})
                        ok = _remark_ok(res)
                    else:
                        ok = True
                    n += ok
                    print(f"  {'cleared' if a.save else 'would clear'} {sx} s{l.get('SX_SEQ')}: {(l.get('RJT_RMK') or '')[:60]}")
    print(f"=== remark cleanup: {n} line(s) {'cleared' if a.save else '(DRY — add --save)'} ===")


def cmd_stage2(a):
    """2차 검증: Operational AOR · 외화 환율(≥120%) · 관리비 SMA · NRC → 리젝 체크 + 리마크. 승인·제출·메일 없음."""
    import soa_stage2 as S2
    preflight(("pdftotext", "pdftoppm", "tesseract"))   # NRC 인보이스 판독용(2차는 AI 호출 없음)
    svms.login()
    targets = _targets(a.vessel, fleet())
    S2.VERIFY, S2.SL_TP, S2.FM_DM, S2.TO_DM, S2.CAP = bool(a.save), a.dept, a.fm, a.to, a.cap
    rc = 0
    for vsl in targets:
        rc = max(rc, S2.run(vsl) or 0)
    if rc:
        raise SystemExit(rc)


def cmd_setup(_a):
    user = input("SVMS user id: ").strip()
    pw = getpass.getpass("SVMS password: ")
    if not user or not pw:
        raise SystemExit("cancelled")
    _write_config(user, pw)


def _default_period():
    """Default period = current calendar year (YYYY01 ~ YYYY12), overridable by env FM_DM / TO_DM."""
    import datetime
    y = datetime.date.today().year
    return os.environ.get("FM_DM", f"{y}01"), os.environ.get("TO_DM", f"{y}12")


def build_parser():
    fm0, to0 = _default_period()
    p = argparse.ArgumentParser(prog="soa_verify")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("doctor", help="check Python, external programs, credentials and SVMS login").set_defaults(fn=cmd_doctor)
    v = sub.add_parser("verify")
    v.add_argument("--vessel", required=True, help="vessel code(s), comma separated")
    v.add_argument("--from", dest="fm", default=fm0)
    v.add_argument("--to", dest="to", default=to0)
    v.add_argument("--dept", default="05", choices=["05", "04"], help="05 Technical (default) / 04 Crew")
    v.add_argument("--save", action="store_true", help="write checkbox / reject remark to SVMS")
    v.add_argument("--cap", type=int, default=300, help="max line writes per run")
    v.set_defaults(fn=cmd_verify)
    c = sub.add_parser("cleanup", help="remove leftover reject remarks on confirmed lines")
    c.add_argument("--vessel", required=True)
    c.add_argument("--from", dest="fm", default=fm0)
    c.add_argument("--to", dest="to", default=to0)
    c.add_argument("--save", action="store_true")
    c.set_defaults(fn=cmd_cleanup)
    s2 = sub.add_parser("stage2", help="stage 2: Operational AOR, FX >=120%%, mgmt fee vs SMA, NRC invoice")
    s2.add_argument("--vessel", required=True)
    s2.add_argument("--from", dest="fm", default=fm0)
    s2.add_argument("--to", dest="to", default=to0)
    s2.add_argument("--dept", default="05", choices=["05", "04"])
    s2.add_argument("--save", action="store_true", help="write reject checkbox + remark to SVMS")
    s2.add_argument("--cap", type=int, default=300)
    s2.set_defaults(fn=cmd_stage2)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    for k in ("fm", "to"):
        if hasattr(a, k) and not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", getattr(a, k)):
            raise SystemExit(f"--{k.replace('fm', 'from')} must be YYYYMM")
    if hasattr(a, "fm") and a.fm > a.to:
        raise SystemExit("--from must not be later than --to")
    a.fn(a)


if __name__ == "__main__":
    main()
