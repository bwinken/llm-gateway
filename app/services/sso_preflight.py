"""Checks to run before the web UI's sign-in is switched to Keycloak, and
before the leaver check (``offboarding``) is switched on.

``scripts/keycloak_preflight.py`` runs them and prints the result. Each
check answers one question the switch depends on, with what to do when the
answer is no:

- **settings**: are the ``KEYCLOAK_*`` values there and shaped right;
- **certificate**: does this host trust Keycloak's certificate, with the
  CA file when one is set; if not, which CA signed it and what is in the
  file (``probe_tls``, ``inspect_ca_file``);
- **realm**: does the realm name itself the way the tokens will
  (``iss``), and can its signing keys be read (the JWKS: nothing to set up,
  the realm publishes them);
- **accounts**: which accounts here would not be found again (Keycloak
  lower-cases usernames; a name it does not know signs in to a new, empty
  account);
- **client**: is the sign-in client set up as the gateway reads it (client
  roles ``read`` / ``admin``, who holds ``admin``, the org-code mapper);
- **token**: an access token of a real person, checked by the very
  function the web UI will use (``keycloak.decode``);
- **oauth2-proxy**: what ``deploy/.env`` must say at the switch, and
  whether its container will trust Keycloak: it sees only the public CAs,
  not this host's trust store, so a company CA has to be handed to it
  (``OAUTH2_PROXY_PROVIDER_CA_FILES``).

It reads and never writes: not the database, not Keycloak, not a setting.
Nothing here runs in the gateway; it is a script's module, kept under
``app/services`` so the tests can reach it.

A check that could not be made is ``skip`` with the reason, never ``ok``:
the Admin API parts need the leaver check's client (``OFFBOARDING_KEYCLOAK_*``)
and, for the client and the realm, two more read-only roles
(``view-clients``, ``view-realm``) that the leaver check itself does not need.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.core import keycloak
from app.models.schema import User, service_account_clause
from app.services import offboarding

OK, WARN, FAIL, SKIP, TODO = "ok", "warn", "fail", "skip", "todo"
_TIMEOUT_S = 10.0
_EXPIRY_WARN_DAYS = 30
_LIST_LIMIT = 20
# OpenSSL verification results that mean "the CA is not trusted here".
_UNTRUSTED = {2, 18, 19, 20, 21}
_EXPIRED = 10
_HOSTNAME_MISMATCH = {62, 64}   # hostname, IP address


@dataclass(frozen=True)
class Check:
    area: str
    name: str
    status: str          # ok | warn | fail | skip | todo (to do at the switch)
    detail: str
    fix: str = ""


def _names(items: list[str], limit: int = _LIST_LIMIT) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" and {len(items) - limit} more" if len(items) > limit else "")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def check_settings(kc: dict[str, str], provider: str) -> list[Check]:
    out: list[Check] = []
    out.append(Check(
        "settings", "SSO_PROVIDER", OK,
        f"{provider}" + ("" if provider == "keycloak" else ": sign-in is not Keycloak's yet; these checks are for the switch"),
    ))
    issuer = kc["issuer"]
    if not issuer:
        out.append(Check("settings", "KEYCLOAK_ISSUER_URL", FAIL, "not set",
                         "Set it to the realm URL: https://<keycloak>/realms/<realm>"))
        return out
    parts = urlsplit(issuer)
    head, sep, realm = parts.path.rpartition("/realms/")
    if not sep or not realm or "/" in realm or not parts.hostname:
        out.append(Check("settings", "KEYCLOAK_ISSUER_URL", FAIL, f"{issuer} is not a realm URL",
                         "It is https://<keycloak>/realms/<realm>, exactly as the tokens carry it in iss"))
    elif parts.scheme != "https":
        out.append(Check("settings", "KEYCLOAK_ISSUER_URL", WARN, f"{issuer}: not https",
                         "Tokens and the client secret would cross the network unencrypted"))
    else:
        out.append(Check("settings", "KEYCLOAK_ISSUER_URL", OK, issuer))
    explicit = os.getenv("KEYCLOAK_CLIENT_ID", "").strip()
    out.append(Check(
        "settings", "KEYCLOAK_CLIENT_ID", OK if explicit else WARN,
        kc["client_id"] + ("" if explicit else " (the default: not set)"),
        "" if explicit else "Set it to the sign-in client's ID, the one oauth2-proxy uses",
    ))
    return out


def check_proxy(host: str) -> list[Check]:
    """The gateway's reads of Keycloak follow ``HTTPS_PROXY`` like any httpx
    call: an internal Keycloak behind a proxy that cannot reach it fails."""
    try:
        proxied = bool(urllib.request.getproxies().get("https")) and not urllib.request.proxy_bypass(host)
    except Exception:
        proxied = False
    if not proxied:
        return []
    return [Check("certificate", "proxy", WARN,
                  f"requests to {host} go through the proxy in HTTPS_PROXY",
                  f"If Keycloak is inside the network, add {host} to NO_PROXY")]


# ---------------------------------------------------------------------------
# Certificate: the CA file and the handshake
# ---------------------------------------------------------------------------

def _load_x509():
    from cryptography import x509  # noqa: PLC0415 - a broken install costs these checks only
    return x509


def _cert_name(cert) -> str:
    return cert.subject.rfc4514_string() or "(no subject)"


def _is_ca(cert) -> bool:
    x509 = _load_x509()
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _fingerprint(cert) -> str:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _expiry(cert) -> datetime:
    return cert.not_valid_after_utc


def inspect_ca_file(path: str, setting: str = "KEYCLOAK_CA_FILE") -> list[Check]:
    """What is in the CA file: readable, PEM certificates, CAs, not expired,
    no private key. Whether it is the *right* CA is ``probe_tls``'s answer."""
    name = f"{setting}"
    file = Path(path)
    if not file.exists():
        return [Check("certificate", name, FAIL, f"{path} does not exist",
                      "Use an absolute path; in the container it must be a path inside the container")]
    try:
        data = file.read_bytes()
    except OSError as exc:
        return [Check("certificate", name, FAIL, f"{path} cannot be read: {type(exc).__name__}",
                      "The account the gateway runs as must be able to read it")]
    out: list[Check] = []
    if b"PRIVATE KEY" in data:
        out.append(Check("certificate", name, FAIL, f"{path} holds a private key",
                         "A CA file is public certificates only; take the key out and treat it as leaked"))
    if b"-----BEGIN CERTIFICATE-----" not in data:
        kind = "a DER (binary) file" if data[:1] == b"\x30" else "not PEM"
        return out + [Check(
            "certificate", name, FAIL, f"{path} is {kind}",
            "Convert it: openssl x509 -inform der -in ca.cer -out ca.pem (a .p7b: openssl pkcs7 -print_certs)",
        )]
    try:
        certs = _load_x509().load_pem_x509_certificates(data)
    except Exception as exc:
        return out + [Check("certificate", name, FAIL, f"{path} is not valid PEM: {type(exc).__name__}",
                            "Each certificate is a BEGIN CERTIFICATE ... END CERTIFICATE block, nothing cut off")]
    now = datetime.now(timezone.utc)
    expired = [c for c in certs if _expiry(c) < now]
    soon = [c for c in certs if now <= _expiry(c) < now + timedelta(days=_EXPIRY_WARN_DAYS)]
    cas = [c for c in certs if _is_ca(c)]
    summary = f"{path}: {len(certs)} certificate(s): " + _names([_cert_name(c) for c in certs], 5)
    if expired:
        out.append(Check("certificate", name, FAIL, f"expired: {_names([_cert_name(c) for c in expired], 5)}",
                         "Get the CA's current certificate"))
    elif not cas:
        out.append(Check(
            "certificate", name, WARN, summary + ". None of them is a CA",
            "This looks like the server's own certificate: it works until that certificate is renewed. Use the CA that signed it",
        ))
    elif soon:
        out.append(Check("certificate", name, WARN,
                         f"expires within {_EXPIRY_WARN_DAYS} days: {_names([_cert_name(c) for c in soon], 5)}"))
    else:
        out.append(Check("certificate", name, OK, summary))
    return out


