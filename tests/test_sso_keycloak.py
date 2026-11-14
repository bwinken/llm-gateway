"""Keycloak as the SSO provider (``SSO_PROVIDER=keycloak``,
app/core/keycloak.py): the token check, the signing keys, and that the
switch is one setting in front of one function (``auth._decode_jwt``).

Tokens here are really signed (RS256) and really verified; only the HTTP
read of the realm's key set is replaced."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from sqlmodel import select

from app.core import auth, keycloak
from app.models.schema import User

# The conftest replaces ``auth._decode_jwt`` in every test; this is the real one.
_REAL_DECODE = auth._decode_jwt
_REAL_FETCH = keycloak._fetch_jwks

ISSUER = "https://sso.example.test/realms/company"
CLIENT = "llm_gateway"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"

_REALM_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ROTATED_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwk(private_key, kid: str, **extra) -> dict:
    jwk = pyjwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    return {**jwk, "kid": kid, "alg": "RS256", "use": "sig", **extra}


def _jwks(*keys: dict) -> dict:
    return {"keys": list(keys)}


REALM_JWKS = _jwks(
    _jwk(_REALM_KEY, "realm-1"),
    # A realm publishes its encryption key in the same document.
    _jwk(_OTHER_KEY, "realm-enc", alg="RSA-OAEP", use="enc"),
)


def _claims(**over) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "exp": now + 300,
        "iat": now,
        "typ": "Bearer",
        "azp": CLIENT,
        "aud": "account",
        "sub": "f3b0c1de-0000-4000-8000-000000000001",
        "preferred_username": "alice",
        "name": "Alice Chen",
        "org_code": "RD-7",
        "realm_access": {"roles": ["offline_access", "default-roles-company"]},
        "resource_access": {CLIENT: {"roles": ["read"]}, "account": {"roles": ["view-profile"]}},
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=_REALM_KEY, kid: str | None = "realm-1", alg: str = "RS256", **over) -> str:
    return pyjwt.encode(_claims(**over), key, algorithm=alg, headers={"kid": kid} if kid else None)


@pytest.fixture(autouse=True)
def _keycloak(monkeypatch):
    """Keycloak configured, nothing cached, the realm's key set served."""
    for name in ("KEYCLOAK_JWKS_URL", "KEYCLOAK_USERNAME_CLAIM", "KEYCLOAK_ORG_CLAIM", "KEYCLOAK_CA_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SSO_PROVIDER", "keycloak")
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT)
    keycloak.reset_keys()
    with patch.object(keycloak, "_fetch_jwks", return_value=REALM_JWKS) as fetch:
        yield fetch
    keycloak.reset_keys()


