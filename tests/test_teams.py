"""Teams bridge: HMAC fail-closed, mention/role parsing, ACL-gated answers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from teambrain import teams

SECRET = base64.b64encode(b"webhook-shared-token").decode()


def _sign(body: bytes, secret_b64: str = SECRET) -> str:
    key = base64.b64decode(secret_b64)
    return "HMAC " + base64.b64encode(hmac.new(key, body, hashlib.sha256).digest()).decode()


def _body(text: str) -> bytes:
    return json.dumps({"type": "message", "text": text}).encode()


def test_signature_fail_closed():
    body = _body("<at>brain</at> hello")
    assert teams.verify_signature(body, _sign(body), SECRET)
    assert not teams.verify_signature(body, _sign(body), None)          # no secret
    assert not teams.verify_signature(body, None, SECRET)               # no header
    assert not teams.verify_signature(body, "Bearer xyz", SECRET)       # wrong scheme
    assert not teams.verify_signature(b"tampered" + body, _sign(body), SECRET)
    status, msg = teams.handle(body, "HMAC bogus", secret=SECRET)
    assert status == 401 and "signature" in msg["text"]


def test_clean_text_and_role_prefix():
    assert teams.clean_text("<at>team-brain</at> why &amp; how?") == "why & how?"
    assert teams.parse_question("as tester: what breaks?", "developer") == \
        ("tester", "what breaks?")
    assert teams.parse_question("what breaks?", "developer") == \
        ("developer", "what breaks?")


def test_handle_answers_with_citations(temp_store):
    temp_store.save("Counterparty rule",
                    "Match settlement rows on the counterparty account.",
                    tier="semantic", tags=["openspec"], source="openspec",
                    namespace="team-eng")
    body = _body("<at>brain</at> how do we match settlement rows?")
    status, msg = teams.handle(body, _sign(body), secret=SECRET,
                               namespace="team-eng", groups=None)
    assert status == 200
    assert "counterparty account" in msg["text"].lower()
    assert "Sources" in msg["text"] and "Counterparty rule" in msg["text"]


def test_handle_acl_fail_closed(temp_store):
    temp_store.save("Restricted note", "The settlement cutoff is 16:30 CET.",
                    tier="semantic", tags=["acl:ops"], source="manual",
                    namespace="team-eng")
    body = _body("<at>brain</at> what is the settlement cutoff?")
    _, anon = teams.handle(body, _sign(body), secret=SECRET,
                           namespace="team-eng", groups=None)
    assert "16:30" not in anon["text"]                    # unknown asker: denied
    _, ops = teams.handle(body, _sign(body), secret=SECRET,
                          namespace="team-eng", groups=["ops"])
    assert "16:30" in ops["text"]                         # channel-level grant


def test_handle_empty_and_unknown_role(temp_store):
    body = _body("<at>brain</at>   ")
    status, msg = teams.handle(body, _sign(body), secret=SECRET)
    assert status == 200 and "Ask me" in msg["text"]
    body = _body("<at>brain</at> as wizard: abracadabra")
    _, msg = teams.handle(body, _sign(body), secret=SECRET, namespace="team-eng")
    assert "unknown role" in msg["text"]
