"""Offline selftest — no SVMS login, no Claude call. Checks the decision rules wire up as upstream."""
import os, sys, unittest
from unittest import mock

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "soa_verify")
sys.path.insert(0, PKG)
os.environ.pop("SOA_DIRECT_SKIP", None)

import svms  # noqa: E402
import llm_router as L  # noqa: E402
import soa_amount as SA  # noqa: E402
import soa_review as R  # noqa: E402

LINE = {"SOA_AMT": "570.00", "AMT_USD": "570.00", "SOA_CUR_CD": "USD", "EXP_CD": "010101"}
FILES = [{"FILE_NM": "INV 570.00.pdf", "SAVE_NM": "x", "FILE_EXT": "pdf"}]


class EngineRules(unittest.TestCase):
    def test_model_pinned_haiku_55(self):
        with mock.patch.dict(os.environ, {"SOA_REVIEW_MODEL": "auto"}):
            self.assertEqual(L.review_model(), "claude-haiku-5-5")

    def test_no_attachment_is_flag(self):
        with mock.patch.object(R, "get_files", return_value=[]):
            self.assertEqual(R.verify_opex("SX", "0001", "TEST", LINE, True)[0], "flag")

    def test_filename_match_alone_never_confirms(self):
        # file name carries the amount, but the invoice body does not support it → must stay flag
        with mock.patch.object(R, "get_files", return_value=FILES), \
             mock.patch.object(R, "dl", return_value=["/tmp/x.pdf"]), \
             mock.patch.object(SA, "resolve", return_value={"status": "flag", "reason": "t"}), \
             mock.patch.object(SA, "resolve_sum", return_value=None):
            self.assertEqual(R.verify_opex("SX", "0001", "TEST", LINE, True)[0], "flag")

    def test_body_supported_confirms(self):
        with mock.patch.object(R, "get_files", return_value=FILES), \
             mock.patch.object(R, "dl", return_value=["/tmp/x.pdf"]), \
             mock.patch.object(SA, "resolve", return_value={"status": "clean", "reason": "t"}):
            self.assertEqual(R.verify_opex("SX", "0001", "TEST", LINE, True)[0], "confirm")

    def test_llm_error_with_absent_amount_is_flag(self):
        body = "INVOICE\nVessel TEST\nTotal amount USD 1,261.00\nPayable within 30 days\n" + "x 1 2 3 4 5 6 7 8 9\n" * 30
        with mock.patch.object(SA, "pdf_body", return_value=body), \
             mock.patch.object(SA, "llm_json", return_value=dict(L.LLM_ERROR)):
            self.assertEqual(SA.resolve("/tmp/x.pdf", "TEST", 1268.0, "USD", usd_amt=1268.0)["status"], "flag")

    def test_direct_amount_in_total_line_is_clean(self):
        body = "INVOICE\nVessel TEST\nService charge\nTotal amount USD 570.00\n" + "x 1 2 3 4 5 6 7 8 9\n" * 30
        with mock.patch.object(SA, "pdf_body", return_value=body), \
             mock.patch.object(SA, "llm_json", return_value={"matches": True, "found_amount": 570.0,
                                                               "basis": "total", "confidence": 0.9}):
            self.assertEqual(SA.resolve("/tmp/x.pdf", "TEST", 570.0, "USD", usd_amt=570.0)["status"], "clean")

    def test_amount_reject_classifier(self):
        self.assertTrue(R._is_amount_reject("Cost no match: x"))


class RemarkAndWeb(unittest.TestCase):
    def test_reject_remark_template(self):
        R.LINE_DETAIL[("SX", "9")] = {"paths": [], "found_real": None}
        rmk = R.reject_remark("SX", "9", {"SOA_CUR_CD": "USD", "SOA_AMT": 10, "INV_NO": "A-1"}, "첨부 없음")
        self.assertTrue(rmk.startswith("Cost no match - Amount: SOA USD 10"))
        self.assertIn("Invoice No.", rmk)
        self.assertTrue(R._is_amount_reject(rmk))   # resubmission logic stays compatible
        self.assertLessEqual(len(rmk), 240)

    def test_web_build_cmd_validation(self):
        sys.path.insert(0, os.path.dirname(PKG))
        from soa_verify import web
        cmd = web.build_cmd("verify", "abcd, efgh", "202601", "202606", "05")
        self.assertEqual(cmd[3:], ["verify", "--vessel", "ABCD,EFGH", "--from", "202601", "--to", "202606", "--dept", "05", "--save"])
        for bad in (("verify", "AB", "202601", "202606", "05"), ("verify", "ABCD", "202607", "202601", "05"),
                    ("approve", "ABCD", "202601", "202606", "05"), ("verify", "ABCD;rm", "202601", "202606", "05")):
            with self.assertRaises(ValueError):
                web.build_cmd(*bad)


