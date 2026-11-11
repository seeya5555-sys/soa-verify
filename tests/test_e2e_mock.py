"""End-to-end wiring test against a fake SVMS (no network, no AI call).

Runs the real CLI (`verify`, `stage2`, `cleanup`) with only the HTTP layer (`svms._post`) replaced, so every
procedure the engine calls must pass the client-side allowlist and every write must pass the write guard.
If the engine starts calling a procedure that `svms.READ_ALLOW` does not list, this test fails.
"""
import contextlib, copy, io, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(os.path.dirname(HERE), "soa_verify")
sys.path.insert(0, PKG)

import svms  # noqa: E402
import cli  # noqa: E402
import soa_stage2 as S2  # noqa: E402

VSL = "ABCD"
SX = "ABCD-2601010001"


class FakeSvms:
    def __init__(self, lines, status="S", sl_tp="05"):
        self.lines = {str(l["SX_SEQ"]): dict(l) for l in lines}
        self.status, self.sl_tp = status, sl_tp
        self.calls, self.saves = [], []

    def post(self, path, payload):
        if path == "/api/login":
            return {"authenticated": True}
        ds = payload["DATASET"][0]
        pkg, proc, prm = ds["PACKAGE"], ds["PROCEDURE"], ds["PARAM"]
        self.calls.append((pkg, proc))
        if path.endswith("saveVmsCommonProcedure"):
            self.saves.append(dict(prm))
            self.lines[str(prm["SX_SEQ"])].update({k: prm[k] for k in ("CFM_YN", "RJT_YN", "RJT_RMK", "STATUS2", "STATUS_RMK2")})
            return {"data": {proc: {}}}
        return {"data": {proc: self.select(proc, prm)}}

    def select(self, proc, prm):
        if proc == "SP_GET_VSL":   # vessel master (CODE) — also answers the engine's single-vessel lookup (NAME)
            return {"P_RS": [{"CODE": VSL, "OW_COMP_ID": "037", "NAME": "TEST SHIP NOT IN FEE TABLE", "IMO_NO": "1234567"},
                             {"CODE": "EFGH", "OW_COMP_ID": "001", "NAME": "OTHER SHIP", "IMO_NO": "7654321"}]}
        if proc == "SP_GET_SOA":
            return {"P_RS": [{"SX_CD": SX, "SL_TP": self.sl_tp, "STATUS": self.status, "SL_DM": "202601"}]}
        if proc == "SP_GET_SOA_INFO":
            return {"P_RS": [{"SX_CD": SX, "STATUS": self.status}], "P_RS_D": copy.deepcopy(list(self.lines.values()))}
        if proc == "SP_GET_AOR":
            cd = prm.get("AOR_CD")
            return {"P_RS": [{"AOR_CD": cd, "DEPT_CD": "O" if cd.endswith("0001") else "T"}]}
        if proc == "SP_GET_EX_RATE":   # 1 EUR = 1600/1400 USD
            return {"P_RS": [{"CUR_CD": prm.get("CUR_CD"), "EX_RATE": 1600, "EX_UNIT": 1, "EX_RATE_USD": 1400}]}
        return {"P_RS": [], "P_RS_D": []}   # SP_GET_OPEX / SP_GET_FILE / SP_GET_REF_DOC: nothing


