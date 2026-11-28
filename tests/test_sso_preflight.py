"""The preflight for signing in through Keycloak (``app/services/sso_preflight.py``,
``scripts/keycloak_preflight.py``): each check against the situation it is
for. The certificate checks run against a real TLS server on localhost with
a CA made here; Keycloak's HTTP answers are a mock transport."""

from __future__ import annotations

import inspect
import json
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import jwt as pyjwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.core import keycloak
from app.models.schema import User
from app.services import offboarding
from app.services import sso_preflight as preflight

ISSUER = "https://sso.example.test/realms/company"
CLIENT = "llm_gateway"
KC = {
    "issuer": ISSUER, "client_id": CLIENT, "jwks_url": f"{ISSUER}/protocol/openid-connect/certs",
    "username_claim": "preferred_username", "org_claim": "org_code", "ca_file": "",
}


def _by_name(checks: list[preflight.Check]) -> dict[str, preflight.Check]:
    return {c.name: c for c in checks}


def _statuses(checks: list[preflight.Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


# ── Certificates made here ──

def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert(subject: str, key, issuer_cert=None, issuer_key=None, *, ca=False, dns: str = "", days: int = 365,
          start_days_ago: int = 1, key_ids: bool = True):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_cert.subject if issuer_cert is not None else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=start_days_ago))
        .not_valid_after(now - timedelta(days=start_days_ago) + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if dns:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False)
    if key_ids:  # what RFC 5280 asks for, and Python 3.13+ insists on
        signer = (issuer_key or key).public_key()
        builder = builder.add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        builder = builder.add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer), critical=False)
    if ca:
        builder = builder.add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False), critical=True)
    return builder.sign(issuer_key or key, hashes.SHA256())


def _pem(cert) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """A CA, another CA, and a server certificate for ``localhost``."""
    d = tmp_path_factory.mktemp("pki")
    ca_key, other_key, leaf_key = _key(), _key(), _key()
    ca = _cert("Test Company Root CA", ca_key, ca=True)
    other = _cert("Somebody Else CA", other_key, ca=True)
    leaf = _cert("localhost", leaf_key, ca, ca_key, dns="localhost")
    wrong_host = _cert("keycloak.internal", leaf_key, ca, ca_key, dns="keycloak.internal")
    files = {
        "ca": d / "ca.pem", "other": d / "other.pem", "leaf": d / "leaf.pem", "key": d / "key.pem",
        "chain": d / "chain.pem", "wrong_chain": d / "wrong-chain.pem",
    }
    files["ca"].write_bytes(_pem(ca))
    files["other"].write_bytes(_pem(other))
    files["leaf"].write_bytes(_pem(leaf))
    files["key"].write_bytes(_key_pem(leaf_key))
    files["chain"].write_bytes(_pem(leaf) + _pem(ca))
    files["wrong_chain"].write_bytes(_pem(wrong_host) + _pem(ca))
    return {**{k: str(v) for k, v in files.items()}, "dir": d, "ca_cert": ca, "ca_key": ca_key}


def _serve(chain_file: str, key_file: str):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(chain_file, key_file)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def loop():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    tls.settimeout(1.0)
                    try:
                        tls.recv(1)  # until the client goes
                    except OSError:
                        pass
            except (OSError, ssl.SSLError):
                pass  # a client that refused the certificate

    threading.Thread(target=loop, daemon=True).start()
    return listener, port


@pytest.fixture(scope="module")
def tls_server(pki):
    listener, port = _serve(pki["chain"], pki["key"])
    yield f"https://localhost:{port}/realms/company"
    listener.close()


@pytest.fixture(scope="module")
def tls_server_wrong_name(pki):
    listener, port = _serve(pki["wrong_chain"], pki["key"])
    yield f"https://localhost:{port}/realms/company"
    listener.close()


# ── The CA file ──

