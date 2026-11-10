import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "soa_verify"))
import soa_stage2 as S2

AORS = {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"},
        "ATMTCA2509250007": {"AOR_CD": "ATMTCA2509250007", "DEPT_CD": "T"}}


def lk(a):
    return AORS.get(a)


def boom(a):
    raise SystemExit("svms 500")


class T(unittest.TestCase):
    def test_non_aor_skipped(self):
        self.assertEqual(S2.judge_line({"REF_NO": "ATMTCO2601210002"}, lk, "05")[0], "skip")
        self.assertEqual(S2.judge_line({"REF_NO": None}, lk, "05")[0], "skip")

    def test_operational_rejected(self):
        v, a, _, _r = S2.judge_line({"REF_NO": "ABCDCA2609010001", "CFM_YN": "Y"}, lk, "05")
        self.assertEqual((v, a), ("reject", "ABCDCA2609010001"))

    def test_technical_passes(self):
        self.assertEqual(S2.judge_line({"REF_NO": "ATMTCA2509250007"}, lk, "05")[0], "pass")

    def test_lookup_fail_closed(self):
        self.assertEqual(S2.judge_line({"REF_NO": "ABCDCA2609010001"}, boom, "05")[0], "error")
        self.assertEqual(S2.judge_line({"REF_NO": "XXXXCA2609010001"}, lk, "05")[0], "error")

    def test_existing_reject_not_overwritten(self):
        l = {"REF_NO": "ABCDCA2609010001", "RJT_YN": "Y", "RJT_RMK": "Cost no match - Amount: SOA USD 1 / Invoice 2"}
        v, _, _, r = S2.judge_line(l, lk, "05")                       # 1차 엔진 리마크 → 덧붙임
        self.assertEqual(v, "reject")
        self.assertTrue(r.startswith("Cost no match - Amount: SOA USD 1 / Invoice 2 | Operational AOR: ABCDCA2609010001"))
        self.assertEqual(S2.judge_line({**l, "RJT_RMK": r}, lk, "05")[0], "already")   # 같은 사유 재기입 안 함
        self.assertEqual(S2.judge_line({**l, "RJT_RMK": "pls check with master"}, lk, "05")[0], "already")  # 사람 리마크 보존
        l2 = {**l, "STATUS2": "Y", "STATUS_RMK2": "fixed"}
        self.assertEqual(S2.judge_line(l2, lk, "05")[0], "resub")

    def test_preserve_unrejected_with_remark(self):
        base = {"REF_NO": "ABCDCA2609010001", "RJT_YN": "N"}
        self.assertEqual(S2.judge_line({**base, "RJT_RMK": "old"}, lk, "05")[0], "already")
        self.assertEqual(S2.judge_line({**base, "STATUS2": "Y"}, lk, "05")[0], "resub")
        self.assertEqual(S2.judge_line({**base, "STATUS_RMK2": "mgr"}, lk, "05")[0], "resub")

    def _env(self, lines, aors, fail_after_write=False):
        st = {"lines": {l["SX_SEQ"]: dict(l) for l in lines}, "saves": [], "hdr": "S"}
        def select(pkg, sp, prm):
            if sp == "SP_GET_SOA":
                return {"P_RS": [{"SX_CD": "TEST-SOA-0001", "SL_TP": "05", "STATUS": st["hdr"], "SL_DM": "202609"}]}
            if sp == "SP_GET_SOA_INFO":
                return {"P_RS_D": [dict(v) for v in st["lines"].values()]}
            if sp == "SP_GET_VSL":
                return {"P_RS": [{"NAME": "SILVER MILLIE"}]}
            if sp == "SP_GET_AOR":
                a = aors.get(prm["AOR_CD"])
                if a == "boom": raise SystemExit("500")
                return {"P_RS": [a] if a else []}
        def save(pkg, sp, prm):
            st["saves"].append(prm)
            if not fail_after_write:
                st["lines"][prm["SX_SEQ"]].update(RJT_YN="Y", RJT_RMK=prm["RJT_RMK"], CFM_YN="N",
                                                   STATUS2=prm["STATUS2"], STATUS_RMK2=prm["STATUS_RMK2"])
            return None
        orig = (S2.select, S2.save, S2.login, S2.make_fx_lookup)
        def restore():
            S2.select, S2.save, S2.login, S2.make_fx_lookup = orig
        self.addCleanup(restore)
        S2.select, S2.save, S2.login = select, save, lambda: None
        S2.make_fx_lookup = lambda dt=None: (lambda c: None)
        S2.NRC_SCAN = False
        S2.VERIFY, S2.SL_TP, S2.FM_DM, S2.TO_DM, S2.CAP = True, "05", "202601", "202612", 300
        return st

    def test_preflight_error_blocks_all_writes(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}, {"SX_SEQ": 2, "REF_NO": "ABCDCA2609010002"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}, "ABCDCA2609010002": "boom"})
        self.assertEqual(S2.run("TEST"), 1); self.assertEqual(st["saves"], [])

    def test_write_verified_by_reread(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}})
        self.assertEqual(S2.run("TEST"), 0); self.assertEqual(len(st["saves"]), 1)
        self.assertEqual(st["lines"][1]["RJT_YN"], "Y")

    def test_unconfirmed_write_stops(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}, {"SX_SEQ": 2, "REF_NO": "ABCDCA2609010001"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}}, fail_after_write=True)
        self.assertEqual(S2.run("TEST"), 1); self.assertEqual(len(st["saves"]), 1)

    def test_header_changed_skips(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}})
        orig = S2._soa_now
        def flip(*a, **k):
            st["hdr"] = "C"; return orig(*a, **k)
        S2._soa_now = flip
        try:
            S2.run("TEST")
        finally:
            S2._soa_now = orig
        self.assertEqual(st["saves"], [])

    FX = staticmethod(lambda c: {"EUR": (1.1222, "SVMS"), "TRY": (0.0199, "Hana")}.get(c))

    def test_fx_usd_skipped(self):
        l = {"SOA_CUR_CD": "USD", "SOA_AMT": 100, "AMT_USD": 500}
        self.assertEqual(S2.judge_line(l, lk, "05", self.FX)[0], "skip")
        l2 = {"SOA_CUR_CD": "usd", "SOA_AMT": 100, "AMT_USD": 500}
        self.assertEqual(S2.judge_line(l2, lk, "05", self.FX)[0], "skip")

    def test_fx_within_and_lower_pass(self):
        ok = {"SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 700.71}      # 형 캡처 3번 줄(+4%)
        self.assertEqual(S2.judge_line(ok, lk, "05", self.FX)[0], "pass")
        low = {"SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 100}
        self.assertEqual(S2.judge_line(low, lk, "05", self.FX)[0], "pass")
        edge = {"SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 600 * 1.1222 * 1.199}
        self.assertEqual(S2.judge_line(edge, lk, "05", self.FX)[0], "pass")

    def test_fx_over_rejected(self):
        l = {"SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 600 * 1.1222 * 1.2}
        v, k, _, r = S2.judge_line(l, lk, "05", self.FX)
        self.assertEqual(v, "reject"); self.assertTrue(r.startswith("FX over-charged: EUR 600.00"))
        self.assertIn("SVMS", r)

    def test_fx_hana_fallback_and_missing(self):
        l = {"SOA_CUR_CD": "TRY", "SOA_AMT": 1000, "AMT_USD": 50}
        v, _, _, r = S2.judge_line(l, lk, "05", self.FX)
        self.assertEqual(v, "reject"); self.assertIn("Hana", r)
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "XYZ", "SOA_AMT": 1, "AMT_USD": 1}, lk, "05", self.FX)[0], "error")

    def test_fx_negative_skipped(self):
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "EUR", "SOA_AMT": -600, "AMT_USD": -9000}, lk, "05", self.FX)[0], "skip")

    def test_both_findings_combined(self):
        l = {"REF_NO": "ABCDCA2609010001", "SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 2000}
        v, _, tags, r = S2.judge_line(l, lk, "05", self.FX)
        self.assertEqual((v, tags), ("reject", "A+B")); self.assertIn(" | FX over-charged", r)
        self.assertTrue(r.startswith("Operational AOR: ABCDCA2609010001"))
        self.assertLessEqual(len(r), 240)

    def test_hana_parser(self):
        html = "<td>미국 USD</td><td>1,360.39</td><td>1.75</td><td>1,313.61</td><td>1.75</td><td>1,350.10</td><td>1,323.90</td><td>0.00</td><td>1,337.00</td><td>5.76</td><td>1.0000</td>" \
               "<td>일본 JPY (100)</td><td>860.79</td><td>1.75</td><td>831.19</td><td>1.75</td><td>854.28</td><td>837.70</td><td>837.00</td><td>845.99</td><td>3.39</td><td>0.6328</td>" \
               "<td>튀르키예 TRY</td><td>0.00</td><td>0.00</td><td>0.00</td><td>0.00</td><td>26.91</td><td>26.33</td><td>0.00</td><td>26.62</td><td>39.2</td><td>0.0199</td>"
        d = S2.parse_hana("기준일 : 2026년11월10일 " + html, "20261110")
        self.assertAlmostEqual(d["JPY"], 8.4599 / 1337.0, 6); self.assertAlmostEqual(d["TRY"], 26.62 / 1337.0, 6)
        self.assertEqual(S2.parse_hana("기준일 : 2026년11월09일 " + html, "20261110"), {})   # 다른 날 = 무효
        self.assertEqual(S2.parse_hana(html, "20261110"), {})                                # 기준일 없음 = 무효
        no_usd = html.replace("미국 USD", "미국 XXX")
        self.assertEqual(S2.parse_hana("기준일 : 2026년11월10일 " + no_usd, "20261110"), {})  # USD 교차기준 없음 = 무효

    def test_fx_exact_boundary(self):
        fx = lambda c: (0.1, "SVMS")
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "XXX", "SOA_AMT": 1000, "AMT_USD": 120.00}, lk, "05", fx)[0], "reject")
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "XXX", "SOA_AMT": 1000, "AMT_USD": 119.99}, lk, "05", fx)[0], "pass")
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "XXX", "SOA_AMT": 1000, "AMT_USD": 120.01}, lk, "05", fx)[0], "reject")

    def test_svms_missing_uses_hana(self):
        S2_select, S2_hana = S2.select, S2.hana_rates
        try:
            S2.select = lambda pkg, sp, prm: {"P_RS": [{"CUR_CD": prm["CUR_CD"], "EX_RATE": 0, "EX_UNIT": 1, "EX_RATE_USD": 1355.7}]}
            S2.hana_rates = lambda dt: {"TRY": 0.0199}
            f = S2.make_fx_lookup("20261110")
            self.assertEqual(f("TRY"), (0.0199, "Hana")); self.assertIsNone(f("BRL"))
        finally:
            S2.select, S2.hana_rates = S2_select, S2_hana

    def test_mgmt_fee(self):
        l = {"EXP_CD": "080101", "SOA_CUR_CD": "USD", "SOA_AMT": 10695, "AMT_USD": 10695}
        self.assertEqual(S2.judge_line(l, lk, "05", None, (10695.0, 10695.0, "345/day x 31d"))[0], "pass")
        self.assertEqual(S2.judge_line(l, lk, "05", None, (10695.0, 10695.99, "x"))[0], "pass")      # 반올림 허용
        v, _, tag, r = S2.judge_line(l, lk, "05", None, (10695.0, 11000.0, "345/day x 31d, CSM"))
        self.assertEqual((v, tag), ("reject", "C")); self.assertTrue(r.startswith("Mgmt fee over SMA: charged USD 11,000.00"))
        self.assertEqual(S2.judge_line(l, lk, "05", None, (10695.0, 4932.0, "x"))[0], "pass")       # 부분월(낮음)
        other = {"EXP_CD": "080104", "SOA_CUR_CD": "USD", "SOA_AMT": 750, "AMT_USD": 750}
        self.assertEqual(S2.judge_line(other, lk, "05", None, (10695.0, 99999.0, "x"))[0], "skip")

    def test_mf_expected(self):
        rec = {"basis": "daily", "rate": 204, "manager": "CSM"}
        self.assertEqual(S2.mf_expected(rec, "202602")[0], 204 * 28)
        self.assertEqual(S2.mf_expected(rec, "202401")[0], 204 * 31)
        self.assertEqual(S2.mf_expected({"basis": "monthly", "rate": 14254.42}, "202602")[0], 14254.42)

    def test_nrc(self):
        self.assertTrue(S2.is_nrc_text("NATIONAL RESPONSE CORPORATION\n2025 OPA 90 Service Invoice"))
        self.assertTrue(S2.is_nrc_text("NRC OPA'90 OSRO Services"))
        self.assertFalse(S2.is_nrc_text("Non recurring cost (NRC) for crane repair"))
        v, _, tag, r = S2.judge_line({"SOA_CUR_CD": "USD"}, lk, "05", None, None, True)
        self.assertEqual((v, tag, r), ("reject", "D", "NRC invoice"))
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "USD"}, lk, "05", None, None, False)[0], "pass")
        self.assertEqual(S2.judge_line({"SOA_CUR_CD": "USD", "RJT_YN": "Y", "RJT_RMK": "NRC invoice"}, lk, "05", None, None, True)[0], "already")

    def test_fee_db_loaded(self):
        db = S2.load_mgmt_fee()
        self.assertEqual(db["ATLANTIC MERCHANT"]["rate"], 204); self.assertEqual(db["PELICAN MARINER"]["basis"], "monthly")

    def test_nrc_remark_exact_when_combined(self):
        l = {"REF_NO": "ABCDCA2609010001", "SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 2000, "EXP_CD": "080101"}
        v, _, tags, r = S2.judge_line(l, lk, "05", self.FX, (10.0, 2000.0, "345/day x 31d, CSM"), True)
        self.assertEqual((v, tags), ("reject", "A+B+C+D"))
        self.assertTrue(r.startswith("NRC invoice | Operational AOR"), r)
        for k in ("ABCDCA2609010001", "FX over-charged", "Mgmt fee over SMA", "2,000"):
            self.assertIn(k, r)
        self.assertLessEqual(len(r), 240)

    def test_other_line_change_makes_stale(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}, {"SX_SEQ": 2, "REF_NO": None, "SOA_AMT": 5}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}})
        orig = S2._soa_now
        def mutate(*a, **k):
            st["lines"][2]["SOA_AMT"] = 999; return orig(*a, **k)
        S2._soa_now = mutate
        try:
            S2.run("TEST")
        finally:
            S2._soa_now = orig
        self.assertEqual(st["saves"], [])

    def test_nrc_scan_exception_is_warn(self):
        import soa_review as R
        o = R.get_files
        R.get_files = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            w = []; self.assertIsNone(S2.nrc_scan("SX", {"SX_SEQ": 1}, w)); self.assertTrue(w)
        finally:
            R.get_files = o

    def test_remark_never_truncates_reasons(self):
        long_b = ("B", "FX over-charged: EUR 99,999,999.99 -> USD 999,999,999.99 (ref rate 1.1222 SVMS, ref USD 112,219,999.99, +791.1%)" + "x" * 120)
        fs = [("A", "Operational AOR: ABCDCA2609010001 (Dept: Operational) - not chargeable to Technical SOA. Pls move to Operational SOA."),
              ("C", "Mgmt fee over SMA: charged USD 99,999,999.00 vs contract USD 10,695.00 (345/day x 31d, CSM)" + "y" * 150),
              long_b, ("D", "NRC invoice")]
        r = S2.combine_remarks(fs)
        self.assertLessEqual(len(r), 240)
        for k in ("NRC invoice", "Operational AOR", "Mgmt fee over SMA", "FX over-charged"):
            self.assertIn(k, r)
        self.assertTrue(r.startswith("NRC invoice"))

    def test_text_cache_hash_guard(self):
        import tempfile, shutil
        import soa_amount as SA
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "a.pdf"); open(p, "wb").write(b"%PDF-1 one")
            SA._text_cache_put(p, "text", "NATIONAL RESPONSE CORPORATION", SA._pdf_sha(p))
            self.assertEqual(SA.cached_texts(p), {"text": "NATIONAL RESPONSE CORPORATION"})
            open(p, "wb").write(b"%PDF-1 two")                        # 같은 크기 교체
            self.assertEqual(SA.cached_texts(p), {})
            SA._text_cache_put(p, "text", "stale", "deadbeef")         # 추출 중 교체(해시 불일치) → 저장 안 함
            self.assertEqual(SA.cached_texts(p), {})
        finally:
            shutil.rmtree(d)

    def test_nrc_checks_all_cached_texts(self):
        import soa_review as R, soa_amount as SA
        o = (R.get_files, R.dl, R._is_pdf_file, SA.cached_texts, SA.pdf_body)
        try:
            R.get_files = lambda *a: [{"FILE_NM": "x.pdf"}]; R._is_pdf_file = lambda f: True
            R.dl = lambda sx, seq, fs: ["/x.pdf"]
            SA.cached_texts = lambda p: {"ocr300p3": "z" * 5000, "text": "short page1 National Response Corporation"}
            SA.pdf_body = lambda p: (_ for _ in ()).throw(AssertionError("should not re-extract"))
            self.assertTrue(S2.nrc_scan("SX", {"SX_SEQ": 1}, []))
        finally:
            R.get_files, R.dl, R._is_pdf_file, SA.cached_texts, SA.pdf_body = o

    def test_multiple_rejects_same_soa_all_written(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001"}, {"SX_SEQ": 2, "REF_NO": "ABCDCA2609010001"},
                        {"SX_SEQ": 3, "REF_NO": "ABCDCA2609010001"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}})
        self.assertEqual(S2.run("TEST"), 0); self.assertEqual(len(st["saves"]), 3)

    def test_concurrent_change_on_written_line_stops_soa(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "ABCDCA2609010001", "SOA_AMT": 10},
                        {"SX_SEQ": 2, "REF_NO": "ABCDCA2609010001"}],
                       {"ABCDCA2609010001": {"AOR_CD": "ABCDCA2609010001", "DEPT_CD": "O"}})
        orig_save = S2.save
        def save_and_tamper(pkg, sp, prm):
            r = orig_save(pkg, sp, prm)
            if prm["SX_SEQ"] == 1:
                st["lines"][1]["SOA_AMT"] = 999        # 우리 기입과 동시에 같은 라인 금액 변경
            return r
        S2.save = save_and_tamper
        S2.run("TEST")
        self.assertEqual([p["SX_SEQ"] for p in st["saves"]], [1])   # 2번 라인은 스킵

    def test_dup_key_and_find(self):
        base = {"SOA_TP": "D", "SOA_CUR_CD": "SGD", "SOA_AMT": 552.6, "EXP_CD": "070101"}
        self.assertEqual(S2.dup_key({**base, "INV_NO": "TI2566393."}), ("TI2566393", "SGD", "552.6"))
        self.assertIsNone(S2.dup_key({**base, "INV_NO": "TI2566393", "SOA_TP": "A"}))        # Asset 정산 라인 제외
        self.assertIsNone(S2.dup_key({**base, "INV_NO": "STAC-000626", "EXP_CD": "080101"})) # 관리비 제외
        self.assertIsNone(S2.dup_key({**base, "INV_NO": "X1", "EXP_CD": "060102"}))           # 통신비 제외
        self.assertEqual(S2.dup_key({**base, "INV_NO": "A1", "SOA_AMT": -5.125})[2], "-5.125")  # 짧은 번호·음수·3자리 소수도 원값 그대로
        idx = {("TI2566393", "SGD", "552.6"): [("TSTA-2610010002", "0048", "TI2566393", "202609"), ("TSTA-2611100001", "0002", "TI2566393.", "202601")]}
        newer = {**base, "INV_NO": "TI2566393.", "SX_SEQ": "0002"}
        self.assertEqual(S2.find_dup(idx, "TSTA-2611100001", newer), ("TSTA-2610010002", "0048", "TI2566393"))
        self.assertFalse(S2.find_dup(idx, "TSTA-2610010002", {**base, "INV_NO": "TI2566393"}))   # 원본은 안 걸림
        self.assertFalse(S2.find_dup(idx, "TSTA-2611100001", {**base, "INV_NO": "TI2566393", "SOA_AMT": 552.61}))
        self.assertFalse(S2.find_dup(idx, "TSTA-2611100001", newer, "202310"))   # 원본이 ±24개월 밖이면 제외
        self.assertTrue(S2.find_dup(idx, "TSTA-2611100001", newer, "202601"))

    def test_dup_short_form_idempotent(self):
        prev = "Cost no match - " + "x" * 150
        r = S2.append_remark(prev, [("E", "Duplicate invoice: " + "9" * 40 + " = TSTA-2610010002 s0048"), ("A", "Operational AOR: ABCDCA2609010001 (Dept: Operational) - not chargeable")])
        self.assertIsNotNone(r); self.assertLessEqual(len(r), 240)
        l = {"SOA_CUR_CD": "USD", "RJT_YN": "Y", "RJT_RMK": r, "REF_NO": "ABCDCA2609010001"}
        self.assertEqual(S2.judge_line(l, lk, "05", None, None, None, ("TSTA-2610010002", "0048", "9" * 40))[0], "already")

    def test_dup_judge_and_append(self):
        l = {"SOA_CUR_CD": "USD", "RJT_YN": "Y", "RJT_RMK": "Cost no match - Amount: SOA USD 1,900 / Invoice 1575"}
        v, _, tags, r = S2.judge_line(l, lk, "05", None, None, True, ("TSTA-2602020002", "0012", "201549"))
        self.assertEqual((v, tags), ("reject", "D+E (기존 리마크에 덧붙임)"))
        self.assertEqual(r, "Cost no match - Amount: SOA USD 1,900 / Invoice 1575 | NRC invoice | Duplicate invoice: 201549 = TSTA-2602020002 s0012")

    def test_append_overflow_preserves(self):
        prev = "Cost no match - " + "x" * 225
        self.assertIsNone(S2.append_remark(prev, [("A", "Operational AOR: ABCDCA2609010001 (...)")]))

    def test_fund_request_dup(self):
        base = {"SOA_TP": "D", "SOA_CUR_CD": "USD", "SOA_AMT": 2100, "EXP_CD": "021408", "INV_NO": "26/11/03692.", "SOA_OPEX_TP": "A"}
        fr = {"OPEX_CD": "TSTCO2610070010", "INV_NO": "26/11/03692", "AMT": 2100, "CUR_CD": "USD", "STATUS": "P", "REF_NO": None, "REQ_DT": "20261007"}
        fidx = {("26110369" + "2", "USD", "2100"): [fr]}
        self.assertEqual(S2.find_fund_dup(fidx, {**base, "REF_NO": "TSTCA2609280001"}), ("TSTCO2610070010", None, "26/11/03692"))
        same_aor = {**fr, "REF_NO": "TSTCA2609280001"}
        self.assertFalse(S2.find_fund_dup({k: [same_aor] for k in fidx}, {**base, "REF_NO": "TSTCA2609280001"}))   # 같은 AOR = 정상
        self.assertFalse(S2.find_fund_dup(fidx, {**base, "REF_NO": "TSTCO2610070010"}))      # 라인이 그 비용청구 번호 참조
        self.assertFalse(S2.find_fund_dup(fidx, {**base, "REF_NO": " TSTCO261007001"}))      # 앞부분 일치(잘린 번호)
        self.assertFalse(S2.find_fund_dup(fidx, {**base, "REF_NO": None}))                    # AOR 라인 REF 미기재 = 판단불가
        self.assertEqual(S2.find_fund_dup(fidx, {**base, "REF_NO": None, "SOA_OPEX_TP": "O"})[0], "TSTCO2610070010")
        line = {**base, "REF_NO": "TSTCA2609280001"}
        self.assertTrue(S2.find_fund_dup(fidx, line, "202810"))      # 24개월 경계 안
        self.assertFalse(S2.find_fund_dup(fidx, line, "202811"))     # 25개월 = 범위 밖
        self.assertFalse(S2.find_fund_dup({k: [{**fr, "REQ_DT": None}] for k in fidx}, line, "202601"))  # 날짜 없음 = 제외
        self.assertEqual(S2.dup_remark("TSTCO2610070010", None, "26/11/03692"), "Duplicate invoice: 26/11/03692 = Fund Request TSTCO2610070010")

    def test_remark(self):
        r = S2.operational_remark("ABCDCA2609010001", "04")
        self.assertTrue(r.startswith("Operational AOR: ABCDCA2609010001"))
        self.assertIn("Crew SOA", r)
        self.assertLessEqual(len(r), 240)


if __name__ == "__main__":
    unittest.main()
