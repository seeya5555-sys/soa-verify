import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "soa_verify"))
import soa_stage2 as S2

AORS = {"SVMLCA2609010001": {"AOR_CD": "SVMLCA2609010001", "DEPT_CD": "O"},
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
        v, a, _, _r = S2.judge_line({"REF_NO": "SVMLCA2609010001", "CFM_YN": "Y"}, lk, "05")
        self.assertEqual((v, a), ("reject", "SVMLCA2609010001"))

    def test_technical_passes(self):
        self.assertEqual(S2.judge_line({"REF_NO": "ATMTCA2509250007"}, lk, "05")[0], "pass")

    def test_lookup_fail_closed(self):
        self.assertEqual(S2.judge_line({"REF_NO": "SVMLCA2609010001"}, boom, "05")[0], "error")
        self.assertEqual(S2.judge_line({"REF_NO": "XXXXCA2609010001"}, lk, "05")[0], "error")

    def test_existing_reject_not_overwritten(self):
        l = {"REF_NO": "SVMLCA2609010001", "RJT_YN": "Y", "RJT_RMK": "Cost no match"}
        self.assertEqual(S2.judge_line(l, lk, "05")[0], "already")
        l2 = {**l, "STATUS2": "Y", "STATUS_RMK2": "fixed"}
        self.assertEqual(S2.judge_line(l2, lk, "05")[0], "resub")

    def test_preserve_unrejected_with_remark(self):
        base = {"REF_NO": "SVMLCA2609010001", "RJT_YN": "N"}
        self.assertEqual(S2.judge_line({**base, "RJT_RMK": "old"}, lk, "05")[0], "already")
        self.assertEqual(S2.judge_line({**base, "STATUS2": "Y"}, lk, "05")[0], "resub")
        self.assertEqual(S2.judge_line({**base, "STATUS_RMK2": "mgr"}, lk, "05")[0], "resub")

    def _env(self, lines, aors, fail_after_write=False):
        st = {"lines": {l["SX_SEQ"]: dict(l) for l in lines}, "saves": [], "hdr": "S"}
        def select(pkg, sp, prm):
            if sp == "SP_GET_SOA":
                return {"P_RS": [{"SX_CD": "SVMLCX2609010001", "SL_TP": "05", "STATUS": st["hdr"], "SL_DM": "202609"}]}
            if sp == "SP_GET_SOA_INFO":
                return {"P_RS_D": [dict(v) for v in st["lines"].values()]}
            if sp == "SP_GET_AOR":
                a = aors.get(prm["AOR_CD"])
                if a == "boom": raise SystemExit("500")
                return {"P_RS": [a] if a else []}
        def save(pkg, sp, prm):
            st["saves"].append(prm)
            if not fail_after_write:
                st["lines"][prm["SX_SEQ"]].update(RJT_YN="Y", RJT_RMK=prm["RJT_RMK"], CFM_YN="N")
            return None
        orig = (S2.select, S2.save, S2.login, S2.make_fx_lookup)
        def restore():
            S2.select, S2.save, S2.login, S2.make_fx_lookup = orig
        self.addCleanup(restore)
        S2.select, S2.save, S2.login = select, save, lambda: None
        S2.make_fx_lookup = lambda dt=None: (lambda c: None)
        S2.VERIFY, S2.SL_TP, S2.FM_DM, S2.TO_DM, S2.CAP = True, "05", "202601", "202612", 300
        return st

    def test_preflight_error_blocks_all_writes(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "SVMLCA2609010001"}, {"SX_SEQ": 2, "REF_NO": "SVMLCA2609010002"}],
                       {"SVMLCA2609010001": {"AOR_CD": "SVMLCA2609010001", "DEPT_CD": "O"}, "SVMLCA2609010002": "boom"})
        self.assertEqual(S2.run("SVML"), 1); self.assertEqual(st["saves"], [])

    def test_write_verified_by_reread(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "SVMLCA2609010001"}],
                       {"SVMLCA2609010001": {"AOR_CD": "SVMLCA2609010001", "DEPT_CD": "O"}})
        self.assertEqual(S2.run("SVML"), 0); self.assertEqual(len(st["saves"]), 1)
        self.assertEqual(st["lines"][1]["RJT_YN"], "Y")

    def test_unconfirmed_write_stops(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "SVMLCA2609010001"}, {"SX_SEQ": 2, "REF_NO": "SVMLCA2609010001"}],
                       {"SVMLCA2609010001": {"AOR_CD": "SVMLCA2609010001", "DEPT_CD": "O"}}, fail_after_write=True)
        self.assertEqual(S2.run("SVML"), 1); self.assertEqual(len(st["saves"]), 1)

    def test_header_changed_skips(self):
        st = self._env([{"SX_SEQ": 1, "REF_NO": "SVMLCA2609010001"}],
                       {"SVMLCA2609010001": {"AOR_CD": "SVMLCA2609010001", "DEPT_CD": "O"}})
        orig = S2._fetch_line
        def flip(*a, **k):
            st["hdr"] = "C"; return orig(*a, **k)
        S2._fetch_line = flip
        try:
            S2.run("SVML")
        finally:
            S2._fetch_line = orig
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
        l = {"REF_NO": "SVMLCA2609010001", "SOA_CUR_CD": "EUR", "SOA_AMT": 600, "AMT_USD": 2000}
        v, _, tags, r = S2.judge_line(l, lk, "05", self.FX)
        self.assertEqual((v, tags), ("reject", "A+B")); self.assertIn(" | FX over-charged", r)
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

    def test_remark(self):
        r = S2.operational_remark("SVMLCA2609010001", "04")
        self.assertTrue(r.startswith("Operational AOR: SVMLCA2609010001"))
        self.assertIn("Crew SOA", r)
        self.assertLessEqual(len(r), 240)


if __name__ == "__main__":
    unittest.main()