class TestCaFile:
    def test_a_ca_certificate(self, pki):
        [check] = preflight.inspect_ca_file(pki["ca"])
        assert check.status == "ok" and "Test Company Root CA" in check.detail and "1 certificate" in check.detail

    def test_a_path_that_is_not_there(self, pki):
        [check] = preflight.inspect_ca_file(str(pki["dir"] / "nope.pem"))
        assert check.status == "fail" and "does not exist" in check.detail

    def test_the_servers_own_certificate_is_not_a_ca(self, pki):
        [check] = preflight.inspect_ca_file(pki["leaf"])
        assert check.status == "warn" and "None of them is a CA" in check.detail

    def test_a_binary_certificate_says_how_to_convert_it(self, pki):
        der = pki["dir"] / "ca.cer"
        der.write_bytes(pki["ca_cert"].public_bytes(serialization.Encoding.DER))
        [check] = preflight.inspect_ca_file(str(der))
        assert check.status == "fail" and "DER" in check.detail and "openssl x509 -inform der" in check.fix

    def test_a_private_key_in_it_is_a_failure(self, pki):
        both = pki["dir"] / "with-key.pem"
        both.write_bytes(_pem(pki["ca_cert"]) + _key_pem(pki["ca_key"]))
        statuses = [c.status for c in preflight.inspect_ca_file(str(both))]
        assert "fail" in statuses

    def test_an_expired_ca(self, pki):
        key = _key()
        old = pki["dir"] / "old.pem"
        old.write_bytes(_pem(_cert("Old CA", key, ca=True, days=10, start_days_ago=30)))
        [check] = preflight.inspect_ca_file(str(old))
        assert check.status == "fail" and "expired" in check.detail

    def test_a_hosts_bundle_with_expired_certificates_in_it_is_fine(self, pki):
        # /etc/ssl/certs/ca-certificates.crt always holds a few that have
        # expired, and certificates that are nobody's CA. Verification skips
        # them: reporting them as a failure sent an admin looking for a
        # problem that was not there.
        bundle = pki["dir"] / "ca-certificates.crt"
        key = _key()
        others = [_cert(f"Public Root {i}", key, ca=True) for i in range(11)]
        dead = [_cert("Old Root", key, ca=True, days=10, start_days_ago=30),
                _cert("*.docker.test", key, days=10, start_days_ago=30)]
        bundle.write_bytes(b"".join(_pem(c) for c in [*others, *dead, pki["ca_cert"]]))
        [check] = preflight.inspect_ca_file(str(bundle))
        assert check.status == "ok"
        assert "a bundle of 14 certificates" in check.detail and "2 of them have expired and are ignored" in check.detail

    def test_an_expired_certificate_beside_a_valid_one_in_a_small_file_is_pointed_out(self, pki):
        both = pki["dir"] / "two.pem"
        both.write_bytes(_pem(pki["ca_cert"]) + _pem(_cert("Old Root", _key(), ca=True, days=10, start_days_ago=30)))
        [check] = preflight.inspect_ca_file(str(both))
        assert check.status == "warn" and "Old Root" in check.detail

    def test_the_bundle_is_judged_by_whether_keycloak_verifies_with_it(self, tls_server, pki):
        bundle = pki["dir"] / "bundle-with-dead.crt"
        key = _key()
        bundle.write_bytes(b"".join(_pem(c) for c in [
            *[_cert(f"Public Root {i}", key, ca=True) for i in range(11)],
            _cert("Old Root", key, ca=True, days=10, start_days_ago=30), pki["ca_cert"],
        ]))
        kc = {**KC, "issuer": tls_server}
        env = {"OIDC_ISSUER_URL": tls_server, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: str(bundle)}
        checks = preflight.check_oauth2_proxy(env, kc, "OAUTH2_PROXY_PROVIDER_CA_FILES:\n", public=False)
        assert not [c for c in checks if c.status == "fail"]
        assert _by_name(checks)["Keycloak with that CA"].status == "ok"

    def test_a_file_that_is_cut_off(self, pki):
        cut = pki["dir"] / "cut.pem"
        cut.write_bytes(_pem(pki["ca_cert"])[:200])
        [check] = preflight.inspect_ca_file(str(cut))
        assert check.status == "fail"


# ── The handshake ──