def _context(ca_file: str) -> ssl.SSLContext:
    """The trust the gateway itself uses for Keycloak (``keycloak._fetch_jwks``,
    ``offboarding._http``): the CA file alone when one is set, else the
    system's store."""
    return ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def _handshake(host: str, port: int, context: ssl.SSLContext) -> None:
    with socket.create_connection((host, port), timeout=_TIMEOUT_S) as raw:
        with context.wrap_socket(raw, server_hostname=host):
            pass


def _passes_without_strict(host: str, port: int, ca_file: str) -> bool:
    """Would the chain verify without the strict RFC 5280 checks? Only to
    tell a wrong CA from a certificate the CA should not have issued that
    way: the gateway itself keeps the strict default."""
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    try:
        context = _context(ca_file)
        if not strict or not context.verify_flags & strict:
            return False
        context.verify_flags &= ~strict
        _handshake(host, port, context)
    except (OSError, ssl.SSLError):
        return False
    return True


def presented_chain(host: str, port: int) -> list:
    """The certificates the server sends, read **without** verifying them.
    For telling the admin which CA to ask for: nothing is sent over this
    connection and nothing it shows is trusted."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=_TIMEOUT_S) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            chain = getattr(tls, "get_unverified_chain", None)  # Python 3.13+
            ders = list(chain() or []) if chain else []
            if not ders:
                leaf = tls.getpeercert(binary_form=True)
                ders = [leaf] if leaf else []
    x509 = _load_x509()
    return [x509.load_der_x509_certificate(bytes(der)) for der in ders]


def _chain_advice(chain: list, ca_file: str, setting: str) -> tuple[str, str]:
    """(what the server presented, what to do) for a CA that is not trusted."""
    if not chain:
        return "the server's certificate could not be read", f"Ask whoever runs Keycloak for its CA certificate (PEM) and set {setting}"
    top = chain[-1]
    self_signed = top.issuer == top.subject
    signer = top.subject.rfc4514_string() if self_signed else top.issuer.rfc4514_string()
    presented = (
        f"the server presents {len(chain)} certificate(s); the chain ends at "
        f"{_cert_name(top)}" + ("" if self_signed else f", issued by {top.issuer.rfc4514_string()}")
    )
    if len(chain) == 1 and not self_signed:
        presented += " (it does not send its intermediate certificate)"
    if ca_file:
        fix = (
            f"{setting} does not hold the CA this chain leads to. It needs the certificate of: {signer}"
            + ("" if self_signed else " (and any intermediate the server does not send)")
        )
    else:
        fix = (
            f"This host does not trust that CA. Save the certificate of {signer} as a PEM file and set "
            f"{setting} to its path. oauth2-proxy needs the same CA"
        )
    if self_signed:
        fix += f". Confirm its SHA-256 with whoever runs the CA before trusting it: {_fingerprint(top)}"
    return presented, fix


def probe_tls(url: str, ca_file: str, setting: str = "KEYCLOAK_CA_FILE") -> list[Check]:
    """Does this host trust the server at ``url``, the way the gateway will
    ask (the CA file alone when one is set)? When it does not: why, and
    which CA is missing."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or 443
    if parts.scheme != "https" or not host:
        return [Check("certificate", "TLS", SKIP, f"{url} is not https: there is no certificate to check")]
    name = f"TLS to {host}:{port}"
    try:
        context = _context(ca_file)
    except (OSError, ssl.SSLError) as exc:
        return [Check("certificate", name, FAIL, f"the CA file cannot be loaded: {type(exc).__name__}",
                      f"See the {setting} check above")]
    try:
        _handshake(host, port, context)
    except ssl.SSLCertVerificationError as exc:
        try:
            chain = presented_chain(host, port)
        except Exception:
            chain = []
        code = getattr(exc, "verify_code", 0)
        reason = getattr(exc, "verify_message", "") or "certificate not verified"
        if code in _HOSTNAME_MISMATCH:
            sans = ""
            if chain:
                x509 = _load_x509()
                try:
                    ext = chain[0].extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                    sans = ", ".join(ext.get_values_for_type(x509.DNSName))
                except x509.ExtensionNotFound:
                    pass
            return [Check("certificate", name, FAIL, f"the certificate is not for {host}" + (f" (it is for: {sans})" if sans else ""),
                          "Use the hostname the certificate names in KEYCLOAK_ISSUER_URL, or have the certificate reissued")]
        if code == _EXPIRED:
            when = f" on {_expiry(chain[0]).date()}" if chain else ""
            return [Check("certificate", name, FAIL, f"a certificate in the chain has expired{when}",
                          "Keycloak's certificate (or its CA's) must be renewed")]
        if code in _UNTRUSTED:
            presented, fix = _chain_advice(chain, ca_file, setting)
            return [Check("certificate", name, FAIL, f"not trusted ({reason}): {presented}", fix)]
        if _passes_without_strict(host, port, ca_file):
            # Python 3.13 and later check certificates strictly (RFC 5280)
            # by default, and so does the gateway: the CA is the right one,
            # the certificate itself is what is refused.
            return [Check(
                "certificate", name, FAIL,
                f"the CA is trusted, but a certificate in the chain is refused: {reason}",
                "Python 3.13+ requires certificates to follow RFC 5280 (key identifiers, critical basic "
                "constraints on a CA). The gateway refuses it the same way. Have the certificate reissued "
                "by the CA with that extension; an older OpenSSL-made CA often lacks it",
            )]
        return [Check("certificate", name, FAIL, f"not verified: {reason}")]
    except (OSError, ssl.SSLError) as exc:
        return [Check("certificate", name, WARN, f"no direct connection: {type(exc).__name__}",
                      "If Keycloak is only reachable through a proxy, the realm check below is the real test")]
    trusted_by = f"the CA in {setting}" if ca_file else "this host's own trust store"
    out = [Check("certificate", name, OK, f"trusted by {trusted_by}" + ("" if ca_file else f": {setting} is not needed"))]
    try:
        leaf = presented_chain(host, port)[0]
        left = _expiry(leaf) - datetime.now(timezone.utc)
        if left < timedelta(days=_EXPIRY_WARN_DAYS):
            out.append(Check("certificate", "server certificate", WARN,
                             f"expires {_expiry(leaf).date()} ({max(left.days, 0)} days)",
                             "Sign-in stops for everyone when it does"))
    except Exception:
        pass
    return out


