# Keycloak setup, step by step

[繁體中文](keycloak-setup.zh-TW.md)

From the first button in Keycloak's admin console to the day of the switch. Afterwards:

- the web pages (dashboard, admin) sign in through Keycloak instead of AuthCenter;
- the gateway asks Keycloak every hour who left and who changed department.

API users notice nothing: they use API keys, and accounts, keys, limits and usage stay as they are.

> Menu names are those of the admin console of Keycloak 19 and later; wording differs a little between versions. These steps were not run against your Keycloak, so run the preflight (step 7) after each part and let it say whether it is right.

## The whole of it

Two **clients** in Keycloak, for two purposes. Don't share one:

| Client | For | Who holds its secret | Permissions |
|---|---|---|---|
| `llm_gateway` | people signing in | oauth2-proxy (`deploy/.env`) | two client roles: `read`, `admin` |
| `llm-gateway-offboarding` | asking who is disabled and who moved, and the preflight | the gateway (`.env`) | read-only: `view-users` |

**Suggested order**: set Keycloak up in one go (steps 1 to 6), get the preflight clean, then go live in two moves.

1. The leaver check first (step 8). It does not touch sign-in, nobody notices it, and it proves the connection and the usernames with real data.
2. Then sign-in (step 9). Everyone signs in once more, and it can be undone at any time (step 10).

With no `FAIL` in the preflight both can be done on one day; doing them apart only leaves one thing less to go wrong at the switch.

## 1. Before you start

| Confirm | Why |
|---|---|
| The realm URL: `https://<keycloak>/realms/<realm>` | Every setting is this value, letter for letter what a token carries in `iss` |
| A person's **username** at Keycloak equals their gateway account name | Accounts are found by username. A name that differs signs in to a new, empty account |
| A person who leaves is **disabled** at Keycloak (Enabled off), not deleted | The leaver check acts on "disabled" only |
| Who signed Keycloak's certificate (a public CA, or the company's own) | With an internal CA the gateway and oauth2-proxy each need the CA's certificate |

Keycloak lower-cases usernames. Look for accounts with capitals first:

```sql
SELECT username FROM users WHERE username <> lower(username) AND NOT is_service_account;
```

They must be renamed before the switch (the preflight lists them too).

## 2. The sign-in client

*Clients* → *Create client*

| Page | Field | Value |
|---|---|---|
| General settings | Client type | `OpenID Connect` |
| | Client ID | `llm_gateway` |
| Capability config | Client authentication | **On** |
| | Authorization | Off |
| | Standard flow | **ticked** |
| | Direct access grants | **unticked** |
| | every other flow | unticked |
| Login settings | Valid redirect URIs | `https://<the gateway's domain>/oauth2/callback` |
| | Web origins | empty |

After saving, copy the **Client secret** from the *Credentials* tab: it is `OAUTH2_CLIENT_SECRET` in `deploy/.env` on the day of the switch.

- Write the one callback URL, never `*`: a wildcard lets another site receive the sign-in.
- Direct access grants off: it lets a program trade a person's password for a token, which nothing here uses.

## 3. Two client roles

*Clients* → `llm_gateway` → *Roles* → *Create role*, twice:

| Role name | Means | Description (worth filling in) |
|---|---|---|
| `read` | may sign in to the dashboard and trade a sign-in for their own API key from a terminal tool | `LLM Gateway: sign in to the dashboard` |
| `admin` | may open the admin pages | `LLM Gateway: administer accounts, limits and models` |

**Exactly these names**: the gateway reads the roles called `read` and `admin` of this client.

### Should the role names be more formal?

No, and renaming them is not advised:

- They are **client roles**: their full name in Keycloak is already `llm_gateway` / `read`, so they cannot be confused with another system's. Realm roles are the ones that need a naming scheme.
- The gateway deliberately reads **no realm role**: a realm-wide `admin` is another system's permission and must not open the gateway's admin pages.

What deserves formality is **how they are given**, not what they are called:

| Advice | How |
|---|---|
| Assign through groups, not person by person | Groups `llm-gateway-users` and `llm-gateway-admins`, with the role on the group |
| Keep the admin group small and named | Two or three people. An admin can see anyone's API key and change anyone's limit |
| Fill in the description of every role and group | An access review can then tell what a permission is |
| Changes go through a request | Who joined the admin group is in Keycloak's admin events (Realm settings → Events must be on) |

If your Keycloak enforces a naming rule for roles (a prefix, say), the gateway can be given a setting that maps your names; you need not bend to it.

### Putting a role on a group

*Groups* → *Create group* → `llm-gateway-admins` → open it → *Role mapping* → *Assign role* → change the filter to *Filter by clients* → search `llm_gateway` → tick `admin` → *Assign*. Then add the administrators under *Members*.

`read` can be given two ways; pick one:

| To whom | How |
|---|---|
| **Those who ask** | A group `llm-gateway-users` with `read`; add people to it |
| **Everyone** | *Realm settings* → *User registration* → *Default roles* → *Assign role* → *Filter by clients* → `read` of `llm_gateway`. Everyone in the realm then has it |

