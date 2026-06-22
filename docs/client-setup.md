# Setting up downstream service clients

This Keycloak runs the `openhost-customers` realm as the central identity
provider for the OpenHost services. Before those services can talk to it, the
realm needs a small, fixed set of clients, one shared client scope, and the
mappers that scope carries. This doc — and the script next to it,
[`scripts/setup-keycloak-clients.sh`](../scripts/setup-keycloak-clients.sh) —
get all of that in place in one shot.

After running it, the only remaining work is to **copy the printed credentials
into each service and deploy them**. Keycloak itself is fully configured.

## What gets created

Everything lives in the **`openhost-customers`** realm (never `master`).

```
                       openhost-customers realm
  ┌──────────────────────────────────────────────────────────────────┐
  │  cert-api (client scope)            vm-manager-provisioner (client)│
  │   ├─ mapper: subdomain               ├─ service account ON         │
  │   └─ mapper: aud=openhost-cert-api   └─ roles: manage-clients,     │
  │                                              manage-users          │
  │                                                                    │
  │  hosted-spaces (client)                                            │
  │   └─ auth-code login for the public site                          │
  │                                                                    │
  │  instance-<fqdn> (clients)   ← minted automatically by vm-manager  │
  │   ├─ service account: subdomain=<fqdn>                             │
  │   └─ default scope: cert-api  (supplies the two mappers above)     │
  └──────────────────────────────────────────────────────────────────┘
```

| # | Object | For | Why |
|---|---|---|---|
| 1 | `cert-api` **client scope** + `subdomain` and `cert-api-audience` mappers | openhost-cert-api | cert-api requires every instance token to carry a `subdomain` claim and `aud: openhost-cert-api`. vm-manager attaches this scope to each per-instance client it mints, so the mappers live in **one** place instead of on N clients. |
| 2 | `vm-manager-provisioner` **client** (confidential, service account, `realm-management` → `manage-clients` + `manage-users`) | openhost-vm-manager | The trusted identity vm-manager authenticates as to call the admin REST API and mint/revoke the per-instance clients. |
| 3 | `hosted-spaces` **client** (confidential, authorization-code flow) | imbue-hosted-spaces | The public signup/dashboard site logs customers in through this client. Only created when you pass its base URL. |

Two things are deliberately **not** created here:

- **openhost-cert-api has no client of its own** — it only validates JWTs
  (signature, issuer, `aud`, `subdomain`). It holds no Keycloak credentials.
- **`instance-<fqdn>` clients** are created and revoked automatically by
  vm-manager during provisioning. You never touch them. The `subdomain` mapper
  on the `cert-api` scope is exactly the "mapper vm-manager attaches to the
  per-instance service clients."

The cross-repo contract these match lives in openhost-cert-api's
`docs/auth-keycloak-structure.md` and openhost-vm-manager's
`docs/keycloak-setup.md`.

## Prerequisites

- `curl` and `jq` on the machine you run the script from.
- The Keycloak base URL, e.g. `https://openhost-keycloak.fly.dev`.
- A **master-realm admin** username + password. On a fresh deploy this is the
  `KC_BOOTSTRAP_ADMIN_*` account; ideally create a personal admin first (see the
  repo `README.md` / `fly/README.md` "Admin bootstrap") and use that.
- The `openhost-customers` realm already imported (it is, on Keycloak's first
  boot).