def ca_certificates_presented(url: str) -> list:
    """The CA certificates in the chain the server sends (everything but the
    leaf), for ``--save-ca``. Unverified: the caller says so."""
    parts = urlsplit(url)
    chain = presented_chain(parts.hostname or "", parts.port or 443)
    return [c for c in chain[1:] if _is_ca(c)] or ([chain[0]] if len(chain) == 1 and _is_ca(chain[0]) else [])


def pem_bundle(certs: list) -> bytes:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    return b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)


# ---------------------------------------------------------------------------
# The realm: issuer and signing keys
# ---------------------------------------------------------------------------

def _get(url: str, ca_file: str) -> httpx.Response:
    """GET the way the gateway reads Keycloak (``keycloak._fetch_jwks``)."""
    verify: Any = ssl.create_default_context(cafile=ca_file) if ca_file else True
    return httpx.get(url, timeout=_TIMEOUT_S, verify=verify)


def check_realm_document(kc: dict[str, str]) -> list[Check]:
    """The realm's OpenID configuration: reachable, and its ``issuer`` is
    letter for letter what ``KEYCLOAK_ISSUER_URL`` says. A token's ``iss``
    is that value, and a token whose ``iss`` differs is refused."""
    url = f"{kc['issuer']}/.well-known/openid-configuration"
    try:
        resp = _get(url, kc["ca_file"])
    except Exception as exc:
        return [Check("realm", "realm document", FAIL, f"{url} not read: {type(exc).__name__}",
                      "The gateway host must reach Keycloak and trust its certificate (see the certificate checks)")]
    if resp.status_code != 200:
        return [Check("realm", "realm document", FAIL, f"{url} answered HTTP {resp.status_code}",
                      "404 usually means the realm name is wrong, or Keycloak is served under a path such as /auth")]
    try:
        doc = resp.json()
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or not doc.get("issuer"):
        return [Check("realm", "realm document", FAIL, f"{url} is not an OpenID configuration")]
    out: list[Check] = []
    if doc["issuer"] == kc["issuer"]:
        out.append(Check("realm", "issuer", OK, f"the realm names itself {doc['issuer']}"))
    else:
        out.append(Check(
            "realm", "issuer", FAIL,
            f"the realm names itself {doc['issuer']}, KEYCLOAK_ISSUER_URL says {kc['issuer']}",
            f"Set KEYCLOAK_ISSUER_URL={doc['issuer']} (the tokens' iss). If the gateway cannot reach that "
            "address, keep it and set KEYCLOAK_JWKS_URL to the address it can reach",
        ))
    published = str(doc.get("jwks_uri") or "")
    if published and published != kc["jwks_url"]:
        out.append(Check("realm", "signing keys address", WARN,
                         f"the realm publishes its keys at {published}; the gateway will read {kc['jwks_url']}",
                         "Fine when KEYCLOAK_JWKS_URL is the same realm at an internal address; otherwise remove it"))
    return out


