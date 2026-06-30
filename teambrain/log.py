"""One logger for the whole app — stderr only (stdout is the MCP JSON-RPC wire,
logging there would corrupt the protocol).

Warnings/errors always show. The verbose per-call tracing (DB queries, the
vectors that came back) is gated behind ``TEAMBRAIN_LOG`` so it's off by default:

    TEAMBRAIN_LOG=1   # show search queries + results + synth decisions
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("team-brain")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("[team-brain] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.DEBUG if os.environ.get("TEAMBRAIN_LOG") else logging.WARNING)
    log.propagate = False


def row_brief(m: dict) -> str:
    """Compact one-line view of a retrieved memory for debug logs: id · score ·
    title. Score key varies by store (score/rank/distance) — show whichever is
    present so you can see *what the vectors actually ranked*."""
    score = m.get("score", m.get("rank", m.get("distance")))
    score = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
    return f"{m.get('id')}·{score}·{str(m.get('title'))[:50]}"
