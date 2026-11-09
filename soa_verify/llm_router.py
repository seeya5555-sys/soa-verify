
import json
import os
import re
import subprocess


CLAUDE_LIGHT_MODEL = os.environ.get("SOA_CLAUDE_REVIEW_MODEL", "claude-haiku-5-5")
OPENAI_LIGHT_MODEL = os.environ.get("SOA_OPENAI_REVIEW_MODEL", "openai/gpt-6-luna")
LLM_ERROR = {"_llm_error": True}


def _primary_model():
    override = os.environ.get("DEKU_HEAD_MODEL", "").strip()
    if override:
        return override
    cfg = os.path.expanduser("")
    try:
        with open(cfg) as f:
            d = json.load(f)
        return (((d.get("agents") or {}).get("defaults") or {}).get("model") or {}).get("primary") or ""
    except Exception:
        return ""


def review_model():
    """Return the SOA lightweight review model selected from the current Deku head."""
    explicit = os.environ.get("SOA_REVIEW_MODEL", "auto").strip()
    if explicit and explicit.lower() != "auto":
        return explicit
    head = _primary_model().lower()
    if head.startswith("openai/") or head.startswith("gpt"):



        if os.environ.get("SOA_ENABLE_OPENAI_REVIEW") == "1":
            return OPENAI_LIGHT_MODEL
        return CLAUDE_LIGHT_MODEL
    return CLAUDE_LIGHT_MODEL


def _json_obj(text):
    if not text:
        return None
    matches = list(re.finditer(r"\{.*?\}", text, re.S))
    for m in reversed(matches):
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _openclaw_json(text):
    try:
        d = json.loads(text)
        outs = d.get("outputs") or []
        if outs and isinstance(outs[0], dict):
            return _json_obj(outs[0].get("text") or "")
    except Exception:
        pass
    return _json_obj(text)


def _is_openai_model(model):
    m = (model or "").lower()
    return m.startswith("openai/") or m.startswith("gpt")


def llm_json(prompt, timeout=120):
    """Run the selected lightweight model and parse one JSON object.

    Fail-closed contract: caller receives {"_llm_error": true} when the model
    path is unavailable or returns non-JSON. A model-emitted empty JSON object
    remains {}, so callers can distinguish "no finding" from execution failure.
    """
    model = review_model()
    try:
        if _is_openai_model(model):
            if not model.startswith("openai/"):
                model = "openai/" + model
            r = subprocess.run(
                ["openclaw", "infer", "model", "run", "--json", "--model", model, "--prompt", prompt],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            parsed = _openclaw_json(r.stdout)
            return parsed if isinstance(parsed, dict) else dict(LLM_ERROR)
        else:
            r = subprocess.run(
                ["claude", "-p", "--model", model],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            parsed = _json_obj(r.stdout)
            return parsed if isinstance(parsed, dict) else dict(LLM_ERROR)
    except Exception:
        return dict(LLM_ERROR)