def check_signing_keys(kc: dict[str, str]) -> list[Check]:
    """The realm's signing keys (JWKS), read and parsed by the gateway's own
    code. Nothing is set up for this in Keycloak: every realm publishes them."""
    try:
        keys = keycloak._parse_jwks(keycloak._fetch_jwks(kc["jwks_url"], kc["ca_file"]))
    except Exception as exc:
        return [Check("realm", "signing keys (JWKS)", FAIL, f"{kc['jwks_url']} not usable: {type(exc).__name__}: {exc}"[:300],
                      "Without them no token can be checked and every sign-in is a 401")]
    algs = sorted({k.algorithm_name for k in keys.values()})
    return [Check("realm", "signing keys (JWKS)", OK, f"{len(keys)} key(s) at {kc['jwks_url']} ({', '.join(algs)})")]


# ---------------------------------------------------------------------------
# Accounts: who would not be found again
# ---------------------------------------------------------------------------

def check_accounts(session: Session, reading: offboarding.Reading) -> list[Check]:
    """Accounts are found by username, and Keycloak's are lower case
    (``auth._sync_user`` is an exact lookup): a name that differs signs in
    to a new, empty account and leaves the old one, with its key, behind."""
    people = session.exec(select(User).where(~service_account_clause()).order_by(User.username)).all()
    services = session.exec(select(User.username).where(service_account_clause())).all()
    active = [p for p in people if not p.is_disabled]
    out: list[Check] = []

    mixed = [p.username for p in active if p.username != p.username.strip().lower()]
    out.append(Check(
        "accounts", "usernames in lower case", FAIL if mixed else OK,
        f"{len(mixed)} active account(s) have capital letters or blanks: {_names(mixed)}" if mixed
        else f"all {len(active)} active accounts",
        "Keycloak signs them in as the lower-case name, which is another account here. Rename them in the "
        "database before the switch (and anything that names them: IP allowlist rules)" if mixed else "",
    ))

    unknown = [p for p in active if p.username.strip().lower() not in reading.usernames]
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    in_use = [p for p in unknown if p.api_key_last_used_at is not None and p.api_key_last_used_at >= cutoff]
    if unknown:
        out.append(Check(
            "accounts", "known to Keycloak", WARN,
            f"{len(unknown)} of {len(active)} active accounts have no Keycloak user of that name "
            f"({len(in_use)} used their key in the last 30 days): {_names([p.username for p in unknown])}",
            "Each would sign in to a new, empty account, or cannot sign in at all. People who left: disable "
            "them. A different name at Keycloak: make the two equal before the switch",
        ))
    else:
        out.append(Check("accounts", "known to Keycloak", OK, f"all {len(active)} active accounts are Keycloak users"))

    taken = sorted({name for name in services if name.strip().lower() in reading.usernames})
    if taken:
        out.append(Check(
            "accounts", "service account names", WARN,
            f"{len(taken)} service account(s) have a Keycloak user's name: {_names(taken)}",
            "Nobody signs in under a service account's name: that person would be refused (403). Rename the service account",
        ))
    left = [p.username for p in active if p.username.strip().lower() in reading.disabled]
    if left:
        out.append(Check("accounts", "disabled at Keycloak", WARN,
                         f"{len(left)} active account(s) here are disabled there: {_names(left)}",
                         "They left or were suspended: this is what the leaver check (OFFBOARDING_MODE) lists"))
    return out