class TestToken:
    def test_an_access_token_comes_out_in_authcenters_shape(self):
        # Callers read sub / scopes / display_name / org_code and nothing else.
        assert keycloak.decode(_token()) == {
            "sub": "alice", "scopes": ["read"], "display_name": "Alice Chen", "org_code": "RD-7",
        }

    def test_the_account_is_the_username_not_keycloaks_uuid(self):
        # Existing accounts are found by username; Keycloak's sub is a UUID.
        assert keycloak.decode(_token())["sub"] == "alice"
        assert keycloak.decode(_token(preferred_username=None)) is None
        assert keycloak.decode(_token(preferred_username="  ")) is None

    def test_the_claims_to_read_are_settings(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_USERNAME_CLAIM", "employee_id")
        monkeypatch.setenv("KEYCLOAK_ORG_CLAIM", "department")
        got = keycloak.decode(_token(employee_id="E0042", department=["QA-1", "QA-2"]))
        # A multivalued user attribute arrives as a list.
        assert (got["sub"], got["org_code"]) == ("E0042", "QA-1")

    def test_missing_names_are_empty_never_null(self):
        got = keycloak.decode(_token(name=None, org_code=None))
        assert (got["display_name"], got["org_code"]) == ("", "")

    def test_scopes_are_this_clients_roles(self):
        got = keycloak.decode(_token(resource_access={CLIENT: {"roles": ["read", "admin"]}}))
        assert got["scopes"] == ["read", "admin"]

    def test_a_realm_role_or_another_clients_role_grants_nothing(self):
        # A realm-wide "admin" is somebody else's permission for something else.
        got = keycloak.decode(_token(
            realm_access={"roles": ["admin", "read"]},
            resource_access={"some-other-app": {"roles": ["admin", "read"]}},
            scope="openid admin read",
        ))
        assert got["scopes"] == []

    def test_no_roles_at_all(self):
        assert keycloak.decode(_token(resource_access=None))["scopes"] == []
        assert keycloak.decode(_token(resource_access={CLIENT: {"roles": "admin"}}))["scopes"] == []


class TestWhatIsRefused:
    def test_another_issuer(self):
        assert keycloak.decode(_token(iss="https://sso.example.test/realms/other")) is None

    def test_expired(self):
        assert keycloak.decode(_token(exp=int(time.time()) - 60)) is None

    def test_no_expiry(self):
        assert keycloak.decode(_token(exp=None)) is None

    def test_issued_for_this_gateway_by_azp_or_by_aud(self):
        # Keycloak names its own client in azp; a second client (a terminal
        # tool's) reaches the gateway through an audience mapper.
        assert keycloak.decode(_token(azp=CLIENT, aud="account")) is not None
        assert keycloak.decode(_token(azp="gateway-cli", aud=["account", CLIENT])) is not None
        assert keycloak.decode(_token(azp="gateway-cli", aud=CLIENT)) is not None

    def test_a_token_for_another_client(self):
        assert keycloak.decode(_token(azp="some-other-app", aud="account")) is None
        assert keycloak.decode(_token(azp="some-other-app", aud=None)) is None
        assert keycloak.decode(_token(azp=None, aud=["account", "some-other-app"])) is None

    def test_an_id_token_is_not_an_access_token(self):
        # Same key, same person, aud = the client: still not a permission.
        assert keycloak.decode(_token(typ="ID", aud=CLIENT)) is None
        assert keycloak.decode(_token(typ=None)) is None

    def test_signed_by_someone_else_under_the_realms_key_id(self):
        assert keycloak.decode(_token(key=_OTHER_KEY, kid="realm-1")) is None

    def test_no_key_id_or_an_unknown_one(self):
        assert keycloak.decode(_token(kid=None)) is None
        assert keycloak.decode(_token(key=_OTHER_KEY, kid="made-up")) is None

    def test_the_encryption_key_does_not_sign(self):
        assert keycloak.decode(_token(key=_OTHER_KEY, kid="realm-enc")) is None

    def test_a_symmetric_signature_made_from_the_public_key(self):
        # Algorithm confusion: HS256 keyed with what everyone can download.
        # (PyJWT refuses to sign that way, so the token is put together by hand.)
        pem = _REALM_KEY.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "realm-1"}).encode())
        signing_input = head + b"." + b64(json.dumps(_claims()).encode())
        signature = b64(hmac.new(pem, signing_input, hashlib.sha256).digest())
        assert keycloak.decode((signing_input + b"." + signature).decode()) is None

    def test_the_tokens_algorithm_must_be_the_keys(self):
        ec_key = ec.generate_private_key(ec.SECP256R1())
        assert keycloak.decode(_token(key=ec_key, alg="ES256")) is None
        assert keycloak.decode(_token(alg="RS512")) is None

    def test_garbage_and_an_api_key_sent_by_mistake(self):
        for value in ("", "sk-your-api-key", "a.b.c", "not a token"):
            assert keycloak.decode(value) is None

    def test_no_issuer_configured_refuses_everything(self, monkeypatch, _keycloak):
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL")
        assert keycloak.decode(_token()) is None
        _keycloak.assert_not_called()