With everyone, anybody's first sign-in creates their gateway account (with the default daily limit). Check that this is what AuthCenter does today.

An admin needs no `read` as well: `admin` includes it.

## 4. The department in the token (optional, advised)

The gateway uses the org code for IP allowlist rules by department and for whether an owner still sees a service account that belongs to a department. It is read from the token at sign-in.

*Clients* → `llm_gateway` → *Client scopes* → `llm_gateway-dedicated` → *Add mapper* → *By configuration* → *User Attribute*

| Field | Value |
|---|---|
| Name | `org code` |
| User Attribute | the user attribute that holds the org code |
| Token Claim Name | `org_code` |
| Claim JSON Type | `String` |
| Add to access token | **On** |
| Add to ID token / userinfo | Off |

Not sure what the attribute is called? Skip this, and after step 7 run `uv run python scripts/offboarding_check.py --everyone`: it lists the attribute names users have.

Its value must be the same kind of code the gateway shows today (the Users table), or everyone's department changes at their next sign-in.

## 5. Two realm switches

*Realm settings* → *Login*:

| Setting | Value | Why |
|---|---|---|
| User registration | **Off** | Whoever can register can choose a username |
| Edit username | **Off** | Whoever can change their username can take another person's account name, and with it that person's API key |

*Realm settings* → *Tokens*: note the **Access Token Lifespan** (5 minutes by default). oauth2-proxy must refresh sooner than that.

With users synchronised from AD / LDAP both are usually off already.

## 6. The checking client

*Clients* → *Create client*

| Page | Field | Value |
|---|---|---|
| General settings | Client ID | `llm-gateway-offboarding` |
| Capability config | Client authentication | **On** |
| | Service accounts roles | **ticked** |
| | Standard flow, Direct access grants | **unticked** |

After saving:

1. *Credentials* → copy the client secret.
2. *Service accounts roles* → *Assign role* → *Filter by clients* → search `realm-management`, and tick:

| Role | For | When it can go |
|---|---|---|
| `view-users` | reading the users (who is disabled, departments) | never: the check needs it |
| `view-clients` | the preflight's look at the sign-in client | after the switch |
| `view-realm` | the preflight's look at the token lifespan and the realm switches | after the switch |

All three only read. Without the last two the preflight marks those checks `skip`, never as passed.

## 7. Settings, and the preflight

In the gateway's `.env` (**leave `SSO_PROVIDER` alone**: sign-in stays with AuthCenter):

```bash
# Sign-in (takes effect on the day of the switch)
KEYCLOAK_ISSUER_URL=https://<keycloak>/realms/<realm>
KEYCLOAK_CLIENT_ID=llm_gateway

# The leaver and department check
OFFBOARDING_KEYCLOAK_URL=https://<keycloak>/realms/<realm>
OFFBOARDING_KEYCLOAK_CLIENT_ID=llm-gateway-offboarding
OFFBOARDING_KEYCLOAK_CLIENT_SECRET=<the secret from step 6>
```

Then (it only reads; no restart is needed):

```bash
uv run python scripts/keycloak_preflight.py
```

| Part | What it checks |
|---|---|
| Settings | the values are there and shaped right |
| Keycloak's certificate | whether this host trusts it, and if not, which CA signed it |
| The realm | the realm's issuer is letter for letter the setting; the signing keys can be read |
| oauth2-proxy | what `deploy/.env` must say at the switch |
| Accounts | accounts with capitals, accounts Keycloak does not know, people already disabled there |
| The sign-in client | its settings, the redirect URI, the two roles, who has `admin`, the org-code mapper |
| A real token | see below |

A line is `ok`; `FAIL` (must be fixed); `WARN` (have a look); `todo` (to do on the day of the switch); or `skip` (could not be checked, with the reason: **not a pass**).

### Do the signing keys (JWKS) need setting up?