# ---------------------------------------------------------------------------
# The sign-in client and the realm (Admin API: view-clients, view-realm)
# ---------------------------------------------------------------------------

def _admin_get(http: httpx.Client, url: str, auth: dict[str, str], **params: Any) -> tuple[int, Any]:
    resp = http.get(url, params=params or None, headers=auth)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, None


def _redirect_allowed(patterns: list[str], wanted: str) -> bool:
    for pattern in patterns:
        if pattern == wanted or (pattern.endswith("*") and wanted.startswith(pattern[:-1])):
            return True
    return False


def check_client(
    http: httpx.Client, base: str, auth: dict[str, str], kc: dict[str, str], redirect_url: str = "",
) -> list[Check]:
    """The sign-in client as the gateway reads it: confidential, standard
    flow, the redirect URI, the client roles ``read`` and ``admin`` and who
    holds them, and a mapper for the org code."""
    client_id = kc["client_id"]
    status, found = _admin_get(http, f"{base}/clients", auth, clientId=client_id)
    if status == 403 or (status == 200 and found == [] and _lacks_view_clients(http, base, auth)):
        return [Check("client", f"client {client_id}", SKIP, "the checking client may not read clients",
                      "Give it realm-management → view-clients for the preflight (the leaver check does not need it)")]
    if status != 200 or not isinstance(found, list):
        return [Check("client", f"client {client_id}", SKIP, f"clients not read: HTTP {status}")]
    if not found:
        return [Check("client", f"client {client_id}", FAIL, "no such client in the realm",
                      "Create it, or set KEYCLOAK_CLIENT_ID to the client oauth2-proxy uses")]
    client = found[0]
    out: list[Check] = []
    problems = []
    if client.get("enabled") is False:
        problems.append("it is disabled")
    if client.get("publicClient"):
        problems.append("Client authentication is off (a public client has no secret; oauth2-proxy needs one)")
    if client.get("standardFlowEnabled") is False:
        problems.append("Standard flow is off (the browser sign-in)")
    out.append(Check("client", f"client {client_id}", FAIL if problems else OK,
                     "; ".join(problems) if problems else "enabled, confidential, standard flow on"))
    if client.get("directAccessGrantsEnabled"):
        out.append(Check("client", "direct access grants", WARN, "on",
                         "Turn it off: it lets a program trade a person's password for a token, which nothing here uses"))
    if redirect_url:
        uris = [u for u in client.get("redirectUris") or [] if isinstance(u, str)]
        allowed = _redirect_allowed(uris, redirect_url)
        out.append(Check("client", "redirect URI", OK if allowed else FAIL,
                         f"{redirect_url} is allowed" if allowed else f"{redirect_url} is not among: {_names(uris, 5) or '(none)'}",
                         "" if allowed else "Add it under Valid redirect URIs, or Keycloak refuses the sign-in with 'Invalid parameter: redirect_uri'"))
    if any(u.strip() in ("*", "/*") for u in client.get("redirectUris") or [] if isinstance(u, str)):
        out.append(Check("client", "redirect URI wildcard", WARN, "any address is allowed",
                         "Name the one callback URL: a wildcard lets another site receive the sign-in"))

    uuid = client.get("id")
    status, roles = _admin_get(http, f"{base}/clients/{uuid}/roles", auth)
    names = {r.get("name") for r in roles if isinstance(r, dict)} if status == 200 and isinstance(roles, list) else None
    if names is None:
        out.append(Check("client", "client roles", SKIP, f"roles not read: HTTP {status}"))
    else:
        missing = [r for r in ("read", "admin") if r not in names]
        out.append(Check("client", "client roles", FAIL if missing else OK,
                         f"missing: {', '.join(missing)}" if missing else "read and admin exist",
                         "Create them as roles of this client (not realm roles): the gateway reads no other" if missing else ""))
        for role, nobody in (("admin", FAIL), ("read", WARN)):
            if role not in names:
                continue
            s1, users = _admin_get(http, f"{base}/clients/{uuid}/roles/{role}/users", auth, max=200)
            s2, groups = _admin_get(http, f"{base}/clients/{uuid}/roles/{role}/groups", auth, max=200)
            if s1 != 200 or not isinstance(users, list):
                out.append(Check("client", f"who has {role}", SKIP, f"not read: HTTP {s1}"))
                continue
            group_names = [g.get("name", "?") for g in groups if isinstance(g, dict)] if s2 == 200 and isinstance(groups, list) else []
            user_names = [u.get("username", "?") for u in users if isinstance(u, dict)]
            if not user_names and not group_names:
                out.append(Check(
                    "client", f"who has {role}", nobody, "no user and no group has it directly",
                    "Nobody could open the admin pages after the switch" if role == "admin"
                    else "Unless it is in the realm's default roles, nobody can open the dashboard (403 Insufficient scope)",
                ))
            else:
                held = []
                if group_names:
                    held.append(f"groups: {_names(group_names, 5)}")
                if user_names:
                    held.append(f"{len(user_names)} user(s) directly" + (f": {_names(user_names, 10)}" if role == "admin" else ""))
                out.append(Check("client", f"who has {role}", OK, "; ".join(held)))

    mappers = [m for m in client.get("protocolMappers") or [] if isinstance(m, dict)]
    org = [m for m in mappers if (m.get("config") or {}).get("claim.name") == kc["org_claim"]]
    in_access = [m for m in org if str((m.get("config") or {}).get("access.token.claim", "")).lower() == "true"]
    if in_access:
        out.append(Check("client", "org code in the token", OK,
                         f"mapper '{in_access[0].get('name', '?')}' adds {kc['org_claim']} to the access token"))
    else:
        out.append(Check(
            "client", "org code in the token", WARN,
            f"no mapper of this client adds '{kc['org_claim']}' to the access token" + (" (one exists, but not for the access token)" if org else ""),
            "Without it a sign-in no longer updates a person's department (IP allowlist rules by org code, "
            "service account departments). Add a User Attribute mapper, or let the leaver check read departments",
        ))
    return out