class TestTls:
    def test_an_internal_ca_this_host_does_not_trust_names_the_ca_to_get(self, tls_server):
        [check] = preflight.probe_tls(tls_server, "")
        assert check.status == "fail" and "not trusted" in check.detail
        # Which CA to ask for, and that oauth2-proxy needs it too.
        assert "Test Company Root CA" in check.fix and "KEYCLOAK_CA_FILE" in check.fix
        assert "oauth2-proxy" in check.fix

    def test_with_the_right_ca_file_it_is_trusted(self, tls_server, pki):
        checks = preflight.probe_tls(tls_server, pki["ca"])
        assert checks[0].status == "ok" and "the CA in KEYCLOAK_CA_FILE" in checks[0].detail

    def test_with_another_ca_the_file_is_said_to_be_the_wrong_one(self, tls_server, pki):
        [check] = preflight.probe_tls(tls_server, pki["other"])
        assert check.status == "fail"
        assert "does not hold the CA" in check.fix and "Test Company Root CA" in check.fix

    def test_a_certificate_for_another_name(self, tls_server_wrong_name, pki):
        [check] = preflight.probe_tls(tls_server_wrong_name, pki["ca"])
        assert check.status == "fail" and "not for localhost" in check.detail
        assert "keycloak.internal" in check.detail

    def test_a_server_certificate_about_to_expire_is_said(self, pki):
        key = _key()
        soon = _cert("localhost", key, pki["ca_cert"], pki["ca_key"], dns="localhost", days=6, start_days_ago=1)
        chain, key_file = pki["dir"] / "soon.pem", pki["dir"] / "soon.key"
        chain.write_bytes(_pem(soon) + _pem(pki["ca_cert"]))
        key_file.write_bytes(_key_pem(key))
        listener, port = _serve(str(chain), str(key_file))
        try:
            checks = preflight.probe_tls(f"https://localhost:{port}/realms/x", pki["ca"])
        finally:
            listener.close()
        assert _statuses(checks)["server certificate"] == "warn"

    def test_the_right_ca_but_a_certificate_python_refuses(self, pki):
        # An older CA setup issues certificates without the key identifiers
        # RFC 5280 asks for. Python 3.13+ refuses them by default, and so
        # does the gateway: it must not be reported as "wrong CA file".
        key = _key()
        loose = _cert("localhost", key, pki["ca_cert"], pki["ca_key"], dns="localhost", key_ids=False)
        chain, key_file = pki["dir"] / "loose.pem", pki["dir"] / "loose.key"
        chain.write_bytes(_pem(loose) + _pem(pki["ca_cert"]))
        key_file.write_bytes(_key_pem(key))
        listener, port = _serve(str(chain), str(key_file))
        try:
            checks = preflight.probe_tls(f"https://localhost:{port}/realms/x", pki["ca"])
        finally:
            listener.close()
        strict = ssl.create_default_context().verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict:
            assert checks[0].status == "fail" and "the CA is trusted" in checks[0].detail
            assert "RFC 5280" in checks[0].fix and "does not hold the CA" not in checks[0].fix
        else:  # an older Python accepts it, as the gateway there does
            assert checks[0].status == "ok"

    def test_nothing_listening_is_not_called_a_certificate_problem(self):
        with socket.create_server(("127.0.0.1", 0)) as s:
            port = s.getsockname()[1]
        [check] = preflight.probe_tls(f"https://localhost:{port}/realms/x", "")
        assert check.status == "warn" and "no direct connection" in check.detail

    def test_plain_http_has_no_certificate(self):
        [check] = preflight.probe_tls("http://sso.example.test/realms/x", "")
        assert check.status == "skip"

    def test_the_ca_the_server_presents_can_be_saved_and_then_works(self, tls_server, pki, tmp_path):
        certs = preflight.ca_certificates_presented(tls_server)
        if not hasattr(ssl.SSLSocket, "get_unverified_chain"):  # Python < 3.13 sees the leaf only
            assert certs == []
            return
        assert [preflight._cert_name(c) for c in certs] == ["CN=Test Company Root CA"]
        saved = tmp_path / "saved.pem"
        saved.write_bytes(preflight.pem_bundle(certs))
        assert preflight.probe_tls(tls_server, str(saved))[0].status == "ok"
        assert len(preflight._fingerprint(certs[0]).split(":")) == 32

    def test_a_proxy_in_the_way_is_said(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
        monkeypatch.setenv("https_proxy", "http://proxy.test:3128")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        [check] = preflight.check_proxy("sso.example.test")
        assert check.status == "warn" and "NO_PROXY" in check.fix
        monkeypatch.setenv("NO_PROXY", "sso.example.test")
        monkeypatch.setenv("no_proxy", "sso.example.test")
        assert preflight.check_proxy("sso.example.test") == []


# ── Settings ──

class TestSettings:
    def test_no_issuer(self):
        checks = preflight.check_settings({**KC, "issuer": ""}, "authcenter")
        assert _statuses(checks)["KEYCLOAK_ISSUER_URL"] == "fail"

    @pytest.mark.parametrize("issuer", ["https://sso.example.test", "https://sso.example.test/realms/a/b"])
    def test_not_a_realm_url(self, issuer):
        assert _statuses(preflight.check_settings({**KC, "issuer": issuer}, "authcenter"))["KEYCLOAK_ISSUER_URL"] == "fail"

    def test_as_it_should_be(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT)
        statuses = _statuses(preflight.check_settings(KC, "authcenter"))
        assert statuses == {"SSO_PROVIDER": "ok", "KEYCLOAK_ISSUER_URL": "ok", "KEYCLOAK_CLIENT_ID": "ok"}

    def test_the_client_id_left_at_its_default_is_pointed_out(self, monkeypatch):
        monkeypatch.delenv("KEYCLOAK_CLIENT_ID", raising=False)
        assert _statuses(preflight.check_settings(KC, "authcenter"))["KEYCLOAK_CLIENT_ID"] == "warn"


# ── The realm ──

def _answer(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


class TestRealm:
    def test_the_issuer_matches(self):
        with patch.object(preflight, "_get", return_value=_answer(200, {"issuer": ISSUER, "jwks_uri": KC["jwks_url"]})):
            assert _statuses(preflight.check_realm_document(KC)) == {"issuer": "ok"}

    def test_another_issuer_is_the_classic_401(self):
        public = "https://login.example.test/realms/company"
        with patch.object(preflight, "_get", return_value=_answer(200, {"issuer": public})):
            [check] = preflight.check_realm_document(KC)
        assert check.status == "fail" and public in check.fix and "KEYCLOAK_JWKS_URL" in check.fix

    def test_a_wrong_realm_name(self):
        with patch.object(preflight, "_get", return_value=_answer(404, {"error": "Realm does not exist"})):
            [check] = preflight.check_realm_document(KC)
        assert check.status == "fail" and "HTTP 404" in check.detail

    def test_not_reached(self):
        with patch.object(preflight, "_get", side_effect=httpx.ConnectTimeout("")):
            [check] = preflight.check_realm_document(KC)
        assert check.status == "fail" and "ConnectTimeout" in check.detail

    def test_the_signing_keys_are_read_by_the_gateways_own_code(self):
        key = _key()
        jwk = {**pyjwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": "k1", "alg": "RS256", "use": "sig"}
        with patch.object(keycloak, "_fetch_jwks", return_value={"keys": [jwk]}):
            [check] = preflight.check_signing_keys(KC)
        assert check.status == "ok" and "1 key(s)" in check.detail and "RS256" in check.detail
        with patch.object(keycloak, "_fetch_jwks", return_value={"keys": []}):
            assert preflight.check_signing_keys(KC)[0].status == "fail"


# ── Accounts ──

def _add(db_session, username: str, **fields) -> User:
    user = User(username=username, api_key=f"sk-{username}", **fields)
    db_session.add(user)
    db_session.commit()
    return user


def _reading(usernames: set[str], disabled: set[str] = frozenset()) -> offboarding.Reading:
    return offboarding.Reading(set(disabled), None, {}, set(usernames) | set(disabled))


class TestAccounts:
    def test_everyone_is_found(self, db_session):
        _add(db_session, "alice")
        _add(db_session, "bob")
        statuses = _statuses(preflight.check_accounts(db_session, _reading({"alice", "bob"})))
        assert statuses == {"usernames in lower case": "ok", "known to Keycloak": "ok"}

    def test_a_name_with_capitals_would_be_another_account(self, db_session):
        _add(db_session, "Alice")
        check = _by_name(preflight.check_accounts(db_session, _reading({"alice"})))["usernames in lower case"]
        assert check.status == "fail" and "Alice" in check.detail

    def test_accounts_keycloak_does_not_know(self, db_session):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        _add(db_session, "alice")
        _add(db_session, "ghost", api_key_last_used_at=now - timedelta(days=2))
        _add(db_session, "gone", is_disabled=True)  # already disabled: not counted
        check = _by_name(preflight.check_accounts(db_session, _reading({"alice"})))["known to Keycloak"]
        assert check.status == "warn" and "1 of 2 active accounts" in check.detail
        assert "ghost" in check.detail and "gone" not in check.detail and "1 used their key" in check.detail

    def test_a_service_account_under_a_persons_name(self, db_session):
        _add(db_session, "alice")
        _add(db_session, "bob", is_service_account=True)
        checks = _by_name(preflight.check_accounts(db_session, _reading({"alice", "bob"})))
        assert checks["service account names"].status == "warn" and "bob" in checks["service account names"].detail
        # ... and a service account is never one of the accounts to be found.
        assert checks["known to Keycloak"].status == "ok"

    def test_people_disabled_at_keycloak_are_pointed_out(self, db_session):
        _add(db_session, "alice")
        _add(db_session, "bob")
        checks = _by_name(preflight.check_accounts(db_session, _reading({"alice"}, {"bob"})))
        assert checks["disabled at Keycloak"].status == "warn" and "bob" in checks["disabled at Keycloak"].detail


# ── The sign-in client and the realm, from the Admin API ──

BASE = "https://sso.example.test/admin/realms/company"
CALLBACK = "https://gateway.example.test/oauth2/callback"


class Admin:
    def __init__(self):
        self.client = {
            "id": "uuid-1", "clientId": CLIENT, "enabled": True, "publicClient": False, "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False, "redirectUris": [CALLBACK],
            "protocolMappers": [{"name": "org code", "config": {"claim.name": "org_code", "access.token.claim": "true"}}],
        }
        self.roles = ["read", "admin"]
        self.holders = {"admin": (["carol"], []), "read": ([], ["llm-gateway-users"])}
        self.realm = {"realm": "company", "accessTokenLifespan": 300, "registrationAllowed": False, "editUsernameAllowed": False}
        self.may_view_clients = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/admin/realms/company")
        if path == "":
            return httpx.Response(200, json=self.realm)
        if path == "/clients":
            if not self.may_view_clients:
                return httpx.Response(403, json={"error": "HTTP 403 Forbidden"})
            wanted = request.url.params.get("clientId")
            return httpx.Response(200, json=[self.client] if wanted in (None, self.client["clientId"]) else [])
        if path == "/clients/uuid-1/roles":
            return httpx.Response(200, json=[{"name": r} for r in self.roles])
        for role, (users, groups) in self.holders.items():
            if path == f"/clients/uuid-1/roles/{role}/users":
                return httpx.Response(200, json=[{"username": u} for u in users])
            if path == f"/clients/uuid-1/roles/{role}/groups":
                return httpx.Response(200, json=[{"name": g} for g in groups])
        return httpx.Response(404, json={})

    def checks(self, redirect: str = CALLBACK) -> dict[str, preflight.Check]:
        with httpx.Client(transport=httpx.MockTransport(self)) as http:
            return _by_name(preflight.check_client(http, BASE, {"Authorization": "Bearer t"}, KC, redirect))

    def realm_checks(self, refresh: str = "") -> dict[str, preflight.Check]:
        with httpx.Client(transport=httpx.MockTransport(self)) as http:
            return _by_name(preflight.check_realm_settings(http, BASE, {"Authorization": "Bearer t"}, refresh))


class TestClient:
    def test_a_client_set_up_as_the_gateway_reads_it(self):
        checks = Admin().checks()
        assert {c.status for c in checks.values()} == {"ok"}
        assert "carol" in checks["who has admin"].detail
        assert "llm-gateway-users" in checks["who has read"].detail

    def test_no_such_client(self):
        admin = Admin()
        admin.client["clientId"] = "something-else"
        checks = admin.checks()
        assert checks[f"client {CLIENT}"].status == "fail"

    def test_a_public_client_or_one_without_the_browser_flow(self):
        admin = Admin()
        admin.client.update(publicClient=True, standardFlowEnabled=False)
        check = admin.checks()[f"client {CLIENT}"]
        assert check.status == "fail" and "Client authentication is off" in check.detail and "Standard flow is off" in check.detail

    def test_the_callback_is_not_allowed(self):
        admin = Admin()
        admin.client["redirectUris"] = ["https://other.example.test/*"]
        check = admin.checks()["redirect URI"]
        assert check.status == "fail" and "Valid redirect URIs" in check.fix
        admin.client["redirectUris"] = ["https://gateway.example.test/*"]
        assert admin.checks()["redirect URI"].status == "ok"

    def test_a_wildcard_and_password_grants_are_pointed_out(self):
        admin = Admin()
        admin.client.update(redirectUris=["*"], directAccessGrantsEnabled=True)
        checks = admin.checks()
        assert checks["redirect URI wildcard"].status == "warn" and checks["direct access grants"].status == "warn"

    def test_the_roles_are_missing(self):
        admin = Admin()
        admin.roles = ["user"]
        check = admin.checks()["client roles"]
        assert check.status == "fail" and "read, admin" in check.detail and "not realm roles" in check.fix

    def test_nobody_would_be_an_admin(self):
        admin = Admin()
        admin.holders["admin"] = ([], [])
        admin.holders["read"] = ([], [])
        checks = admin.checks()
        assert checks["who has admin"].status == "fail"
        assert checks["who has read"].status == "warn" and "default roles" in checks["who has read"].fix

    def test_no_mapper_for_the_org_code(self):
        admin = Admin()
        admin.client["protocolMappers"] = [{"name": "org", "config": {"claim.name": "org_code", "access.token.claim": "false"}}]
        check = admin.checks()["org code in the token"]
        assert check.status == "warn" and "not for the access token" in check.detail

    def test_a_client_that_may_not_read_clients_is_a_skip_not_a_pass(self):
        admin = Admin()
        admin.may_view_clients = False
        [check] = admin.checks().values()
        assert check.status == "skip" and "view-clients" in check.fix


class TestRealmSettings:
    def test_the_refresh_is_shorter_than_the_token(self):
        checks = Admin().realm_checks("4m")
        assert checks["OAUTH2_COOKIE_REFRESH"].status == "ok"
        assert checks["User registration"].status == "ok" and checks["Edit username"].status == "ok"

    def test_a_refresh_as_long_as_the_token_is_the_401_after_five_minutes(self):
        assert Admin().realm_checks("5m")["OAUTH2_COOKIE_REFRESH"].status == "fail"
        assert Admin().realm_checks("1h")["OAUTH2_COOKIE_REFRESH"].status == "fail"

    def test_not_set_yet_is_a_to_do_with_the_number(self):
        check = Admin().realm_checks("")["OAUTH2_COOKIE_REFRESH"]
        assert check.status == "todo" and "300 s" in check.detail and "240s" in check.fix

    def test_anyone_may_pick_a_username(self):
        admin = Admin()
        admin.realm.update(registrationAllowed=True, editUsernameAllowed=True)
        checks = admin.realm_checks("4m")
        assert checks["User registration"].status == "fail" and checks["Edit username"].status == "fail"

    def test_a_client_that_only_sees_the_realms_name(self):
        admin = Admin()
        admin.realm = {"realm": "company"}  # what Keycloak answers without view-realm
        [check] = admin.realm_checks("4m").values()
        assert check.status == "skip" and "view-realm" in check.fix

    @pytest.mark.parametrize("text, seconds", [("4m", 240), ("1h", 3600), ("240s", 240), ("240", 240), ("1h30m", 5400), ("", None), ("soon", None)])
    def test_durations(self, text, seconds):
        assert preflight._seconds(text) == seconds


# ── A real token ──

_REALM_KEY = _key()
_JWKS = {"keys": [{**pyjwt.algorithms.RSAAlgorithm.to_jwk(_REALM_KEY.public_key(), as_dict=True),
                   "kid": "realm-1", "alg": "RS256", "use": "sig"}]}


def _claims(**over) -> dict:
    now = int(time.time())
    claims = {
        "iss": ISSUER, "exp": now + 300, "iat": now, "typ": "Bearer", "azp": CLIENT,
        "preferred_username": "alice", "name": "Alice Chen", "org_code": "RD1",
        "resource_access": {CLIENT: {"roles": ["read"]}},
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _token(key=_REALM_KEY, **over) -> str:
    return pyjwt.encode(_claims(**over), key, algorithm="RS256", headers={"kid": "realm-1"})


@pytest.fixture()
def realm(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_CLIENT_ID", CLIENT)
    for name in ("KEYCLOAK_JWKS_URL", "KEYCLOAK_CA_FILE", "KEYCLOAK_USERNAME_CLAIM", "KEYCLOAK_ORG_CLAIM"):
        monkeypatch.delenv(name, raising=False)
    keycloak.reset_keys()
    with patch.object(keycloak, "_fetch_jwks", return_value=_JWKS):
        yield keycloak.settings()
    keycloak.reset_keys()


class TestToken:
    def test_a_persons_token_is_accepted_by_the_gateways_own_check(self, realm, db_session):
        _add(db_session, "alice")
        checks = _by_name(preflight.check_token(_token(), realm, db_session))
        assert checks["the gateway's own check"].status == "ok"
        assert "account alice" in checks["the gateway's own check"].detail
        assert "a user, not an admin" in checks["roles"].detail
        assert checks["the account here"].status == "ok" and checks["org_code"].detail == "RD1"

    def test_an_admin(self, realm):
        token = _token(resource_access={CLIENT: {"roles": ["read", "admin"]}})
        assert "an admin" in _by_name(preflight.check_token(token, realm))["roles"].detail

    def test_roles_given_as_realm_roles_are_said_to_be_the_wrong_kind(self, realm):
        token = _token(resource_access=None, realm_access={"roles": ["admin", "read"]})
        check = _by_name(preflight.check_token(token, realm))["roles"]
        assert check.status == "fail" and "realm roles, which the gateway does not read" in check.detail

    def test_an_id_token(self, realm):
        assert _by_name(preflight.check_token(_token(typ="ID"), realm))["typ"].status == "fail"

    def test_a_token_of_another_client(self, realm):
        check = _by_name(preflight.check_token(_token(azp="other-app"), realm))["aud / azp"]
        assert check.status == "fail" and "other-app" in check.detail

    def test_another_issuer(self, realm):
        check = _by_name(preflight.check_token(_token(iss="https://login.example.test/realms/company"), realm))["iss"]
        assert check.status == "fail"

    def test_signed_by_someone_else(self, realm):
        checks = _by_name(preflight.check_token(_token(key=_key()), realm))
        assert checks["the gateway's own check"].status == "fail"

    def test_expired(self, realm):
        checks = _by_name(preflight.check_token(_token(exp=int(time.time()) - 60), realm))
        assert checks["the gateway's own check"].status == "fail" and "Expired" in checks["the gateway's own check"].fix

    def test_no_org_code_is_a_warning(self, realm):
        assert _by_name(preflight.check_token(_token(org_code=None), realm))["org_code"].status == "warn"

    def test_the_payload_from_the_console_is_read_without_a_signature(self, realm):
        checks = _by_name(preflight.check_token(json.dumps(_claims()), realm))
        assert checks["roles"].status == "ok" and checks["the gateway's own check"].status == "skip"

    def test_rubbish(self, realm):
        [check] = preflight.check_token("not a token", realm)
        assert check.status == "fail"

    def test_the_account_it_would_become(self, realm, db_session):
        _add(db_session, "Alice")  # the gateway's account has a capital letter
        check = _by_name(preflight.check_token(_token(), realm, db_session))["the account here"]
        assert check.status == "fail" and "Rename Alice to alice" in check.fix

    def test_a_service_account_of_that_name(self, realm, db_session):
        _add(db_session, "alice", is_service_account=True)
        check = _by_name(preflight.check_token(_token(), realm, db_session))["the account here"]
        assert check.status == "fail" and "403" in check.detail

    def test_a_person_without_an_account_yet(self, realm, db_session):
        check = _by_name(preflight.check_token(_token(), realm, db_session))["the account here"]
        assert check.status == "warn" and "a new one would be created" in check.detail


# ── oauth2-proxy ──

class TestOauth2Proxy:
    def test_still_authcenters_values_are_a_to_do_not_a_fault(self):
        env = {"OIDC_ISSUER_URL": "http://auth.company.test", "OAUTH2_CLIENT_ID": "llm_gateway_old"}
        checks = _by_name(preflight.check_oauth2_proxy(env, KC))
        assert checks["OIDC_ISSUER_URL"].status == "todo" and ISSUER in checks["OIDC_ISSUER_URL"].fix
        assert checks["OAUTH2_CLIENT_ID"].status == "todo"

    def test_the_nginx_buffers_are_a_reminder_before_the_switch(self):
        # nginx's file cannot be read from here; a sign-in that comes back
        # as a 502 was the second thing to go wrong at the first switch.
        env = {"OIDC_ISSUER_URL": "http://auth.company.test", "OAUTH2_CLIENT_ID": CLIENT}
        check = _by_name(preflight.check_oauth2_proxy(env, KC))["nginx buffers"]
        assert check.status == "todo" and "proxy_buffer_size 16k" in check.fix and "/oauth2/auth" in check.fix
        switched = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT}
        assert "nginx buffers" not in _by_name(preflight.check_oauth2_proxy(switched, KC))

    def test_the_nginx_example_has_them_in_both_places(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "deploy" / "llm-gateway-example.nginx.conf").read_text(encoding="utf-8")
        for start in ("    location /oauth2/ {", "    location = /oauth2/auth {"):
            block = text[text.index(start):]
            block = block[:block.index("\n    }")]
            assert "proxy_buffer_size       16k;" in block and "proxy_busy_buffers_size 32k;" in block, start

    def test_the_nginx_example_takes_a_large_cookie_header(self):
        # Keycloak's session is split over several cookies, sent back in one
        # Cookie header; nginx's 8k default answers it with a 400.
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "deploy" / "llm-gateway-example.nginx.conf").read_text(encoding="utf-8")
        https = text[text.index("    listen 443 ssl;"):]
        https = https[:https.index("    location ")]
        assert "\n    large_client_header_buffers 4 32k;\n" in https

    def test_already_keycloaks(self):
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT}
        assert {c.status for c in preflight.check_oauth2_proxy(env, KC)} == {"ok"}

    # ── Keycloak's CA: the container has only the public ones ──

    def test_a_company_ca_is_not_one_the_container_trusts(self, tls_server):
        assert preflight.publicly_trusted(tls_server) is False
        assert preflight.publicly_trusted("http://sso.example.test/realms/x") is None

    def test_nothing_to_tell_when_it_cannot_be_reached(self):
        with socket.create_server(("127.0.0.1", 0)) as s:
            port = s.getsockname()[1]
        assert preflight.publicly_trusted(f"https://localhost:{port}/realms/x") is None

    def test_a_company_ca_and_none_given_is_a_to_do_before_the_switch(self):
        env = {"OIDC_ISSUER_URL": "http://auth.company.test", "OAUTH2_CLIENT_ID": CLIENT}
        check = _by_name(preflight.check_oauth2_proxy(env, KC, public=False))["Keycloak's CA"]
        assert check.status == "todo" and "OAUTH2_PROXY_PROVIDER_CA_FILES=" in check.fix
        assert "500" in check.fix

    def test_and_a_failure_once_switched(self):
        # The situation that takes every page down: switched, no CA.
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT}
        assert _by_name(preflight.check_oauth2_proxy(env, KC, public=False))["Keycloak's CA"].status == "fail"

    def test_a_public_certificate_needs_nothing(self):
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT}
        assert "Keycloak's CA" not in _by_name(preflight.check_oauth2_proxy(env, KC, public=True))
        assert "Keycloak's CA" not in _by_name(preflight.check_oauth2_proxy(env, KC, public=None))

    def test_the_ca_given_is_looked_at_and_tried(self, tls_server, pki):
        kc = {**KC, "issuer": tls_server}
        env = {"OIDC_ISSUER_URL": tls_server, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: pki["ca"]}
        compose = "OAUTH2_PROXY_PROVIDER_CA_FILES:\n"
        checks = _by_name(preflight.check_oauth2_proxy(env, kc, compose, public=False))
        assert checks[preflight.CA_SETTING].status == "ok"
        assert checks["Keycloak with that CA"].status == "ok"
        assert "Keycloak's CA" not in checks and "docker-compose.yml" not in checks

    def test_the_wrong_ca_is_said(self, tls_server, pki):
        kc = {**KC, "issuer": tls_server}
        env = {"OIDC_ISSUER_URL": tls_server, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: pki["other"]}
        check = _by_name(preflight.check_oauth2_proxy(env, kc, public=False))["Keycloak with that CA"]
        assert check.status == "fail" and "Test Company Root CA" in check.fix

    def test_a_file_that_is_not_there(self, pki):
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: str(pki["dir"] / "nope.pem")}
        check = _by_name(preflight.check_oauth2_proxy(env, KC, public=False))[preflight.CA_SETTING]
        assert check.status == "fail" and "does not exist" in check.detail

    @pytest.mark.parametrize("value", ["ca.pem", "/etc/ssl/a.pem,/etc/ssl/b.pem"])
    def test_it_is_one_absolute_path(self, value):
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: value}
        check = _by_name(preflight.check_oauth2_proxy(env, KC, public=False))[preflight.CA_SETTING]
        assert check.status == "fail" and "one absolute path" in check.detail

    def test_a_file_the_container_cannot_read(self, pki, tmp_path):
        private = tmp_path / "ca.pem"
        private.write_bytes(_pem(pki["ca_cert"]))
        private.chmod(0o600)
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: str(private)}
        check = _by_name(preflight.check_oauth2_proxy(env, KC, public=False))["CA file permissions"]
        assert check.status == "fail" and "chmod 644" in check.fix

    def test_an_older_compose_file_does_not_pass_it_on(self, pki):
        env = {"OIDC_ISSUER_URL": ISSUER, "OAUTH2_CLIENT_ID": CLIENT, preflight.CA_SETTING: pki["ca"]}
        checks = _by_name(preflight.check_oauth2_proxy(env, KC, "services:\n  oauth2-proxy:\n", public=False))
        assert checks["docker-compose.yml"].status == "fail" and "git pull" in checks["docker-compose.yml"].fix

    def test_the_compose_file_takes_it_from_the_env_and_nothing_when_unset(self):
        # The key has no value (so an unset variable is not in the container
        # at all: existing deployments are as they were), and the mount falls
        # back to /dev/null.
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        assert "      OAUTH2_PROXY_PROVIDER_CA_FILES:\n" in text
        assert "${OAUTH2_PROXY_PROVIDER_CA_FILES:-/dev/null}:${OAUTH2_PROXY_PROVIDER_CA_FILES:-/tmp/no-provider-ca}:ro" in text

    def test_the_env_file(self, tmp_path):
        f = tmp_path / ".env"
        f.write_text("# a comment\nOIDC_ISSUER_URL=https://x/realms/y\nexport OAUTH2_CLIENT_ID='abc'\n\nbroken line\n")
        assert preflight.read_env_file(f) == {"OIDC_ISSUER_URL": "https://x/realms/y", "OAUTH2_CLIENT_ID": "abc"}
        assert preflight.read_env_file(tmp_path / "missing") == {}