> **Auth-path caveat.** The script obtains its admin token from the master
> realm's token endpoint (`/realms/master/...`, `admin-cli` password grant).
> - **On Fly** (`fly/`) the master realm is reachable directly — the script
>   just works against the public URL.
> - **On the OpenHost app** (repo root) the in-container `auth_proxy.py` blocks
>   `/realms/master` for any request without the owner header. Run the script
>   from an owner-authenticated context, or do the equivalent calls inside the
>   container with `podman exec openhost-keycloak ... kcadm.sh` (see the
>   appendix in vm-manager's `docs/keycloak-setup.md`).

## Running it

```sh
export KC_URL=https://openhost-keycloak.fly.dev
export KC_ADMIN_USER=admin
export KC_ADMIN_PASSWORD='…'                 # master-realm admin
export HOSTED_SPACES_BASE_URL=https://spaces.imbue.com   # omit to skip that client

./scripts/setup-keycloak-clients.sh
```

The script is **idempotent** — every object is looked up before it's created,
and re-running heals drift (e.g. re-adds the hosted-spaces redirect URI) without
clobbering manual edits. Progress goes to stderr; the credentials block is
written to stdout so you can capture it:

```sh
./scripts/setup-keycloak-clients.sh > keycloak-creds.env   # then move secrets into each service's store
```

### Configuration

| Variable | Required | Default | Notes |
|---|---|---|---|
| `KC_URL` | yes | — | Keycloak base URL (no trailing slash needed). |
| `KC_ADMIN_USER` | yes | — | master-realm admin. |
| `KC_ADMIN_PASSWORD` | yes | — | its password. |
| `HOSTED_SPACES_BASE_URL` | for the site client | *(unset → skipped)* | e.g. `https://spaces.imbue.com`. Redirect URI becomes `<base>/auth/callback`. |
| `REALM` | no | `openhost-customers` | |
| `CERT_API_SCOPE` | no | `cert-api` | Must match vm-manager's `cert_api_scope` setting. |
| `CERT_API_AUDIENCE` | no | `openhost-cert-api` | cert-api hardcodes this; QA mode uses `openhost-cert-api-qa`. |
| `PROVISIONER_CLIENT_ID` | no | `vm-manager-provisioner` | Becomes `KEYCLOAK_ADMIN_CLIENT_ID`. |
| `HOSTED_SPACES_CLIENT_ID` | no | `hosted-spaces` | |

## Wiring the output into each service

The script prints exactly what each service needs. Move these into each
service's secret store (the OpenHost **secrets service** in prod, or plain env
vars in local/dev) and deploy.

### openhost-vm-manager

Secrets:

| Key | Value |
|---|---|
| `KEYCLOAK_ADMIN_CLIENT_ID` | `vm-manager-provisioner` |
| `KEYCLOAK_ADMIN_CLIENT_SECRET` | *(printed)* |

Settings:

| Setting | Value |
|---|---|
| `keycloak_url` | your `KC_URL` |
| `keycloak_realm` | `openhost-customers` |
| `cert_api_scope` | `cert-api` |

### imbue-hosted-spaces

| Env var | Value |
|---|---|
| `IMBUE_OIDC_ISSUER` | `<KC_URL>/realms/openhost-customers` |
| `IMBUE_OIDC_CLIENT_ID` | `hosted-spaces` |
| `IMBUE_OIDC_CLIENT_SECRET` | *(printed)* |

### openhost-cert-api

Nothing to copy — cert-api validates tokens and holds no Keycloak credentials.
It just needs to be configured with the matching issuer and audience
(`openhost-cert-api`); see its own config.

## Verify

```sh
KC=$KC_URL
# vm-manager-provisioner can get a token and exercise its roles (200 with [], not 403):
TOKEN=$(curl -s -X POST "$KC/realms/openhost-customers/protocol/openid-connect/token" \
  -d grant_type=client_credentials -d client_id=vm-manager-provisioner \
  -d client_secret='<printed secret>' | jq -r .access_token)
curl -s "$KC/admin/realms/openhost-customers/clients?clientId=nonexistent" \
  -H "Authorization: Bearer $TOKEN"

# the cert-api scope exists with both mappers:
curl -s "$KC/admin/realms/openhost-customers/client-scopes" \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | select(.name=="cert-api") | .name'
```

Then provision a space through vm-manager: it should create an `instance-<fqdn>`
client, set its `subdomain` attribute, attach the `cert-api` scope, and the host
should obtain its certificate from cert-api. For deeper token/audience
troubleshooting see the "Troubleshooting" section of vm-manager's
`docs/keycloak-setup.md`.
