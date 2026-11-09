"""Optional web embed kit — drop soa-verify into your own Flask site.

    from soa_verify.web import create_blueprint
    app.register_blueprint(create_blueprint(), url_prefix="/soa-verify")   # protect with YOUR login

Page = the same form as the upstream "선박별 SOA 검증": vessel code(s), from/to YYYYMM, dept, [검증 실기입], [리마크정리].
SVMS id/password are entered once on the page and stored locally (~/.soa-verify/config.json, permission 600).
The model is fixed (Claude Haiku 5.5 via your local `claude` CLI). No approval / slip / mail here, by design.
This blueprint has NO authentication of its own — mount it behind your site's login.
"""
import fcntl, json, os, re, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "_cache", "web_run.log")
LOCKFILE = os.path.join(ROOT, "_cache", "web_run.lock")   # cross-process(멀티 워커) 동시 실행 차단
_LOCK = threading.Lock()
_STATE = {"running": False, "started": None, "cmd": None, "rc": None}

VSL_RE = re.compile(r"^[A-Z0-9]{4}(,[A-Z0-9]{4})*$")
YM_RE = re.compile(r"^20\d{2}(0[1-9]|1[0-2])$")


def build_cmd(action, vessels, fm, to, dept):
    """Validated argv for one run. action: verify|cleanup. Always --save (= the page's 실기입 buttons)."""
    vessels = (vessels or "").replace(" ", "").upper()
    if not VSL_RE.match(vessels):
        raise ValueError("선박코드는 4자리, 여러 척이면 쉼표로 구분 (예: SVGW,SAPS)")
    if not (YM_RE.match(fm or "") and YM_RE.match(to or "")) or fm > to:
        raise ValueError("기간은 YYYYMM, 시작 ≤ 끝")
    if dept not in ("05", "04"):
        raise ValueError("부서는 05(Technical) / 04(Crew)")
    base = [sys.executable, "-m", "soa_verify"]
    if action == "verify":
        return base + ["verify", "--vessel", vessels, "--from", fm, "--to", to, "--dept", dept, "--save"]
    if action == "cleanup":
        return base + ["cleanup", "--vessel", vessels, "--from", fm, "--to", to, "--save"]
    raise ValueError("unknown action")


