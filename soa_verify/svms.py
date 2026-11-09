#!/usr/bin/env python3
"""SVMS gateway helper (stdlib only): login + common select/save procedures + NAS file download.

Credentials are read from the user's own local config (never committed):
  env SVMS_USER / SVMS_PASS, or ~/.soa-verify/config.json {"svms_user": "...", "svms_pass": "..."}
This module intentionally has NO approval, slip-submit or mail functions.
"""
import json, os, socket, time, threading, urllib.error, urllib.request, http.cookiejar

CONFIG_PATH = os.path.expanduser(os.environ.get("SOA_VERIFY_CONFIG", "~/.soa-verify/config.json"))


def _cfg():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_C = _cfg()
BASE = os.environ.get("SVMS_BASE") or _C.get("svms_base") or "https://svms.sinokor.co.kr"
USER_ID = os.environ.get("SVMS_USER") or _C.get("svms_user") or ""
if not BASE.startswith("https://"):
    raise SystemExit("SVMS base URL must be https://")

# Client-side allowlist (defense in depth; the real permission boundary is your SVMS account).
READ_ALLOW = {
    ("PKG_CM_USER", "SP_GET_USER_INFO"), ("PKG_CM_CD", "SP_GET_VSL"),
    ("PKG_CO_SLIP_EX", "SP_GET_SOA"), ("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO"), ("PKG_CO_SLIP_EX", "SP_GET_SOA_OPR"),
    ("PKG_CM_FILE", "SP_GET_FILE"), ("PKG_CM_REF", "SP_GET_REF_DOC"),
}
WRITE_PKG, WRITE_PROC = "PKG_CO_SLIP_EX", "SP_SET_SOA_D_STATUS"
WRITE_KEYS = {"SX_CD", "SX_SEQ", "CFM_YN", "RJT_YN", "RJT_RMK", "STATUS2", "STATUS_RMK2"}

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))
_OPEN_LOCK = threading.RLock()


def _post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with _OPEN_LOCK:
        with _opener.open(req, timeout=60) as r:
            return json.loads(r.read().decode())


def login():
    pw = os.environ.get("SVMS_PASS") or _C.get("svms_pass") or ""
    if not USER_ID or not pw:
        raise SystemExit(f"SVMS credentials missing — run `python3 -m soa_verify setup` or edit {CONFIG_PATH}")
    last = None
    for attempt in range(6):
        try:
            res = _post("/api/login", {"p": f"{USER_ID}=:=", "c": pw, "lang": "ko_KR"})
            if res.get("authenticated"):
                return res
            last = "not authenticated"
        except Exception as e:
            last = type(e).__name__
        time.sleep(2 * (attempt + 1))
    raise SystemExit(f"LOGIN FAILED after retries: {last}")


def select(package, procedure, param):
    if (package, procedure) not in READ_ALLOW:
        raise PermissionError(f"soa-verify does not call {package}.{procedure}")
    last = None
    for attempt in range(4):
        try:
            res = _post("/api/vms/comm/selectVmsCommonProcedure",
                {"DATASET": [{"PACKAGE": package, "PROCEDURE": procedure, "PARAM": param}]})
            return res.get("data", {}).get(procedure, {})
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (500, 502, 503, 504):
                time.sleep(2 * (attempt + 1)); continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as e:
            last = e
            time.sleep(2 * (attempt + 1)); continue
    raise SystemExit(f"SELECT FAILED after retries ({package}.{procedure}): {last}")


def select_opt(package, procedure, param):
    try:
        return select(package, procedure, param)
    except (Exception, SystemExit) as e:
        try:
            raw = str(e).split(": ")[-1]
            reason = "".join(c if c.isprintable() else " " for c in raw).strip()[:80] or "unknown"
            print(f"[info] optional lookup skipped ({package}.{procedure}) — {reason}")
        except Exception:
            pass
        return None


def save(package, procedure, param, cursor=None):
    # soa-verify only ever writes the SOA line status (checkbox / reject remark).
    if (package, procedure) != (WRITE_PKG, WRITE_PROC) or cursor is not None:
        raise PermissionError(f"soa-verify does not write {package}.{procedure} (line status only)")
    if not isinstance(param, dict) or set(param) != WRITE_KEYS:
        raise PermissionError("line-status write must carry exactly the line status fields")
    if param.get("CFM_YN") not in ("Y", "N") or param.get("RJT_YN") not in ("Y", "N") \
            or (param["CFM_YN"] == "Y") == (param["RJT_YN"] == "Y"):
        raise PermissionError("line status must be exactly one of confirm / reject")
    ds = {"PACKAGE": package, "PROCEDURE": procedure, "PARAM": param}
    if cursor is not None:
        ds["CURSOR"] = cursor
    last = None; last_body = None
    for attempt in range(4):
        try:
            return _post("/api/vms/comm/saveVmsCommonProcedure", {"DATASET": [ds]})
        except urllib.error.HTTPError as e:
            last = e
            try:
                raw = e.read().decode("utf-8", "replace")
                try:
                    last_body = json.loads(raw).get("message") or raw
                except Exception:
                    last_body = raw
            except Exception:
                last_body = None
            if e.code in (409, 500, 502, 503):
                time.sleep(1.5 * (attempt + 1)); continue
            raise
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1)); continue
    return {"error": f"HTTP {getattr(last, 'code', '?')} after retries",
            "message": str(last), "body": (last_body or "")[:500]}


def download_nas(remote_path, file_nm, save_nm, out_path):
    import urllib.parse
    qs = urllib.parse.urlencode({"remotePath": remote_path, "fileNm": file_nm, "saveNm": save_nm})
    req = urllib.request.Request(BASE + "/api/file/downloadFileNas?" + qs)
    tmp = out_path + ".part"
    last = None
    for attempt in range(4):
        try:
            with _OPEN_LOCK:
                with _opener.open(req, timeout=120) as r:
                    b = r.read()
            with open(tmp, "wb") as f:
                f.write(b)
            os.replace(tmp, out_path)
            return len(b)
        except (TimeoutError, socket.timeout, urllib.error.URLError, ConnectionError) as e:
            last = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            time.sleep(2 * (attempt + 1))
    raise TimeoutError(f"download_nas failed after retries: {last}")


def purge_pdfs(pdfdir, prefix):
    n = 0
    try:
        for fn in os.listdir(pdfdir):
            if fn.startswith(prefix):
                try:
                    os.remove(os.path.join(pdfdir, fn)); n += 1
                except OSError:
                    pass
    except OSError:
        pass
    return n
