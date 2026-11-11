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
    # 2차 검증(stage2): A Operational AOR · B 환율 · E 중복(비용청구) — 읽기 전용 (11-10 누락 수정)
    ("PKG_CO_OPEX", "SP_GET_AOR"), ("PKG_CO", "SP_GET_EX_RATE"), ("PKG_CO_OPEX", "SP_GET_OPEX"),
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
    return _save_raw(package, procedure, param, cursor)


def _save_raw(package, procedure, param, cursor=None):
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


REMARK_MAX = 120   # SVMS 'Check remark max length(200)' — 11-11 실측 120 저장·190 거부 → 안전값 120
REMARK_KEYS = ("SUBJ", "RMK")
_VOLATILE = ("UPST_DI", "UPST_USER_ID", "UPST_NM", "STATUS_DT")   # 저장 시 서버가 갱신(11-11 SAPS 실측)


def save_line_remarks(sx_cd, updates, expect=None):
    """SOA 라인 Remark(SUBJ·RMK)만 저장. updates={SX_SEQ: {"SUBJ": .., "RMK": ..}}.
    헤더 저장 요청(SP_SET_SOA_INFO)을 쓰지만 아래 가드를 통과해야만 보낸다(11-11 형 승인 — 승인·상태변경 불가):
      ① 직전 재조회 헤더 STATUS == "S"(Submitted)만 · 헤더는 재조회값 그대로(STATUS 변경 불가)
      ② 라인은 재조회값 사본에 SUBJ·RMK만 교체, 길이 ≤ REMARK_MAX
      ③ 저장 후 재조회: 헤더(UPST_* 외) 불변·STATUS S, 대상 라인 SUBJ·RMK 외 불변, 다른 라인 불변
    expect={SX_SEQ: {"SUBJ": 요약 전 값, "RMK": 요약 전 값}} — 재조회값과 다르면(그 사이 사람이 수정) 저장 거부(원문 보존).
    returns (ok: bool, msg: str)"""
    if not isinstance(updates, dict) or not updates:
        return False, "no updates"
    info = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx_cd})
    hdr = (info.get("P_RS") or [None])[0]
    lines = info.get("P_RS_D") or []
    if not hdr or hdr.get("STATUS") != "S" or hdr.get("SX_CD") != sx_cd:
        return False, f"header not Submitted ({hdr.get('STATUS') if hdr else None})"
    by = {str(l.get("SX_SEQ")): l for l in lines}
    updates = {str(k): v for k, v in updates.items()}
    pic = []
    for seq, u in updates.items():
        cur = by.get(str(seq))
        if cur is None or not isinstance(u, dict) or set(u) - set(REMARK_KEYS):
            return False, f"bad update s{seq}"
        for k, v in u.items():
            if not isinstance(v, str) or len(v) > REMARK_MAX:
                return False, f"s{seq} {k} too long/invalid"
        exp = (expect or {}).get(seq, (expect or {}).get(str(seq)))
        if exp is None or any(str(cur.get(k) or "") != str(exp.get(k) or "") for k in REMARK_KEYS):
            return False, f"s{seq} remark changed since read (or no expect) — not saved"
        pic.append(dict(cur, **u))
    res = _save_raw("PKG_CO_SLIP_EX", "SP_SET_SOA_INFO", dict(hdr), {"P_IC": pic})
    if not isinstance(res, dict) or res.get("error"):
        return False, f"save failed: {str(res)[:160]}"
    post = select("PKG_CO_SLIP_EX", "SP_GET_SOA_INFO", {"SX_CD": sx_cd})
    h2 = (post.get("P_RS") or [None])[0] or {}
    if h2.get("STATUS") != "S" or any(hdr.get(k) != h2.get(k) for k in set(hdr) | set(h2) if k not in _VOLATILE):
        return False, "header changed after save"
    by2 = {str(l.get("SX_SEQ")): l for l in (post.get("P_RS_D") or [])}
    if set(by2) != set(by):
        return False, "line set changed"
    for seq, l0 in by.items():
        l1 = by2[seq]
        want = dict(l0, **updates.get(seq, {}))
        if any(want.get(k) != l1.get(k) for k in set(want) | set(l1) if k not in _VOLATILE):
            return False, f"s{seq} not as expected after save"
    return True, "ok"