def _lacks_view_clients(http: httpx.Client, base: str, auth: dict[str, str]) -> bool:
    """An empty client list from a client that may not view clients: every
    realm has clients, so none at all means it sees none."""
    status, rows = _admin_get(http, f"{base}/clients", auth, max=1)
    return status == 403 or (status == 200 and rows == [])


def _seconds(text: str) -> int | None:
    """``4m`` / ``1h`` / ``240s`` / ``240`` as seconds (oauth2-proxy durations)."""
    text = text.strip().lower()
    if not text:
        return None
    total, number = 0, ""
    for ch in text:
        if ch.isdigit():
            number += ch
        elif ch in "hms" and number:
            total += int(number) * {"h": 3600, "m": 60, "s": 1}[ch]
            number = ""
        else:
            return None
    return total + (int(number) if number else 0)


def check_realm_settings(http: httpx.Client, base: str, auth: dict[str, str], cookie_refresh: str = "") -> list[Check]:
    status, realm = _admin_get(http, base, auth)
    if status != 200 or not isinstance(realm, dict) or "accessTokenLifespan" not in realm:
        return [Check("realm", "realm settings", SKIP, "the checking client may not read the realm's settings",
                      "Give it realm-management → view-realm for the preflight (the leaver check does not need it)")]
    out: list[Check] = []
    lifespan = int(realm.get("accessTokenLifespan") or 0)
    refresh = _seconds(cookie_refresh)
    if refresh is None:
        out.append(Check("oauth2-proxy", "OAUTH2_COOKIE_REFRESH", TODO,
                         f"access tokens live {lifespan} s",
                         f"At the switch set OAUTH2_COOKIE_REFRESH in deploy/.env below that, e.g. {max(lifespan - 60, 30)}s"))
    elif refresh >= lifespan:
        out.append(Check("oauth2-proxy", "OAUTH2_COOKIE_REFRESH", FAIL,
                         f"{cookie_refresh} is not shorter than the access token's life ({lifespan} s)",
                         "The web UI turns 401 once the first token expires. Set it below the lifespan"))
    else:
        out.append(Check("oauth2-proxy", "OAUTH2_COOKIE_REFRESH", OK, f"{cookie_refresh}, below the access token's {lifespan} s"))
    for field, label in (("registrationAllowed", "User registration"), ("editUsernameAllowed", "Edit username")):
        on = bool(realm.get(field))
        out.append(Check(
            "realm", label, FAIL if on else OK, "on" if on else "off",
            "Turn it off (Realm settings → Login): whoever can take a username gets that account, API key included" if on else "",
        ))
    return out


# ---------------------------------------------------------------------------
# A real token
# ---------------------------------------------------------------------------

def _unverified_claims(text: str) -> tuple[dict | None, bool]:
    """(claims, is a signed token). ``text`` is an access token, or the JSON
    the admin console's *Evaluate* tab shows."""
    text = text.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            return None, False
        return (data if isinstance(data, dict) else None), False
    import jwt  # noqa: PLC0415
    try:
        return jwt.decode(text, options={"verify_signature": False}), True
    except jwt.PyJWTError:
        return None, True


