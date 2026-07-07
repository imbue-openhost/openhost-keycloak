# Keycloak on Fly.io

A **stateless** Keycloak deployment for [Fly.io](https://fly.io), as an
alternative to the OpenHost app in the repo root. Differences from the
OpenHost image:

| | OpenHost (repo root) | Fly.io (`fly/`) |
|---|---|---|
| Postgres | embedded in the container | separate **Fly Managed Postgres** cluster |
| Persistence | OpenHost app-data backups | the Postgres cluster (independent of Keycloak deploys) |
| `/admin` gating | OpenHost owner header + Keycloak login | **Keycloak login only** (no owner layer on Fly) |
| Auth proxy | `auth_proxy.py` | none — Keycloak listens on the routed port directly |
| TLS / forwarded headers | zone Caddy → OpenHost router | Fly's proxy (`KC_PROXY_HEADERS=xforwarded`) |

Because Postgres is a separate cluster, the Keycloak app holds no state:
deploys are rolling machine replacements, and the database is untouched.

> **Exposure note:** there is no OpenHost owner-gating on Fly, so `/admin` is
> reachable from the internet, protected only by Keycloak's master-realm
> login (standard for Keycloak). To lock it down further, restrict it via Fly
> private networking or an IP allowlist — see "Hardening" below.

## Files

```
fly/Dockerfile   # stateless Keycloak: optimized build + realm import, no PG/proxy
fly/fly.toml     # app config: hostname, proxy headers, 1 GB machine, health check
fly/README.md    # this file
realms/          # (repo root) openhost-customers.json, imported on first boot
```

## First-time setup

All commands run from the **repo root** (the build context must include
`realms/`). Org: `imbue-development`.

```sh
# 1. Create the app (name must be globally unique on Fly).
fly apps create openhost-keycloak --org imbue-development

# 2. Create the Managed Postgres cluster (Basic = cheapest, ~$38/mo + storage).
fly mpg create --org imbue-development --name openhost-keycloak-db \
    --region sjc --plan Basic --volume-size 10

# 3. Wire the DB into Keycloak. Get the cluster's connection string, then set
#    Keycloak's discrete DB secrets (Keycloak does not read a libpq URL):
#      KC_DB_URL      = jdbc:postgresql://<host>:<port>/<db>
#      KC_DB_USERNAME = <user>
#      KC_DB_PASSWORD = <password>
#    See "Wiring the database" below for the exact derivation.

# 4. Bootstrap admin (first boot only — see "Admin bootstrap").
fly secrets set --app openhost-keycloak \
    KC_BOOTSTRAP_ADMIN_USERNAME=admin \
    KC_BOOTSTRAP_ADMIN_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"

# 5. Deploy.
fly deploy --config fly/fly.toml --dockerfile fly/Dockerfile \
    --app openhost-keycloak
```

### Wiring the database

Fly Managed Postgres exposes two endpoints reachable over the org's private
network (`fly mpg status <cluster-id> --json` shows both):

- **pooled** — `pgbouncer.<cluster-id>.flympg.net` (PgBouncer, transaction mode)
- **direct** — `direct.<cluster-id>.flympg.net` (a real Postgres session)

Keycloak (already built with `KC_DB=postgres`) wants the connection split into
a JDBC URL plus username and password. **We use the direct endpoint**: at this
scale Keycloak's own Agroal pool is enough, and it avoids PgBouncer
transaction-pooling pitfalls with Keycloak's Liquibase schema migrations and
prepared statements. Set the three secrets from the cluster's credentials
(`fly mpg status <cluster-id> --json` → `credentials`):

```sh
fly secrets set --app openhost-keycloak --stage \
    KC_DB_URL="jdbc:postgresql://direct.<cluster-id>.flympg.net:5432/<dbname>" \
    KC_DB_USERNAME="<user>" \
    KC_DB_PASSWORD="<password>"
```

If you ever need the pooled endpoint instead (e.g. running several instances
with many connections), point `KC_DB_URL` at `pgbouncer.<cluster-id>.flympg.net`
and append `?prepareThreshold=0` to disable server-side prepared statements.

If DB credentials are rotated, re-derive and re-set these three secrets.

**This deployment's values** (cluster `openhost-keycloak-db`, id
`ey5qn0y2dj8o8zmw`, region `sjc`):