class TestSigningKeys:
    def test_read_once_from_the_realms_certs_endpoint(self, _keycloak):
        for _ in range(5):
            assert keycloak.decode(_token()) is not None
        _keycloak.assert_called_once_with(JWKS_URL, "")

    def test_the_key_set_is_really_read_over_http(self, monkeypatch):
        # The one test without the patched read: a local server plays the realm.
        import http.server
        import threading

        class Certs(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps(REALM_JWKS).encode() if self.path == "/certs" else b"{}"
                self.send_response(200 if self.path == "/certs" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Certs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            assert keycloak._parse_jwks(_REAL_FETCH(f"{base}/certs", "")).keys() == {"realm-1"}
            with pytest.raises(Exception):
                _REAL_FETCH(f"{base}/missing", "")
        finally:
            server.shutdown()
            server.server_close()

    def test_the_address_and_ca_can_be_set(self, monkeypatch, _keycloak):
        monkeypatch.setenv("KEYCLOAK_JWKS_URL", "http://keycloak.internal:8080/realms/company/protocol/openid-connect/certs")
        monkeypatch.setenv("KEYCLOAK_CA_FILE", "/etc/ssl/company-ca.pem")
        assert keycloak.decode(_token()) is not None
        _keycloak.assert_called_once_with(
            "http://keycloak.internal:8080/realms/company/protocol/openid-connect/certs", "/etc/ssl/company-ca.pem",
        )

    def test_unknown_key_ids_cannot_make_the_gateway_call_keycloak_each_time(self, _keycloak):
        # POST /auth/api-key is reachable without a login.
        assert keycloak.decode(_token()) is not None
        for n in range(20):
            assert keycloak.decode(_token(key=_OTHER_KEY, kid=f"made-up-{n}")) is None
        assert _keycloak.call_count == 1

    def test_a_rotated_key_is_picked_up_after_the_pause(self, _keycloak):
        assert keycloak.decode(_token()) is not None
        _keycloak.return_value = _jwks(_jwk(_ROTATED_KEY, "realm-2"))
        new = _token(key=_ROTATED_KEY, kid="realm-2")
        assert keycloak.decode(new) is None  # read a moment ago: not again yet
        with patch.object(keycloak, "_REFETCH_MIN_S", 0.0):
            assert keycloak.decode(new) is not None
            # ... and the key the realm no longer lists is no longer trusted.
            assert keycloak.decode(_token()) is None
        assert _keycloak.call_count == 3

    def test_a_removed_key_stops_working_when_the_set_is_read_again(self, _keycloak):
        assert keycloak.decode(_token()) is not None
        _keycloak.return_value = _jwks(_jwk(_ROTATED_KEY, "realm-2"))
        with patch.object(keycloak, "_KEYS_MAX_AGE_S", 0.0), patch.object(keycloak, "_REFETCH_MIN_S", 0.0):
            assert keycloak.decode(_token()) is None

    def test_keycloak_unreachable_keeps_the_keys_already_known(self, _keycloak):
        assert keycloak.decode(_token()) is not None
        _keycloak.side_effect = OSError("connection refused")
        with patch.object(keycloak, "_KEYS_MAX_AGE_S", 0.0), patch.object(keycloak, "_REFETCH_MIN_S", 0.0):
            assert keycloak.decode(_token()) is not None
        assert _keycloak.call_count == 2

    def test_unreachable_at_the_first_token_refuses_and_tries_again_later(self, _keycloak):
        _keycloak.side_effect = OSError("connection refused")
        assert keycloak.decode(_token()) is None
        assert keycloak.decode(_token()) is None
        assert _keycloak.call_count == 1  # not once per token while it is down
        _keycloak.side_effect = None
        with patch.object(keycloak, "_REFETCH_MIN_S", 0.0):
            assert keycloak.decode(_token()) is not None

    def test_a_key_set_without_a_signing_key_is_a_failed_read(self, _keycloak):
        _keycloak.return_value = _jwks(_jwk(_OTHER_KEY, "realm-enc", alg="RSA-OAEP", use="enc"))
        assert keycloak.decode(_token()) is None
        _keycloak.return_value = {"error": "not a key set"}
        with patch.object(keycloak, "_REFETCH_MIN_S", 0.0):
            assert keycloak.decode(_token()) is None

    def test_never_read_on_the_event_loop(self, _keycloak):
        # AuditMiddleware names its actor on the loop: a blocking HTTP call
        # there would stall every stream on the worker.
        async def on_the_loop(token):
            return keycloak.decode(token)

        assert asyncio.run(on_the_loop(_token())) is None
        _keycloak.assert_not_called()
        # Once a handler (a threadpool thread) has read them, the loop
        # answers from what is known.
        assert keycloak.decode(_token()) is not None
        assert asyncio.run(on_the_loop(_token()))["sub"] == "alice"
        assert _keycloak.call_count == 1


class TestTheSwitch:
    def test_authcenter_is_the_default(self, monkeypatch):
        monkeypatch.delenv("SSO_PROVIDER")
        assert auth.sso_provider() == "authcenter"
        monkeypatch.setenv("SSO_PROVIDER", "  ")
        assert auth.sso_provider() == "authcenter"
        monkeypatch.setenv("SSO_PROVIDER", " KeyCloak ")
        assert auth.sso_provider() == "keycloak"

    def test_by_default_a_keycloak_token_is_not_accepted(self, monkeypatch, _keycloak, tmp_path):
        # AuthCenter's check, with AuthCenter's key file: Keycloak is not asked.
        monkeypatch.delenv("SSO_PROVIDER")
        pem = tmp_path / "public.pem"
        pem.write_bytes(pyjwt.algorithms.RSAAlgorithm.to_jwk(_OTHER_KEY.public_key()).encode())
        with patch.object(auth, "AUTH_CENTER_PUBLIC_KEY_PATH", str(pem)), patch.object(auth, "_pk_cache", (0.0, "")):
            assert _REAL_DECODE(_token()) is None
        _keycloak.assert_not_called()

    def test_with_keycloak_the_one_check_is_keycloaks(self):
        assert _REAL_DECODE(_token())["sub"] == "alice"
        assert _REAL_DECODE(_token(iss="https://elsewhere.test/realms/company")) is None

    def test_an_unknown_provider_accepts_nothing(self, monkeypatch, _keycloak):
        monkeypatch.setenv("SSO_PROVIDER", "okta")
        assert _REAL_DECODE(_token()) is None
        _keycloak.assert_not_called()

    def test_the_gateway_does_not_start_on_a_setting_that_cannot_work(self, monkeypatch):
        auth.check_sso_settings()  # keycloak, with its issuer
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL")
        with pytest.raises(RuntimeError, match="KEYCLOAK_ISSUER_URL"):
            auth.check_sso_settings()
        monkeypatch.setenv("SSO_PROVIDER", "okta")
        with pytest.raises(RuntimeError, match="okta"):
            auth.check_sso_settings()
        monkeypatch.delenv("SSO_PROVIDER")
        auth.check_sso_settings()  # authcenter needs nothing new


def _bearer(**over) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(**over)}"}


class TestSignedInThroughKeycloak:
    """The pages and the key exchange behind the real check, Keycloak's."""

    @pytest.fixture(autouse=True)
    def _real_check(self):
        with patch("app.core.auth._decode_jwt", _REAL_DECODE):
            yield

    def test_a_first_visit_creates_the_account_under_the_username(self, client, db_session):
        assert client.get("/dashboard", headers=_bearer()).status_code == 200
        db_session.expire_all()
        user = db_session.exec(select(User).where(User.username == "alice")).one()
        assert (user.display_name, user.org_code) == ("Alice Chen", "RD-7")

    def test_an_existing_account_is_found_by_the_same_username(self, client, db_session, test_user):
        # The switch keeps everyone's account, key and usage.
        resp = client.post("/auth/api-key", headers=_bearer(preferred_username="testuser"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["api_key"] == "sk-testkey123"
        db_session.expire_all()
        assert len(db_session.exec(select(User).where(User.username == "testuser")).all()) == 1

    def test_the_read_role_opens_the_dashboard_and_nothing_without_it(self, client):
        assert client.get("/dashboard", headers=_bearer()).status_code == 200
        none = _bearer(resource_access={CLIENT: {"roles": []}})
        assert client.get("/dashboard", headers=none).status_code == 403
        assert client.post("/auth/api-key", headers=none).status_code == 403

    def test_admin_is_the_clients_admin_role(self, client):
        assert client.get("/admin", headers=_bearer()).status_code == 403
        realm_admin = _bearer(realm_access={"roles": ["admin"]})
        assert client.get("/admin", headers=realm_admin).status_code == 403
        admin = _bearer(resource_access={CLIENT: {"roles": ["admin"]}})
        assert client.get("/admin", headers=admin).status_code == 200

    def test_a_token_of_the_other_provider_is_a_401(self, client):
        from tests.conftest import web_auth_header

        assert client.get("/dashboard", headers=web_auth_header(sub="testuser")).status_code == 401
        assert client.post("/auth/api-key", headers=web_auth_header(sub="testuser")).status_code == 401

    def test_the_audit_log_names_the_keycloak_user(self, client, db_session, test_user):
        from app.models.schema import AuditLog

        resp = client.post("/auth/api-key", headers=_bearer(preferred_username="testuser"))
        assert resp.status_code == 200, resp.text
        db_session.expire_all()
        rows = db_session.exec(select(AuditLog).where(AuditLog.path == "/auth/api-key")).all()
        assert [r.actor for r in rows] == ["testuser"]
