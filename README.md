# openhost-keycloak

[Keycloak](https://www.keycloak.org/) packaged as an OpenHost app. Intended to
run on a **public OpenHost instance** and act as the central auth backend
(OIDC identity provider) for customers who pay for managed OpenHost
instances: customer-facing services (billing portal, vm-manager, dashboards)
register as OIDC clients in the `openhost-customers` realm and delegate
login/registration/password-reset to this Keycloak.

## What's inside

One container, three processes supervised by `start.sh`:

| Process        | Bind                | Purpose                                   |
|----------------|---------------------|-------------------------------------------|
| `auth_proxy.py`| `0.0.0.0:8080`      | OpenHost-routed port; gating + headers     |
| Keycloak 26.6  | `127.0.0.1:8081`    | The identity provider                      |
| Postgres 16    | `127.0.0.1:5432`    | Keycloak's database                        |

Postgres data lives in `$OPENHOST_APP_DATA_DIR/postgres`, so OpenHost's
normal app-data backups cover the entire Keycloak state.

## Auth / exposure model

OpenHost gates app subdomains behind the zone owner's login by default.
`public_paths` in `openhost.toml` opens exactly what anonymous customers
need:

- `/realms/` – OIDC discovery/token/userinfo endpoints, realm login and
  registration pages, and the account console of customer realms.
- `/resources/`, `/js/` – static theme assets and `keycloak.js`.
- `/robots.txt`

Everything else (the `/` welcome page, `/admin`, Keycloak's admin REST API)
stays behind OpenHost owner auth. Because `public_paths` matching is
prefix-based, `/realms/` would also expose the **master** realm; the
in-container proxy therefore additionally rejects `/admin` and
`/realms/master` (with 404) for any request the OpenHost router did not
stamp with `X-OpenHost-Is-Owner: true`. The router strips client-supplied
`X-OpenHost-*` headers, so that header cannot be forged. Net effect:

- Anonymous internet users: customer realm logins + OIDC only.
- OpenHost zone owner: everything, including the admin console — with
  **automatic login** (see below).

## Owner SSO / admin bootstrap

The OpenHost owner is logged into the Keycloak admin console
automatically; there is no Keycloak login form to fill in. How it works:

1. On every container start, `start.sh` generates a random per-boot
   bootstrap admin (`openhost-sso-<hex>` + random password) via
   `kc.sh bootstrap-admin user`. The credentials live only in process
   environments — **nothing secret is ever written to
   `$OPENHOST_APP_DATA_DIR`**, which other apps such as file-browser can
   read.
2. When a request stamped `X-OpenHost-Is-Owner: true` navigates to a
   non-public page without a Keycloak session, `auth_proxy.py` drives
   Keycloak's own browser login on loopback with those credentials and
   replays the resulting `KEYCLOAK_*` session cookies onto the owner's
   browser (plus a short-lived `OPENHOST_KC_SSO` marker cookie, since
   Keycloak scopes its cookies to `/realms/master/` where the proxy can't
   see them). The admin console's OIDC flow then completes silently.
3. Once Keycloak is up, the proxy deletes stale `openhost-sso-*` users
   left over from previous boots, so exactly one per-boot admin exists.

Consequences worth knowing:

- Logging out of the admin console just triggers a fresh silent auto-login
  on the next navigation; "logout" is effectively a no-op for the owner.
- Anonymous visitors on public paths are never auto-logged-in.
- Keycloak shows a "temporary admin user" notice in the admin console;
  that user is this boot's SSO account. You can create permanent admin
  users for other humans, but the temporary one is recreated every boot
  by design — don't rely on deleting it for security (it is unreachable
  without OpenHost owner auth anyway).
- If auto-login ever breaks, recover with
  `podman exec openhost-keycloak gosu keycloak /opt/keycloak/bin/kc.sh
  bootstrap-admin user --optimized` on the host, then log in manually at
  `/admin/` (owner-gated).

## The `openhost-customers` realm

`realms/openhost-customers.json` is imported on startup. Import only
happens when the realm doesn't already exist, so changes made later in the
admin console persist. Defaults:

- Self-registration **disabled** — enable it (Realm settings → Login) after
  configuring SMTP (Realm settings → Email), otherwise password reset and
  email verification cannot work.
- Email as username, brute-force protection on, 12-char minimum passwords,
  refresh-token revocation on, login/admin event logging on (30-day
  retention).

### Registering a downstream service (e.g. billing portal, vm-manager)

1. Admin console → realm `openhost-customers` → Clients → Create client.
2. OpenID Connect, client ID e.g. `billing-portal`, client authentication
   ON (confidential), valid redirect URIs limited to the service's callback
   URL.
3. The service uses standard OIDC with discovery URL:
   `https://keycloak.<zone>/realms/openhost-customers/.well-known/openid-configuration`

### Admin REST API automation

`/admin/` is owner-gated, so external automation can't call the Keycloak
admin REST API anonymously. Options, in order of preference:

- Preferred: run the automation next to Keycloak and call it on loopback,
  e.g. `podman exec openhost-keycloak curl http://127.0.0.1:8081/admin/...`
  (curl ships in the image for exactly this), or run it as an app on the
  same OpenHost zone calling through the router with the owner's API token.
- If you need direct service-account access from outside, open the surface
  in BOTH layers: add `"/admin/realms/"` to `public_paths` in
  `openhost.toml` AND remove (or narrow) the `("admin",)` entry in
  `OWNER_ONLY_SEGMENT_PREFIXES` in `auth_proxy.py` — otherwise the
  in-container proxy still 404s the request. Keycloak fully enforces its
  own bearer-token auth on the admin REST API either way; the gates are
  defense in depth, not the only lock.

## Installing

Install like any OpenHost app: dashboard → Add App →
`https://github.com/imbue-openhost/openhost-keycloak`. Note this repo is
private, so the OpenHost instance needs GitHub authorization (the add-app
flow offers it) or a deploy token; alternatively mirror the repo somewhere
the instance can clone. First build takes a few minutes (Keycloak dist +
Postgres image layers).

## Operational notes

- **Memory**: 2 GiB for the app; the JVM heap is capped at 1 GiB in
  `start.sh` so Keycloak, Postgres, and the proxy fit under the cgroup
  limit.
- **Cold start**: the proxy serves a 200 placeholder on `/` and a static
  `/_healthz` so OpenHost's 60-second readiness window is met even though
  Keycloak takes ~30s+ to start (the Dockerfile pre-builds the optimized
  Keycloak config to keep this as short as possible).
- **Hostname**: Keycloak runs with `KC_HOSTNAME_STRICT=false` and
  `KC_PROXY_HEADERS=xforwarded`; issuer URLs derive from
  `X-Forwarded-Host`/`X-Forwarded-Proto`, which only the OpenHost router
  (and our proxy) set. TLS terminates at the zone's Caddy.
- **Postgres password** is a fixed constant (`keycloak-loopback-only`).
  Postgres listens on loopback inside the container only; the constant is
  deliberately not a secret, mirroring the pattern used by other OpenHost
  apps with in-container databases.
- **Upgrades**: bump the Keycloak tag in the Dockerfile and redeploy;
  Keycloak migrates the schema automatically on start. Take note of the
  [Keycloak upgrade guide](https://www.keycloak.org/docs/latest/upgrading/)
  for major-version jumps.

## Repo layout

```
Dockerfile                     # KC dist (optimized build) + Ubuntu/Postgres/JRE
openhost.toml                  # manifest: port 8080, public_paths, 2 GiB
start.sh                       # supervisor: postgres + keycloak + proxy
auth_proxy.py                  # routed-port front proxy + owner SSO (stdlib only)
realms/openhost-customers.json # imported-on-first-boot customer realm
```
