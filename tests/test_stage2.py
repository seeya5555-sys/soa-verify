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
        v, a, _ = S2.judge_line({"REF_NO": "SVMLCA2609010001", "CFM_YN": "Y"}, lk, "05")
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
        S2.select, S2.save, S2.login = select, save, lambda: None
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

    def test_remark(self):
        r = S2.operational_remark("SVMLCA2609010001", "04")
        self.assertTrue(r.startswith("Operational AOR: SVMLCA2609010001"))
        self.assertIn("Crew SOA", r)
        self.assertLessEqual(len(r), 240)


if __name__ == "__main__":
    unittest.main()