def _try_lock():
    os.makedirs(os.path.dirname(LOCKFILE), exist_ok=True)
    fd = os.open(LOCKFILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _run(cmd, lock_fd):
    try:
        _run_inner(cmd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)


def _run_inner(cmd):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w") as f:
        f.write(f"$ {' '.join(cmd[2:])}\n")
        f.flush()
        rc = subprocess.call(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                             env=dict(os.environ, PYTHONUNBUFFERED="1"))
    with _LOCK:
        _STATE.update(running=False, rc=rc)


def _cred_saved():
    from . import svms
    try:
        with open(svms.CONFIG_PATH) as f:
            c = json.load(f)
        return bool(c.get("svms_user") and c.get("svms_pass")), c.get("svms_user") or ""
    except (OSError, ValueError):
        return False, ""


PAGE = """<!doctype html><meta charset=utf-8><title>SOA 검증</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;color:#222}.box{border:1px solid #ddd;border-radius:6px;background:#f6f5f1;padding:12px;max-width:980px}
input,select{padding:7px 9px;border:1px solid #ccc;border-radius:4px;font-size:14px;margin:3px}button{padding:8px 14px;border-radius:5px;border:1px solid #1f5fa8;font-weight:700;margin:3px;cursor:pointer}
.live{background:#1f5fa8;color:#fff}.soft{background:#e6f0fb;color:#1f5fa8}pre{background:#111;color:#ddd;padding:10px;border-radius:6px;max-height:420px;overflow:auto;white-space:pre-wrap}</style>
<h3>선박별 SOA 검증 (온디맨드)</h3>
<div class=box id=cred></div><br>
<div class=box>
<input id=vsl placeholder="선박코드 (예: SVGW,SAPS)" style="width:190px;text-transform:uppercase">
<input id=fm placeholder="시작 YYYYMM" maxlength=6 style="width:110px"><input id=to placeholder="끝 YYYYMM" maxlength=6 style="width:110px">
<select id=tp><option value=05>Technical (05)</option><option value=04>Crew (04)</option></select>
<button class=live onclick="run('verify')">검증 실기입</button><br><button class=soft onclick="run('cleanup')">리마크정리</button>
<div style="font-size:12px;color:#666;margin-top:6px">판정 모델: Claude Haiku 5.5 고정 · 체크박스/리젝 리마크만 저장(승인·출금·메일 없음)</div></div>
<h4>실행 로그</h4><pre id=log>—</pre>
<script>
const B=location.pathname.replace(/\\/$/,'');
async function j(u,o){const r=await fetch(B+u,o);return r.json()}
async function cred(){const c=await j('/api/cred');const el=document.getElementById('cred');
 if(!c.saved){edit();return}
 el.innerHTML='SVMS 계정 저장됨: <b id=cu></b> <button class=soft onclick="edit()">변경</button>';
 document.getElementById('cu').textContent=c.user}
function edit(){document.getElementById('cred').innerHTML=`SVMS 아이디 <input id=u> 비밀번호 <input id=p type=password> <button class=live onclick="save()">저장</button>`}
async function save(){const r=await j('/api/cred',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:u.value,pw:p.value})});if(r.error)alert(r.error);cred()}
async function run(a){if(a==='verify'&&!confirm('SVMS에 체크박스·리젝 리마크가 실제로 기입됩니다. 진행할까요?'))return;
 const r=await j('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:a,vessels:vsl.value,fm:fm.value,to:to.value,dept:tp.value})});if(r.error)alert(r.error);poll()}
async function poll(){const s=await j('/api/status');document.getElementById('log').textContent=(s.running?'[실행중] ':s.rc===null?'':'[종료 rc='+s.rc+'] ')+'\\n'+s.log;if(s.running)setTimeout(poll,3000)}
cred();poll();
</script>"""


def create_blueprint(name="soa_verify"):
    from flask import Blueprint, jsonify, request   # optional dependency, only needed for the web kit
    from . import cli
    bp = Blueprint(name, __name__)

    @bp.get("/")
    def page():
        return PAGE

    @bp.get("/api/cred")
    def cred_get():
        saved, user = _cred_saved()
        return jsonify(saved=saved, user=user)

    @bp.post("/api/cred")
    def cred_set():
        d = request.get_json(silent=True) or {}
        user, pw = str(d.get("user") or "").strip(), str(d.get("pw") or "")
        if not user or not pw:
            return jsonify(error="아이디/비밀번호를 입력하세요"), 400
        cli._write_config(user, pw)
        return jsonify(ok=True)

    @bp.post("/api/run")
    def run():
        d = request.get_json(silent=True) or {}
        if not _cred_saved()[0]:
            return jsonify(error="먼저 SVMS 계정을 저장하세요"), 400
        try:
            cmd = build_cmd(d.get("action"), d.get("vessels"), d.get("fm"), d.get("to"), d.get("dept", "05"))
        except ValueError as e:
            return jsonify(error=str(e)), 400
        lock_fd = _try_lock()
        if lock_fd is None:
            return jsonify(error="이미 실행 중입니다 (한 번에 하나만)"), 409
        with _LOCK:
            _STATE.update(running=True, started=time.time(), cmd=cmd[3], rc=None)
        threading.Thread(target=_run, args=(cmd, lock_fd), daemon=True).start()
        return jsonify(ok=True)

    @bp.get("/api/status")
    def status():
        try:
            with open(LOG) as f:
                log = f.read()[-20000:]
        except OSError:
            log = ""
        fd = _try_lock()   # 다른 워커가 실행 중이면 잠금 실패 = running
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
        with _LOCK:
            return jsonify(running=_STATE["running"] or fd is None, rc=_STATE["rc"], log=log)

    return bp
