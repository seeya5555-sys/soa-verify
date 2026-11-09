"""Stub for the upstream group-config module.

Upstream (the maintainer's private deployment) runs SOA review by vessel *groups* synced from a web UI.
soa-verify selects vessels directly from your SVMS "My Vessel" list (see cli.py), so group functions
are not used. They fail loudly if anything calls them.
"""


def _unused(*_a, **_k):
    raise RuntimeError("group-based review is not part of soa-verify; use `python3 -m soa_verify verify`")


load_groups = group_keys = resolve_targets = uncovered_vessels = is_thorough_group = _unused