def check_token(text: str, kc: dict[str, str], session: Session | None = None) -> list[Check]:
    """An access token of a real person, checked by ``keycloak.decode`` (the
    function the web UI will use), and read claim by claim so a refusal says
    which claim. A JSON payload is read the same way, without the signature."""
    claims, signed = _unverified_claims(text)
    if claims is None:
        return [Check("token", "token", FAIL, "neither an access token nor a JSON payload",
                      "Pass the access token (three parts separated by dots), or the JSON from Clients → Client scopes → Evaluate")]
    out: list[Check] = []
    client_id = kc["client_id"]
    if claims.get("iss") != kc["issuer"]:
        out.append(Check("token", "iss", FAIL, f"{claims.get('iss')!r}, expected {kc['issuer']!r}",
                         "KEYCLOAK_ISSUER_URL must be exactly this value"))
    if claims.get("typ") != "Bearer":
        out.append(Check("token", "typ", FAIL, f"{claims.get('typ')!r}: not an access token",
                         "An ID token is refused. oauth2-proxy must pass the access token (it does: PASS_ACCESS_TOKEN)"))
    if not keycloak._issued_for(claims, client_id):
        out.append(Check("token", "aud / azp", FAIL,
                         f"issued for azp={claims.get('azp')!r} aud={claims.get('aud')!r}, not for {client_id}",
                         "KEYCLOAK_CLIENT_ID must be the client oauth2-proxy signs in with (or add an Audience mapper)"))
    username = keycloak._text(claims.get(kc["username_claim"]))
    if not username:
        out.append(Check("token", kc["username_claim"], FAIL, "missing: nothing names the account",
                         "The built-in 'profile' client scope adds preferred_username; keep it a default scope of the client"))
    access = claims.get("resource_access")
    roles = ((access.get(client_id) or {}).get("roles") if isinstance(access, dict) else None) or []
    realm_roles = ((claims.get("realm_access") or {}).get("roles") if isinstance(claims.get("realm_access"), dict) else None) or []
    if "read" in roles or "admin" in roles:
        out.append(Check("token", "roles", OK, f"client roles of {client_id}: {', '.join(sorted(map(str, roles)))}"
                         + (" → an admin" if "admin" in roles else " → a user, not an admin")))
    else:
        hint = ""
        if "read" in realm_roles or "admin" in realm_roles:
            hint = " (read / admin are there as realm roles, which the gateway does not read)"
        out.append(Check("token", "roles", FAIL,
                         f"no client role read or admin of {client_id} in resource_access{hint}",
                         "Assign the client's role to the person or their group; the 'roles' client scope must be a default scope"))
    org = keycloak._text(claims.get(kc["org_claim"]))
    out.append(Check("token", kc["org_claim"], OK if org else WARN, org or "not in the token",
                     "" if org else "The person's department will not follow their sign-in (see 'org code in the token')"))

    if signed and not any(c.status == FAIL for c in out):
        payload = keycloak.decode(text)
        if payload is None:
            out.append(Check("token", "the gateway's own check", FAIL, "keycloak.decode refused it",
                             "Expired (take a fresh one: they live minutes), or not signed by this realm's keys"))
        else:
            out.append(Check("token", "the gateway's own check", OK,
                             f"accepted: account {payload['sub']}, scopes {', '.join(payload['scopes']) or '(none)'}"))
    elif not signed:
        out.append(Check("token", "the gateway's own check", SKIP, "a JSON payload has no signature to check",
                         "Claims only. A real access token is the full test"))

    if username and session is not None:
        account = session.exec(select(User).where(User.username == username)).first()
        if account is None:
            near = session.exec(select(User.username).where(User.username.ilike(username))).first()  # type: ignore[attr-defined]
            out.append(Check(
                "token", "the account here", WARN if near is None else FAIL,
                f"no account named {username}" + (f"; there is {near}" if near else ": a new one would be created at sign-in"),
                f"Rename {near} to {username} before the switch, or this person gets a new, empty account" if near else "",
            ))
        elif account.is_service_account:
            out.append(Check("token", "the account here", FAIL, f"{username} is a service account: the sign-in is refused (403)",
                             "Rename the service account"))
        else:
            out.append(Check("token", "the account here", OK,
                             f"{username} exists" + (" (disabled here)" if account.is_disabled else "")))
    return out


# ---------------------------------------------------------------------------
# oauth2-proxy (deploy/.env)
# ---------------------------------------------------------------------------

def read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of a dotenv file; {} when it does not exist."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


CA_SETTING = "OAUTH2_PROXY_PROVIDER_CA_FILES"


