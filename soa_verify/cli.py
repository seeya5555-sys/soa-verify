#!/usr/bin/env python3
"""soa-verify — SVMS SOA stage-1 verification (amount vs invoice) for your own My Vessel list.

  python3 -m soa_verify setup                         # store your SVMS id/password locally
  python3 -m soa_verify owners                        # list owners (OW_COMP_ID) and vessels in your My Vessel
  python3 -m soa_verify verify --owner 037            # DRY run (no write) for all vessels of an owner
  python3 -m soa_verify verify --vessel ABCD,EFGH     # DRY run for specific vessels
  python3 -m soa_verify verify --all --save           # write line checkbox / reject remark to SVMS

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


def my_vessels():
    """{vsl_cd: {"owner": OW_COMP_ID, "name": .., "imo": ..}} from the logged-in user's My Vessel."""
    u = svms.select("PKG_CM_USER", "SP_GET_USER_INFO", {"USER_ID": svms.USER_ID})
    out = {}
    for r in (u.get("P_RS_VSL") or []):
        v = r.get("VSL_CD")
        if not v:
            continue
        d = (svms.select("PKG_CM_CD", "SP_GET_VSL", {"VSL_CD": v}).get("P_RS") or [{}])[0]
        out[v] = {"owner": d.get("OW_COMP_ID"), "name": (d.get("NAME") or "").strip(),
                  "imo": str(d.get("IMO_NO") or "").strip()}
    return out


def _write_roster(vessels):
    # Used by the engine's direct-skip gate to detect other fleet vessels on shared invoices.
    os.makedirs(CACHE, exist_ok=True)
    rows = [{"vsl_cd": k, "name": v["name"], "imo": v["imo"]} for k, v in vessels.items()]
    with open(os.path.join(CACHE, "roster.json"), "w") as f:
        json.dump({"vessels": rows}, f)


def cmd_owners(_a):
    svms.login()
    vs = my_vessels()
    by = {}
    for k, v in sorted(vs.items()):
        by.setdefault(v["owner"] or "?", []).append(f"{k} ({v['name']})")
    for o, lst in sorted(by.items()):
        print(f"owner {o}: {len(lst)} vessel(s)")
        for x in lst:
            print(f"   - {x}")


def _remark_ok(res):
    # strict: only a dict response without error counts as saved
    return isinstance(res, dict) and bool(res) and not res.get("error") \
        and not (res.get("data") is None and res.get("message"))


def cmd_verify(a):
    svms.login()
    vs = my_vessels()
    if not vs:
        raise SystemExit("My Vessel lookup returned nothing — stopping (fail-closed).")
    _write_roster(vs)
    os.environ.setdefault("SOA_DIRECT_SKIP", "1")
    os.environ.setdefault("SOA_PREFETCH", "1")
    import soa_review as R  # engine (imported after roster is written)
    for k, v in vs.items():
        R.VSL_IDENT[k] = [t for t in (v["imo"], v["name"]) if t]

    if a.vessel:
        targets = [x.strip().upper() for x in a.vessel.split(",") if x.strip()]
        bad = [t for t in targets if t not in vs]
        if bad:
            raise SystemExit(f"not in your My Vessel: {', '.join(bad)}")
    elif a.owner:
        owners = {x.strip() for x in a.owner.split(",") if x.strip()}
        targets = sorted(k for k, v in vs.items() if v["owner"] in owners)
    elif a.all:
        targets = sorted(vs)
    else:
        raise SystemExit("choose --vessel, --owner or --all (see `owners`)")
    if not targets:
        raise SystemExit("no vessels matched")

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
    vs = my_vessels()
    if not vs:
        raise SystemExit("My Vessel lookup returned nothing — stopping (fail-closed).")
    targets = [x.strip().upper() for x in (a.vessel or "").split(",") if x.strip()]
    bad = [t for t in targets if t not in vs]
    if not targets or bad:
        raise SystemExit(f"choose --vessel from your My Vessel {('(not yours: ' + ', '.join(bad) + ')') if bad else ''}")
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
    svms.login()
    vs = my_vessels()
    if not vs:
        raise SystemExit("My Vessel lookup returned nothing — stopping (fail-closed).")
    targets = [x.strip().upper() for x in (a.vessel or "").split(",") if x.strip()]
    bad = [t for t in targets if t not in vs]
    if not targets or bad:
        raise SystemExit(f"choose --vessel from your My Vessel {('(not yours: ' + ', '.join(bad) + ')') if bad else ''}")
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="soa_verify")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("owners").set_defaults(fn=cmd_owners)
    v = sub.add_parser("verify")
    g = v.add_mutually_exclusive_group()
    g.add_argument("--vessel"); g.add_argument("--owner"); g.add_argument("--all", action="store_true")
    v.add_argument("--from", dest="fm", default=os.environ.get("FM_DM", "202601"))
    v.add_argument("--to", dest="to", default=os.environ.get("TO_DM", "202612"))
    v.add_argument("--dept", default="05", choices=["05", "04"], help="05 Technical (default) / 04 Crew")
    v.add_argument("--save", action="store_true", help="write checkbox / reject remark to SVMS")
    v.add_argument("--cap", type=int, default=300, help="max line writes per run")
    v.set_defaults(fn=cmd_verify)
    c = sub.add_parser("cleanup", help="remove leftover reject remarks on confirmed lines")
    c.add_argument("--vessel", required=True)
    c.add_argument("--from", dest="fm", default=os.environ.get("FM_DM", "202601"))
    c.add_argument("--to", dest="to", default=os.environ.get("TO_DM", "202612"))
    c.add_argument("--save", action="store_true")
    c.set_defaults(fn=cmd_cleanup)
    s2 = sub.add_parser("stage2", help="stage 2: Operational AOR, FX >=120%%, mgmt fee vs SMA, NRC invoice")
    s2.add_argument("--vessel", required=True)
    s2.add_argument("--from", dest="fm", default=os.environ.get("FM_DM", "202601"))
    s2.add_argument("--to", dest="to", default=os.environ.get("TO_DM", "202612"))
    s2.add_argument("--dept", default="05", choices=["05", "04"])
    s2.add_argument("--save", action="store_true", help="write reject checkbox + remark to SVMS")
    s2.add_argument("--cap", type=int, default=300)
    s2.set_defaults(fn=cmd_stage2)
    a = p.parse_args(argv)
    for k in ("fm", "to"):
        if hasattr(a, k) and not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", getattr(a, k)):
            raise SystemExit(f"--{k.replace('fm', 'from')} must be YYYYMM")
    a.fn(a)


if __name__ == "__main__":
    main()