class WebSafety(unittest.TestCase):
    def test_user_rendered_as_text(self):
        sys.path.insert(0, os.path.dirname(PKG))
        from soa_verify import web
        self.assertIn("textContent=c.user", web.PAGE)
        self.assertNotIn("${c.user}", web.PAGE)

    def test_cross_process_lock(self):
        sys.path.insert(0, os.path.dirname(PKG))
        from soa_verify import web
        a = web._try_lock(); self.assertIsNotNone(a)
        self.assertIsNone(web._try_lock())          # second holder refused
        web._unlock(a)
        b = web._try_lock(); self.assertIsNotNone(b); web._unlock(b)   # released → can be taken again


class ScopeGuards(unittest.TestCase):
    def test_save_only_line_status(self):
        with self.assertRaises(PermissionError):
            svms.save("PKG_CO_SLIP_EX", "SP_SET_SOA_INFO", {})

    def test_save_rejects_extra_fields_or_double_status(self):
        base = {"SX_CD": "X", "SX_SEQ": "1", "CFM_YN": "Y", "RJT_YN": "N", "RJT_RMK": None, "STATUS2": "N", "STATUS_RMK2": None}
        with self.assertRaises(PermissionError):
            svms.save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS", dict(base, STATUS="C"))
        with self.assertRaises(PermissionError):
            svms.save("PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS", dict(base, RJT_YN="Y"))

    def test_remark_save_guards(self):
        from unittest import mock
        hdr = {"SX_CD": "X", "STATUS": "C"}
        line = {"SX_SEQ": "0001", "SUBJ": "OTHERS", "RMK": "OTHERS", "CFM_YN": "Y"}
        with mock.patch.object(svms, "select", return_value={"P_RS": [hdr], "P_RS_D": [line]}), \
                mock.patch.object(svms, "_save_raw") as raw:
            ok, _ = svms.save_line_remarks("X", {"0001": {"RMK": "a"}})
            self.assertFalse(ok); raw.assert_not_called()                      # 승인된(C) SOA 거부
        hdr = dict(hdr, STATUS="S")
        with mock.patch.object(svms, "select", return_value={"P_RS": [hdr], "P_RS_D": [line]}), \
                mock.patch.object(svms, "_save_raw") as raw:
            self.assertFalse(svms.save_line_remarks("X", {"0001": {"CFM_YN": "N"}})[0])   # Remark 외 필드 거부
            self.assertFalse(svms.save_line_remarks("X", {"0001": {"RMK": "x" * 121}})[0])  # 길이 초과 거부
            raw.assert_not_called()
        seen = {}
        def fake_raw(pkg, sp, prm, cur):
            seen["status"] = prm.get("STATUS"); seen["pic"] = cur["P_IC"]; return {"data": {}}
        after = [{"P_RS": [hdr], "P_RS_D": [line]}, {"P_RS": [hdr], "P_RS_D": [dict(line, RMK="OTHERS | 적요: x")]}]
        with mock.patch.object(svms, "select", side_effect=after), mock.patch.object(svms, "_save_raw", side_effect=fake_raw):
            ok, msg = svms.save_line_remarks("X", {"0001": {"RMK": "OTHERS | 적요: x"}}, {"0001": {"SUBJ": "OTHERS", "RMK": "OTHERS"}})
        self.assertTrue(ok, msg); self.assertEqual(seen["status"], "S")
        self.assertEqual(seen["pic"][0]["CFM_YN"], "Y")                         # 다른 라인 필드는 재조회값 그대로

    def test_remark_save_refuses_if_changed(self):
        from unittest import mock
        hdr = {"SX_CD": "X", "STATUS": "S"}
        line = {"SX_SEQ": "0001", "SUBJ": "EDITED", "RMK": "EDITED"}
        with mock.patch.object(svms, "select", return_value={"P_RS": [hdr], "P_RS_D": [line]}), \
                mock.patch.object(svms, "_save_raw") as raw:
            ok, _ = svms.save_line_remarks("X", {"0001": {"RMK": "OTHERS | 적요: x"}}, {"0001": {"SUBJ": "OTHERS", "RMK": "OTHERS"}})
            self.assertFalse(ok); raw.assert_not_called()                      # 사이에 사람이 고침 → 저장 안 함
            self.assertFalse(svms.save_line_remarks("X", {"0001": {"RMK": "a"}})[0])   # expect 없음 → 거부
            raw.assert_not_called()

    def test_select_allowlist(self):
        with self.assertRaises(PermissionError):
            svms.select("PKG_CO_SLIP", "SP_SET_SLIP", {})

    def test_no_approval_or_mail_code_shipped(self):
        for fn in os.listdir(PKG):
            if fn.endswith(".py"):
                with open(os.path.join(PKG, fn), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("approve_soa", "slip_submit", "send_mail", "SP_SET_SOA_INFO\"", "PKG_CO_SLIP\""):
                    if bad == "SP_SET_SOA_INFO\"" and fn == "svms.py":
                        continue   # 11-11 형 승인: svms.save_line_remarks 전용(STATUS=S 고정·Remark만) — 아래 테스트로 가드 검증
                    self.assertNotIn(bad, src, f"{fn} contains {bad}")
                if fn == "svms.py":
                    self.assertEqual(src.count("SP_SET_SOA_INFO\""), 1, "header save only inside save_line_remarks")


if __name__ == "__main__":
    unittest.main()