# ── All of it ──

class TestRun:
    def test_without_an_issuer_it_stops_at_the_settings(self, monkeypatch):
        monkeypatch.delenv("KEYCLOAK_ISSUER_URL", raising=False)
        checks = preflight.run(None)
        assert [c.area for c in checks] == ["settings", "settings"] and checks[-1].status == "fail"

    def test_what_cannot_be_checked_is_a_skip_never_a_pass(self, realm, monkeypatch, db_session):
        for name in ("OFFBOARDING_KEYCLOAK_URL", "OFFBOARDING_KEYCLOAK_CLIENT_ID", "OFFBOARDING_KEYCLOAK_CLIENT_SECRET"):
            monkeypatch.delenv(name, raising=False)
        with patch.object(preflight, "probe_tls", return_value=[]), \
             patch.object(preflight, "_get", return_value=_answer(200, {"issuer": ISSUER})):
            checks = _by_name(preflight.run(db_session))
        assert checks["Keycloak's users"].status == "skip"
        assert checks["a real access token"].status == "skip"
        assert checks["issuer"].status == "ok" and checks["signing keys (JWKS)"].status == "ok"

    def test_it_only_reads(self):
        src = inspect.getsource(preflight)
        for write in (".add(", ".commit(", ".delete(", ".post(", ".put(", ".patch("):
            assert write not in src, write