No. Every realm publishes its public signing keys at `<realm URL>/protocol/openid-connect/certs`; the gateway reads them itself and follows a key rotation by itself. There is no key file to copy (AuthCenter's `keys/public.pem` is no longer read after the switch).

The one case with something to set: the gateway reaches Keycloak at another address than the one in the tokens' `iss` (public `login.company.com`, internal address for the gateway). Then `KEYCLOAK_ISSUER_URL` is the one in the token and `KEYCLOAK_JWKS_URL` the one the gateway can reach. The preflight's *The realm* part says whether you need it.

### A certificate from an internal CA

The preflight then prints something like:

```
[ FAIL ] TLS to keycloak.company.com:443: not trusted (unable to get local issuer certificate): ...
         → This host does not trust that CA. Save the certificate of CN=Company Root CA as a PEM file ...
```

1. Ask whoever runs the CA for that CA's certificate, as PEM. From a `.cer` / `.der`: `openssl x509 -inform der -in ca.cer -out ca.pem`.
2. Put it on the gateway host, e.g. `/etc/ssl/company-ca.pem`, and set `KEYCLOAK_CA_FILE=/etc/ssl/company-ca.pem` in `.env`. The leaver check uses the same file.
3. Run the preflight again. It inspects the file (PEM, a CA, not expired, no private key in it) and whether Keycloak verifies with it.

If the CA's certificate cannot be had, the ones the server presents can be saved:

```bash
uv run python scripts/keycloak_preflight.py --save-ca keycloak-ca.pem
```

They are **not verified**. The command prints each one's SHA-256, which must be confirmed with whoever runs the CA before the file is used.

Two results worth knowing:

- **"the CA is trusted, but a certificate in the chain is refused"**: the CA is right, the certificate itself does not follow RFC 5280 (usually a missing key identifier). Python 3.13 and later refuse it by default, and so does the gateway. The certificate must be reissued; another CA file changes nothing.
- **"None of them is a CA"**: the file holds the server's own certificate, not the CA that signed it. It works until that certificate is renewed.

### One real person's token, as the last check

Whether it is all right is best answered by giving a real person's access token to the gateway's own check.

Without anyone signing in: *Clients* → `llm_gateway` → *Client scopes* → *Evaluate* → pick a user → *Generated access token*, and save the JSON shown to a file:

```bash
uv run python scripts/keycloak_preflight.py --token payload.json
```

It says which account that person would be, whether they are an admin, their department, and whether the account exists here. Try at least one ordinary user and one admin.

A really signed access token goes the same way, and then the signature and the expiry are checked too (a token lives minutes: run it at once). Give the token as a file or with `--token -` on stdin, never on the command line.

## 8. The leaver and department check

Once the preflight's *Accounts* part is fine, add to `.env` and restart the gateway:

```bash
OFFBOARDING_MODE=monitor
# OFFBOARDING_KEYCLOAK_ORG_ATTR=<the attribute>    # only to update people who changed department
```

The admin page's *Needs attention* then lists people whose sign-in is disabled and whose account still works; you disable them. Switch to `enforce` once the list has been right for a while. More in [deploy/README.md](../deploy/README.md#accounts-of-people-who-left).

This has nothing to do with sign-in and can be done days before the switch.

## 9. Switching sign-in

Before:

- [ ] no `FAIL` in the preflight, and every `skip` read
- [ ] `--token` tried for one ordinary user and one admin
- [ ] the accounts with capitals and the ones Keycloak does not know are dealt with
- [ ] at least one account you can sign in with has `admin`
- [ ] users told: sign in once more after the switch; API keys are not affected
- [ ] anyone signing in from a terminal tool (`POST /auth/api-key`): the tool must get its token from Keycloak from then on

**(1) oauth2-proxy: `deploy/.env`**

```bash
OIDC_ISSUER_URL=https://<keycloak>/realms/<realm>
OAUTH2_CLIENT_ID=llm_gateway
OAUTH2_CLIENT_SECRET=<the secret from step 2>
# Shorter than the Access Token Lifespan, or every page turns 401 once the first token expires
OAUTH2_COOKIE_REFRESH=4m
```

With an internal CA oauth2-proxy must trust it too, or it fails with `x509: certificate signed by unknown authority`. Under `oauth2-proxy` in `deploy/docker-compose.yml` (not verified in your environment: start it once before the switch):

```yaml
    environment:
      OAUTH2_PROXY_PROVIDER_CA_FILES: "/etc/ssl/keycloak-ca.pem"
    volumes:
      - /etc/ssl/company-ca.pem:/etc/ssl/keycloak-ca.pem:ro
```

**(2) The gateway: `.env`**

```bash
SSO_PROVIDER=keycloak
```

**(3) Apply both** (the pages answer 401 for the seconds in between)

```bash
docker compose -f deploy/docker-compose.yml up -d    # container hosts: bash deploy/migration/compose.sh up -d
systemctl --user restart llm-gateway
```

**(4) Check**

- [ ] the dashboard opens under your own account, with the same API key
- [ ] `/admin` opens for an account with `admin` and answers 403 for one without
- [ ] wait longer than the Access Token Lifespan and reload: still signed in
- [ ] an API request works as before
- [ ] `scripts/keycloak_preflight.py` once more: `SSO_PROVIDER` says `keycloak` and the oauth2-proxy part is all `ok`

Errors and what they mean are at the end of [deploy/README.md](../deploy/README.md#switching-sso-to-keycloak).

Afterwards `view-clients` and `view-realm` can be taken off the checking client (step 6).

## 10. Going back to AuthCenter

1. Remove `SSO_PROVIDER=keycloak` from `.env` (the `KEYCLOAK_*` values may stay).
2. Put AuthCenter's values back in `deploy/.env` (`OIDC_ISSUER_URL`, `OAUTH2_CLIENT_ID`, `OAUTH2_CLIENT_SECRET`) and remove `OAUTH2_COOKIE_REFRESH`.
3. Apply with the same two commands.

Neither the switch nor going back changes an account, a key or usage. The leaver check is not affected and can stay on.
