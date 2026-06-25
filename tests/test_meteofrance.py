"""Tests for the Météo-France AROME API-key validation (offline JWT decode).

No network and no real key: we forge minimal JWTs (header.payload.sig) carrying the fields
``check_arome_key`` reads (exp + subscribedAPIs). See wind/meteofrance.py.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from sillage.wind.meteofrance import AROME_API_CONTEXT, check_arome_key, renewal_text


def _make_key(*, exp: datetime | None, contexts=(AROME_API_CONTEXT,), owner="owner-test") -> str:
    payload = {"application": {"owner": owner},
               "subscribedAPIs": [{"context": c} for c in contexts]}
    if exp is not None:
        payload["exp"] = int(exp.timestamp())
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def test_missing_key():
    s = check_arome_key(None)
    assert s.reason == "missing" and s.ok is False
    assert check_arome_key("   ").reason == "missing"


def test_malformed_key():
    s = check_arome_key("not-a-jwt")
    assert s.reason == "malformed" and s.ok is False


def test_valid_key_far_future():
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    tok = _make_key(exp=now + timedelta(days=1096))
    s = check_arome_key(tok, now=now)
    assert s.ok is True and s.reason == "ok"
    assert s.owner == "owner-test"
    assert s.days_left and s.days_left > 1000


def test_expired_key():
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    tok = _make_key(exp=now - timedelta(days=1))
    s = check_arome_key(tok, now=now)
    assert s.ok is False and s.reason == "expired"


def test_expiring_soon_is_ok_but_flagged():
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    tok = _make_key(exp=now + timedelta(days=10))
    s = check_arome_key(tok, now=now, warn_days=30)
    assert s.ok is True and s.reason == "expiring_soon"
    assert s.days_left == 10


def test_not_subscribed_to_arome():
    now = datetime(2026, 6, 24, tzinfo=timezone.utc)
    tok = _make_key(exp=now + timedelta(days=365), contexts=("/public/arpege/1.0",))
    s = check_arome_key(tok, now=now)
    assert s.ok is False and s.reason == "not_subscribed"


def test_renewal_text_has_account_hints_and_env(monkeypatch):
    monkeypatch.setenv("METEOFRANCE_ACCOUNT_LOGIN", "login-local")
    monkeypatch.setenv("METEOFRANCE_ACCOUNT_EMAIL", "email-local@example.test")
    txt = renewal_text()
    assert "login-local" in txt and "email-local@example.test" in txt
    assert "METEOFRANCE_API_KEY" in txt and "portail-api.meteofrance.fr" in txt