```
KC_DB_URL      = jdbc:postgresql://direct.ey5qn0y2dj8o8zmw.flympg.net:5432/fly-db
KC_DB_USERNAME = fly-user
```

## Admin bootstrap

Same model as the OpenHost app: on a **fresh database**, the
`KC_BOOTSTRAP_ADMIN_*` secrets create a temporary admin. Then:

1. Visit `https://openhost-keycloak.fly.dev/admin/` and log in.
2. Create a permanent, personal admin account for each human (master realm →
   Users → Create user, assign the `admin` role).
3. Delete the temporary `admin` user.
4. Remove the now-unused bootstrap secrets so they are not lying around:
   `fly secrets unset --app openhost-keycloak KC_BOOTSTRAP_ADMIN_USERNAME KC_BOOTSTRAP_ADMIN_PASSWORD`

Lost all admin access? Run a one-off recovery container:
`fly ssh console --app openhost-keycloak -C "/opt/keycloak/bin/kc.sh bootstrap-admin user --optimized"`.

## Verifying

```sh
fly logs --app openhost-keycloak                 # watch startup
# OIDC discovery for the customer realm should return JSON:
curl -s https://openhost-keycloak.fly.dev/realms/openhost-customers/.well-known/openid-configuration | head
```

The `openhost-customers` realm and downstream-client/service-account setup are
documented in the repo-root `README.md` — that all applies unchanged here.

## Deployments / upgrades

- **Routine deploy:** `fly deploy --config fly/fly.toml --dockerfile fly/Dockerfile --app openhost-keycloak`.
  Fly does a rolling replace of the (stateless) Keycloak machine; the database
  is untouched. The new machine runs Keycloak's automatic schema migration on
  start.
- **Keycloak version bump:** change both `26.6.3` tags in `fly/Dockerfile`,
  then deploy. Review the
  [Keycloak upgrade guide](https://www.keycloak.org/docs/latest/upgrading/)
  for major-version jumps.
- **Realm changes:** edit `realms/openhost-customers.json`. Note import only
  applies when the realm does **not** already exist; changing an existing
  realm is done in the admin console (persists in Postgres).

## Custom domain (later)

1. `fly certs add keycloak.yourdomain.com --app openhost-keycloak` and add the
   DNS records it prints.
2. Set `KC_HOSTNAME = "https://keycloak.yourdomain.com"` in `fly/fly.toml` and
   re-deploy. (Changing the public hostname changes OIDC issuer URLs, so any
   already-registered clients must be updated to match.)

## Clustering & deployments

This deployment runs Keycloak's **distributed Infinispan cache** (`KC_CACHE=ispn`,
set both in `fly/Dockerfile` and `fly/fly.toml`). It uses Keycloak's default
`jdbc-ping` discovery stack: nodes register in and find each other through the
shared Postgres — no multicast, no custom cache XML. JGroups binds to Fly's
private 6PN interface via `KC_CACHE_EMBEDDED_NETWORK_BIND_ADDRESS=match-address:fdaa:.*`.

Why this matters: a zero-downtime deploy briefly runs the **old and new
machine at once**, and Fly may run more than one for HA. With a *local* cache
each node is an island, so a login/session handled by one node is invisible to
the other during the overlap. The distributed cache makes that overlap (and
any steady-state multi-instance setup) correct.

- **One instance (default intent):** even at `count=1` this is a healthy
  single-node cluster; during a deploy the transient second node joins the
  cluster over jdbc-ping and drains cleanly.
- **Run N instances for HA / true zero-downtime:** `fly scale count N --app
  openhost-keycloak`. The DB and cache already support it; just raise the
  count. (More instances = more compute cost.)

Confirm cluster membership after a deploy with
`fly logs --app openhost-keycloak | grep -i "ISPN0000\|received new cluster view"`
— you should see the expected number of members.

## Hardening `/admin` (optional)

Options, roughly in increasing effort:
- Put the admin console behind a separate, non-public Fly service / WireGuard
  and only expose `/realms/`, `/resources/`, `/js/` publicly (mirrors the
  OpenHost `public_paths` split).
- Front it with an IP allowlist.
- Re-introduce a small front proxy (like the OpenHost `auth_proxy.py`, minus
  the owner-header logic) if you want path-level control at the app layer.
