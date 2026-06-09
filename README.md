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
- OpenHost zone owner: everything, including the admin console.

The Keycloak admin console still requires a Keycloak admin login on top of
OpenHost owner auth (see bootstrap below) — OpenHost auth gates *reach*,
Keycloak auth gates *privilege*.

## First boot / admin bootstrap

On first boot, `start.sh` generates a random temporary admin password and
prints it to the **container log only** (nothing secret is ever written to
`$OPENHOST_APP_DATA_DIR`, which other apps such as file-browser can read):

```
[start]   username: admin
[start]   password: <random>
```

Read it via the app logs on the OpenHost dashboard (or
`podman logs openhost-keycloak`). Then:

1. Log in at `https://keycloak.<zone>/admin/` (you must be logged in as the
   OpenHost owner to reach it).
2. Create a permanent admin user in the master realm and delete the
   temporary `admin` user (Keycloak marks it as temporary and nags until
   you do).

If the very first boot fails before Keycloak initializes, a fresh password
is generated and printed on the next start. The detection is DB-backed (no
marker files): bootstrap happens whenever the master realm doesn't exist
yet.

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

- Run the automation as an app on the same OpenHost zone and call Keycloak
  through the router with the zone owner's API token (the router stamps the
  owner header and forwards the request; note the `Authorization` header is
  used by the OpenHost router, so use Keycloak's `access_token` query/body
  forms or run the call from a browser-session context).
- If you need direct service-account access from outside, add
  `"/admin/realms/"` to `public_paths` — Keycloak fully enforces its own
  bearer-token auth there; gating it is defense in depth, not the only
  lock.

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
auth_proxy.py                  # routed-port front proxy (stdlib only)
realms/openhost-customers.json # imported-on-first-boot customer realm
```