def line(seq, **kw):
    base = {"SX_CD": SX, "SX_SEQ": seq, "CFM_YN": "N", "RJT_YN": "N", "RJT_RMK": None, "STATUS2": "N", "STATUS_RMK2": None,
            "SOA_TP": "D", "SOA_OPEX_TP": "O", "SOA_CUR_CD": "USD", "SOA_AMT": 100, "AMT_USD": 100, "EXP_CD": "050101",
            "EXP_NM": "TEST", "REF_NO": None, "INV_NO": f"INV-{seq}", "FILE_REF_NO": None, "FILE_NO": None}
    base.update(kw)
    return base


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.saved = (svms._post, svms.USER_ID, cli.CACHE, os.environ.get("SVMS_PASS"),
                      (S2.VERIFY, S2.SL_TP, S2.FM_DM, S2.TO_DM, S2.CAP))
        svms.USER_ID = "tester"; os.environ["SVMS_PASS"] = "x"
        os.environ["SOA_VERIFY_SKIP_PREFLIGHT"] = "1"; self.addCleanup(os.environ.pop, "SOA_VERIFY_SKIP_PREFLIGHT", None)
        cli.CACHE = os.path.join(self.tmp.name, "_cache")
        self.addCleanup(self._restore)

    def _restore(self):
        svms._post, svms.USER_ID, cli.CACHE, pw, s2 = self.saved
        S2.VERIFY, S2.SL_TP, S2.FM_DM, S2.TO_DM, S2.CAP = s2
        if pw is None:
            os.environ.pop("SVMS_PASS", None)
        else:
            os.environ["SVMS_PASS"] = pw

    def run_cli(self, fake, *argv):
        svms._post = fake.post
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cli.main(list(argv)); rc = 0
            except SystemExit as e:
                rc = e.code
        return rc, buf.getvalue()

    def assert_allowed(self, fake):
        reads = {c for c in fake.calls if c[1].startswith("SP_GET")}
        self.assertTrue(reads <= svms.READ_ALLOW, f"not in READ_ALLOW: {sorted(reads - svms.READ_ALLOW)}")

    def stage2_fake(self):
        return FakeSvms([
            line("0001", REF_NO="ABCDCA2609010001"),                               # A: Operational AOR
            line("0002", SOA_CUR_CD="EUR", SOA_AMT=100, AMT_USD=200),               # B: FX 175% of reference
            line("0003", REF_NO="ABCDCA2609010002"),                               # Technical AOR → pass
            line("0004", SOA_CUR_CD="EUR", SOA_AMT=100, AMT_USD=115),               # within 120% → pass
        ])

    def test_stage2_dry_then_save_then_idempotent(self):
        fake = self.stage2_fake()
        rc, out = self.run_cli(fake, "stage2", "--vessel", VSL, "--from", "202601", "--to", "202612")
        self.assertEqual(rc, 0, out); self.assertEqual(fake.saves, [])               # DRY writes nothing
        self.assertNotIn("확인필요 1", out); self.assertNotIn("does not call", out)
        self.assert_allowed(fake)
        for proc in ("SP_GET_AOR", "SP_GET_EX_RATE"):
            self.assertIn(proc, {c[1] for c in fake.calls})
        # Fund Request is a remittance document, not a cost record — never compared for duplicates.
        self.assertNotIn("SP_GET_OPEX", {c[1] for c in fake.calls})

        rc, out = self.run_cli(fake, "stage2", "--vessel", VSL, "--from", "202601", "--to", "202612", "--save")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(s["SX_SEQ"] for s in fake.saves), ["0001", "0002"])
        self.assertTrue(fake.lines["0001"]["RJT_RMK"].startswith("Operational AOR"))
        self.assertTrue(fake.lines["0002"]["RJT_RMK"].startswith("FX over-charged"))
        self.assertEqual((fake.lines["0003"]["RJT_YN"], fake.lines["0004"]["RJT_YN"]), ("N", "N"))

        n = len(fake.saves)
        rc, out = self.run_cli(fake, "stage2", "--vessel", VSL, "--from", "202601", "--to", "202612", "--save")
        self.assertEqual(rc, 0, out); self.assertEqual(len(fake.saves), n)           # second run writes nothing

    def test_unknown_vessel_code_refused(self):
        fake = self.stage2_fake()
        for cmd in ("stage2", "verify", "cleanup"):
            rc, _ = self.run_cli(fake, cmd, "--vessel", "ZZZZ", "--save")
            self.assertNotEqual(rc, 0); self.assertEqual(fake.saves, [])

    def test_my_vessel_is_never_queried_and_bulk_modes_are_gone(self):
        fake = self.stage2_fake()
        self.run_cli(fake, "verify", "--vessel", VSL)
        self.run_cli(fake, "stage2", "--vessel", VSL)
        self.run_cli(fake, "doctor")
        self.assertNotIn("SP_GET_USER_INFO", {c[1] for c in fake.calls})
        for argv in (["owners"], ["verify", "--all"], ["verify", "--owner", "037"], ["verify"]):
            with contextlib.redirect_stderr(io.StringIO()):
                rc, _ = self.run_cli(FakeSvms([line("0001")]), *argv)
            self.assertNotEqual(rc, 0, argv)

    def test_only_named_vessels_are_touched(self):
        fake = self.stage2_fake()                       # master also lists EFGH; only ABCD was named
        self.run_cli(fake, "verify", "--vessel", VSL, "--save")
        self.assertTrue(all(str(s["SX_CD"]).startswith(VSL) for s in fake.saves))

    def test_stage1_verify_asset_confirm_and_no_attachment_flag(self):
        fake = FakeSvms([line("0001", SOA_TP="A"), line("0002")])
        rc, out = self.run_cli(fake, "verify", "--vessel", VSL, "--from", "202601", "--to", "202612")
        self.assertEqual(rc, 0, out); self.assertEqual(fake.saves, [])
        self.assertIn("CONFIRM", out); self.assertIn("REJECT?", out)
        rc, out = self.run_cli(fake, "verify", "--vessel", VSL, "--from", "202601", "--to", "202612", "--save")
        self.assertEqual(rc, 0, out)
        self.assertEqual(fake.lines["0001"]["CFM_YN"], "Y")
        self.assertNotEqual(fake.lines["0002"]["CFM_YN"], "Y")                       # never confirmed without an invoice
        self.assert_allowed(fake)

    def test_cleanup_only_touches_confirmed_lines_with_leftover_remark(self):
        fake = FakeSvms([line("0001", CFM_YN="Y", RJT_RMK="Cost no match - old"), line("0002", RJT_YN="Y", RJT_RMK="keep")])
        rc, out = self.run_cli(fake, "cleanup", "--vessel", VSL, "--from", "202601", "--to", "202612", "--save")
        self.assertEqual(rc, 0, out)
        self.assertEqual([s["SX_SEQ"] for s in fake.saves], ["0001"])
        self.assertIsNone(fake.lines["0001"]["RJT_RMK"]); self.assertEqual(fake.lines["0002"]["RJT_RMK"], "keep")

    def test_missing_program_stops_before_any_svms_call(self):
        os.environ.pop("SOA_VERIFY_SKIP_PREFLIGHT", None)
        fake = FakeSvms([line("0001")])
        orig = cli.missing_tools
        cli.missing_tools = lambda need=(): ["pdftotext"]
        try:
            rc, _ = self.run_cli(fake, "verify", "--vessel", VSL, "--save")
            rc2, _ = self.run_cli(fake, "stage2", "--vessel", VSL, "--save")
        finally:
            cli.missing_tools = orig
        self.assertTrue(rc and rc2); self.assertEqual((fake.calls, fake.saves), ([], []))

    def test_doctor_reports_missing(self):
        fake = FakeSvms([line("0001")])
        orig = cli.missing_tools
        cli.missing_tools = lambda need=(): ["tesseract"]
        try:
            rc, out = self.run_cli(fake, "doctor")
        finally:
            cli.missing_tools = orig
        self.assertEqual(rc, 1); self.assertIn("[NG] tesseract", out); self.assertIn("vessel master 2 vessel", out)
        self.assertEqual(fake.saves, [])

    def test_default_period_is_current_year(self):
        import datetime
        y = datetime.date.today().year
        a = cli.build_parser().parse_args(["stage2", "--vessel", VSL])
        self.assertEqual((a.fm, a.to), (f"{y}01", f"{y}12"))


if __name__ == "__main__":
    unittest.main()
