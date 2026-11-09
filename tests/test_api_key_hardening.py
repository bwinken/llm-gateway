"""API key format, the failed-auth throttle and the older-key nudge.

Keys are 256 random bits (``sk-internal-`` + 64 hex). Keys issued in the old
``sk-internal-<unix time>-<8 hex>`` format keep working: auth is an exact
lookup. Their holders are nudged on the dashboard; admins can list them.
An address that sends many *different* invalid keys is locked out
(app/services/auth_throttle.py); one that repeats the same wrong key is not.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from app.models.schema import (
    API_KEY_LENGTH,
    User,
    _generate_api_key,
    is_legacy_api_key,
)
from app.services import auth_throttle
from tests.conftest import auth_header, web_auth_header

LEGACY = "sk-internal-1741651200-a1b2c3d4"


class TestKeyFormat:
    def test_new_keys_carry_256_random_bits(self):
        keys = {_generate_api_key() for _ in range(50)}
        assert len(keys) == 50
        for key in keys:
            assert re.fullmatch(r"sk-internal-[0-9a-f]{64}", key)
            assert len(key) == API_KEY_LENGTH
            assert not is_legacy_api_key(key)

    def test_old_and_hand_set_keys_are_legacy(self):
        assert is_legacy_api_key(LEGACY)
        assert is_legacy_api_key("sk-testkey123")
        assert is_legacy_api_key("")

    def test_new_accounts_get_the_new_format(self, db_session):
        u = User(username="fresh")
        db_session.add(u)
        db_session.commit()
        assert not is_legacy_api_key(u.api_key)

    def test_legacy_keys_still_authenticate(self, client, db_session):
        db_session.add(User(username="old", api_key=LEGACY))
        db_session.commit()
        resp = client.get("/v1/me", headers=auth_header(LEGACY))
        assert resp.status_code == 200

    def test_regenerated_key_is_in_the_new_format(self, client, test_user):
        resp = client.post("/dashboard/refresh-key", headers=web_auth_header(sub=test_user.username))
        assert resp.status_code == 200
        assert not is_legacy_api_key(resp.json()["api_key"])


class TestThrottle:
    @pytest.fixture(autouse=True)
    def _small_limits(self):
        with (
            patch.object(auth_throttle, "DISTINCT_KEYS", 3),
            patch.object(auth_throttle, "WINDOW_S", 600),
            patch.object(auth_throttle, "LOCKOUT_S", 900),
        ):
            yield

    def _try(self, client, key: str):
        return client.get("/v1/me", headers=auth_header(key))

    def test_many_distinct_invalid_keys_lock_the_address(self, client):
        assert self._try(client, "sk-guess-1").status_code == 401
        assert self._try(client, "sk-guess-2").status_code == 401
        assert self._try(client, "sk-guess-3").status_code == 401  # this one locks
        resp = self._try(client, "sk-guess-4")
        assert resp.status_code == 429
        assert 0 < int(resp.headers["Retry-After"]) <= 900

    def test_a_locked_address_is_refused_even_with_a_valid_key(self, client):
        for i in range(3):
            self._try(client, f"sk-guess-{i}")
        # A lookup here would let a guesser keep testing keys while "locked".
        assert self._try(client, "sk-testkey123").status_code == 429

    def test_repeating_the_same_wrong_key_never_locks(self, client):
        # A stale key in a cron job behind the office NAT must not lock out colleagues.
        for _ in range(20):
            assert self._try(client, "sk-stale-key").status_code == 401
        assert self._try(client, "sk-testkey123").status_code == 200

    def test_failures_outside_the_window_are_forgotten(self):
        addr = "203.0.113.7"
        auth_throttle.record_failure(addr, ["a"], now=0)
        auth_throttle.record_failure(addr, ["b"], now=1)
        assert not auth_throttle.record_failure(addr, ["c"], now=1000)  # a, b expired
        assert auth_throttle.retry_after(addr, now=1000) is None

    def test_the_lock_expires(self):
        addr = "203.0.113.8"
        for i, key in enumerate("abc"):
            auth_throttle.record_failure(addr, [key], now=i)
        assert auth_throttle.retry_after(addr, now=10) is not None
        assert auth_throttle.retry_after(addr, now=2 + 900 + 1) is None

    def test_loopback_is_never_locked(self):
        # Untrusted proxy headers make every request look like nginx's 127.0.0.1.
        for i in range(10):
            assert not auth_throttle.record_failure("127.0.0.1", [f"k{i}"])
        assert auth_throttle.retry_after("127.0.0.1") is None

    def test_zero_turns_it_off(self, client):
        with patch.object(auth_throttle, "DISTINCT_KEYS", 0):
            for i in range(10):
                assert self._try(client, f"sk-guess-{i}").status_code == 401

    def test_failure_log_names_the_client(self, client):
        with patch("app.core.deps.logger") as log:
            self._try(client, "sk-nope")
        line = log.warning.call_args
        assert "client=" in line.args[0]
        assert "testclient" in line.args


class TestLegacyNudge:
    def test_dashboard_nudges_a_legacy_key_holder(self, client, test_user):
        # The conftest user's hand-set key counts as legacy.
        body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert 'id="legacyKeyNotice"' in body

    def test_no_nudge_for_a_current_key(self, client, db_session, test_user):
        test_user.api_key = _generate_api_key()
        db_session.add(test_user)
        db_session.commit()
        body = client.get("/dashboard", headers=web_auth_header(sub=test_user.username)).text
        assert 'id="legacyKeyNotice"' not in body

    def test_admin_can_list_accounts_with_legacy_keys(self, client, db_session, admin_user, test_user):
        admin_user.api_key = _generate_api_key()
        db_session.add(admin_user)
        db_session.add(User(username="oldtimer", api_key=LEGACY))
        db_session.commit()
        resp = client.get(
            "/admin/user-table", params={"kind": "users", "status": "legacy_key"},
            headers=web_auth_header(sub=admin_user.username, scopes=["admin"]),
        )
        assert resp.status_code == 200
        assert 'data-username="oldtimer"' in resp.text
        assert 'data-username="testuser"' in resp.text      # sk-testkey123
        assert 'data-username="admin"' not in resp.text