def publicly_trusted(url: str) -> bool | None:
    """Would a machine that has only the public CAs trust the server at
    ``url``? That is what the oauth2-proxy container is: it does not see
    this host's trust store, so a company CA this host trusts is unknown to
    it. None when it cannot be told (not https, not reached, no bundle).

    Without the strict RFC 5280 checks: oauth2-proxy is a Go program and
    does not make them."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    try:
        import certifi  # noqa: PLC0415 - the public roots, as the image has them
        context = ssl.create_default_context(cafile=certifi.where())
        context.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        _handshake(parts.hostname, parts.port or 443, context)
    except ssl.SSLCertVerificationError:
        return False
    except Exception:
        return None
    return True


def check_oauth2_proxy(
    deploy_env: dict[str, str], kc: dict[str, str], compose_text: str = "", public: bool | None = None,
) -> list[Check]:
    """What oauth2-proxy must be told at the switch. Before it, these still
    hold AuthCenter's values: that is a to-do, not a fault.

    ``public`` is ``publicly_trusted(issuer)``. When Keycloak's certificate
    is not from a public CA, oauth2-proxy needs that CA or it does not start
    (``x509: certificate signed by unknown authority``) and every page is a
    500: ``OAUTH2_PROXY_PROVIDER_CA_FILES`` in ``deploy/.env`` names the PEM
    on this host, and the compose file mounts it into the container."""
    if not deploy_env:
        return [Check("oauth2-proxy", "deploy/.env", SKIP, "not found here",
                      "At the switch: OIDC_ISSUER_URL and OAUTH2_CLIENT_ID as below, OAUTH2_CLIENT_SECRET the client's secret")]
    out: list[Check] = []
    for key, wanted in (("OIDC_ISSUER_URL", kc["issuer"]), ("OAUTH2_CLIENT_ID", kc["client_id"])):
        have = deploy_env.get(key, "")
        out.append(Check("oauth2-proxy", key, OK if have == wanted else TODO,
                         have if have == wanted else f"{have or '(not set)'}",
                         "" if have == wanted else f"At the switch set {key}={wanted}"))
    switched = deploy_env.get("OIDC_ISSUER_URL", "") == kc["issuer"]
    ca = deploy_env.get(CA_SETTING, "")
    if not ca:
        if public is False:
            out.append(Check(
                "oauth2-proxy", "Keycloak's CA", FAIL if switched else TODO,
                "Keycloak's certificate is not from a public CA, and the oauth2-proxy container trusts only those "
                "(it does not see this host's trust store)",
                f"In deploy/.env set {CA_SETTING}=<absolute path of the CA's PEM on this host>, then "
                "docker compose up -d. Without it oauth2-proxy does not start "
                "('x509: certificate signed by unknown authority') and every page is a 500",
            ))
        return out
    if "," in ca or not ca.startswith("/"):
        return out + [Check(
            "oauth2-proxy", CA_SETTING, FAIL, f"{ca}: must be one absolute path",
            "The compose file mounts this one file into the container under the same path. Put every "
            "certificate it needs (the root and any intermediate) in one PEM file",
        )]
    if compose_text and CA_SETTING not in compose_text:
        out.append(Check("oauth2-proxy", "docker-compose.yml", FAIL,
                         f"this deploy/docker-compose.yml does not pass {CA_SETTING} to the container",
                         "It is an older copy: update the repository (git pull), then docker compose up -d"))
    out += [Check("oauth2-proxy", c.name, c.status, c.detail, c.fix) for c in inspect_ca_file(ca, CA_SETTING)]
    try:
        readable = bool(Path(ca).stat().st_mode & 0o004)
    except OSError:
        return out
    if not readable:
        out.append(Check("oauth2-proxy", "CA file permissions", FAIL, f"{ca} is not readable by others",
                         f"The container does not run as root: chmod 644 {ca}"))
    probe = probe_tls(kc["issuer"], ca, CA_SETTING)
    if probe and probe[0].status != SKIP:
        first = probe[0]
        out.append(Check("oauth2-proxy", "Keycloak with that CA", first.status, first.detail, first.fix))
    return out


# ---------------------------------------------------------------------------
# All of it
# ---------------------------------------------------------------------------

def run(session: Session | None = None, *, token_text: str = "", repo_root: Path | None = None) -> list[Check]:
    """Every check that can be made from here, in the order they depend on
    each other. Reads only."""
    from app.core.auth import sso_provider  # noqa: PLC0415

    kc = keycloak.settings()
    ob = offboarding.settings()
    checks = check_settings(kc, sso_provider())
    if not kc["issuer"]:
        return checks
    host = urlsplit(kc["issuer"]).hostname or ""
    checks += check_proxy(host)

    ca_setting = "KEYCLOAK_CA_FILE"
    if kc["ca_file"]:
        checks += inspect_ca_file(kc["ca_file"], ca_setting)
    tls = probe_tls(kc["issuer"], kc["ca_file"], ca_setting)
    checks += tls
    checks += check_realm_document(kc)
    checks += check_signing_keys(kc)

    deploy_env: dict[str, str] = {}
    compose_text = ""
    if repo_root is not None:
        deploy_env = read_env_file(repo_root / "deploy" / ".env")
        try:
            compose_text = (repo_root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
        except OSError:
            pass
    checks += check_oauth2_proxy(deploy_env, kc, compose_text, publicly_trusted(kc["issuer"]))

    missing = offboarding.missing_settings(ob)
    if missing:
        checks.append(Check("accounts", "Keycloak's users", SKIP, "not set: " + ", ".join(missing),
                            "The account, client and realm checks read Keycloak with the leaver check's client"))
    else:
        if ob["ca_file"] and ob["ca_file"] != kc["ca_file"]:
            checks += inspect_ca_file(ob["ca_file"], "OFFBOARDING_KEYCLOAK_CA_FILE")
        try:
            reading = offboarding.read(ob, everyone=True)
        except offboarding.OffboardingError as exc:
            checks.append(Check("accounts", "Keycloak's users", FAIL, f"not read: {exc}",
                                "The client needs Service accounts roles on and realm-management → view-users"))
        else:
            checks.append(Check("accounts", "Keycloak's users", OK,
                                f"{len(reading.usernames)} users read, {len(reading.disabled)} disabled"))
            if session is not None:
                checks += check_accounts(session, reading)
            try:
                with offboarding._http(ob) as http:
                    auth = offboarding.sign_in(http, ob)
                    base = offboarding.admin_url(ob["realm_url"])
                    checks += check_client(http, base, auth, kc, deploy_env.get("OAUTH2_REDIRECT_URL", ""))
                    checks += check_realm_settings(http, base, auth, deploy_env.get("OAUTH2_COOKIE_REFRESH", ""))
            except (offboarding.OffboardingError, httpx.HTTPError) as exc:
                checks.append(Check("client", "sign-in client", SKIP, f"not read: {type(exc).__name__}"))

    if token_text:
        checks += check_token(token_text, kc, session)
    else:
        checks.append(Check(
            "token", "a real access token", SKIP, "none given",
            "The one end-to-end test: pass a person's access token with --token FILE (or the JSON from "
            "Clients → Client scopes → Evaluate) and the gateway's own check says who they would be",
        ))
    return checks
