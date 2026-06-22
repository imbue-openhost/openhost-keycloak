#!/usr/bin/env bash
#
# setup-keycloak-clients.sh — one-shot, idempotent provisioning of the Keycloak
# clients, scope, and mappers that OpenHost's downstream services assume exist.
#
# It creates, in the `openhost-customers` realm:
#
#   1. the shared `cert-api` client scope, carrying the two protocol mappers
#      vm-manager attaches to every per-instance client it mints:
#        - `subdomain`        (User Attribute -> `subdomain` claim)
#        - `cert-api-audience` (Audience -> `aud: openhost-cert-api`)
#   2. `vm-manager-provisioner` — the confidential admin client vm-manager
#      authenticates as to mint/revoke per-instance clients (service account
#      with `realm-management` -> manage-clients + manage-users).
#   3. `hosted-spaces` — the confidential authorization-code client the public
#      signup/dashboard site (imbue-hosted-spaces) logs users in with.
#
# cert-api itself needs no client (it only validates tokens). Per-instance
# `instance-<fqdn>` clients are created/revoked automatically by vm-manager and
# are NOT created here.
#
# Re-running is safe: every object is looked up first and only created/patched
# when needed. The script prints the credentials to copy into each service at
# the end.
#
# See docs/client-setup.md for the full walkthrough and the cross-repo contract.

set -euo pipefail

# ---------------------------------------------------------------- configuration
# Required:
#   KC_URL              Keycloak base URL, e.g. https://openhost-keycloak.fly.dev
#   KC_ADMIN_USER       master-realm admin username (bootstrap or a personal admin)
#   KC_ADMIN_PASSWORD   that admin's password
# Optional (sensible defaults):
#   REALM                   default openhost-customers
#   CERT_API_SCOPE          default cert-api          (must match vm-manager's cert_api_scope)
#   CERT_API_AUDIENCE       default openhost-cert-api (cert-api hardcodes this; -qa in QA mode)
#   PROVISIONER_CLIENT_ID   default vm-manager-provisioner
#   HOSTED_SPACES_CLIENT_ID default hosted-spaces
#   HOSTED_SPACES_BASE_URL  e.g. https://spaces.imbue.com — REQUIRED to create the
#                           hosted-spaces client (its redirect URIs derive from it).
#                           Leave unset to skip that client.

KC_URL="${KC_URL:-}"
KC_ADMIN_USER="${KC_ADMIN_USER:-}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:-}"
REALM="${REALM:-openhost-customers}"
CERT_API_SCOPE="${CERT_API_SCOPE:-cert-api}"
CERT_API_AUDIENCE="${CERT_API_AUDIENCE:-openhost-cert-api}"
PROVISIONER_CLIENT_ID="${PROVISIONER_CLIENT_ID:-vm-manager-provisioner}"
HOSTED_SPACES_CLIENT_ID="${HOSTED_SPACES_CLIENT_ID:-hosted-spaces}"
HOSTED_SPACES_BASE_URL="${HOSTED_SPACES_BASE_URL:-}"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "  $*" >&2; }
step() { echo >&2; echo "==> $*" >&2; }

command -v curl >/dev/null || die "curl is required"
command -v jq   >/dev/null || die "jq is required (brew install jq / apt-get install jq)"

[[ -n "$KC_URL" ]]            || die "set KC_URL (e.g. https://openhost-keycloak.fly.dev)"
[[ -n "$KC_ADMIN_USER" ]]     || die "set KC_ADMIN_USER (a master-realm admin)"
[[ -n "$KC_ADMIN_PASSWORD" ]] || die "set KC_ADMIN_PASSWORD"
KC_URL="${KC_URL%/}"

# ---------------------------------------------------------------- HTTP helpers
# LAST_STATUS / LAST_BODY are set by api(); api() never exits on HTTP errors so
# callers can branch on the status (e.g. 404 = "not found, create it").
LAST_STATUS=""
LAST_BODY=""

api() {
  # api METHOD PATH [JSON_BODY]   — PATH is relative to /admin/realms/$REALM
  local method="$1" path="$2" body="${3:-}"
  local url="$KC_URL/admin/realms/$REALM$path"
  local resp
  if [[ -n "$body" ]]; then
    resp=$(curl -sS -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -H "Content-Type: application/json" \
      -d "$body" \
      -w $'\n%{http_code}') || die "request failed: $method $path"
  else
    resp=$(curl -sS -X "$method" "$url" \
      -H "Authorization: Bearer $TOKEN" \
      -w $'\n%{http_code}') || die "request failed: $method $path"
  fi
  LAST_STATUS="${resp##*$'\n'}"
  LAST_BODY="${resp%$'\n'*}"
}

api_ok() {
  # like api(), but die on any non-2xx (use when failure is unrecoverable)
  api "$@"
  [[ "$LAST_STATUS" =~ ^2 ]] || die "$1 $2 -> HTTP $LAST_STATUS: $LAST_BODY"
}

# ---------------------------------------------------------------- admin token
step "Authenticating to $KC_URL as '$KC_ADMIN_USER' (master realm)"
TOKEN=$(curl -sS -X POST \
  "$KC_URL/realms/master/protocol/openid-connect/token" \
  -d grant_type=password \
  -d client_id=admin-cli \
  -d "username=$KC_ADMIN_USER" \
  -d "password=$KC_ADMIN_PASSWORD" \
  | jq -r '.access_token // empty') || die "token request failed"

if [[ -z "$TOKEN" ]]; then
  die "could not obtain an admin token. On the OpenHost deployment the in-container
       proxy blocks /realms/master for non-owner traffic, so this script must run
       owner-side or via 'podman exec' + kcadm.sh. On Fly the master realm is
       reachable directly. Check the URL and credentials."
fi
info "got admin token"

# Confirm the target realm exists (it is imported on Keycloak's first boot).
api "GET" ""
[[ "$LAST_STATUS" == "200" ]] || die "realm '$REALM' not found (HTTP $LAST_STATUS). It is imported on first boot; check the realm name / deployment."
info "realm '$REALM' present"

# ---------------------------------------------------------------- 1. cert-api scope
step "Client scope '$CERT_API_SCOPE' (+ subdomain & audience mappers)"

api_ok "GET" "/client-scopes"
SCOPE_ID=$(echo "$LAST_BODY" | jq -r --arg n "$CERT_API_SCOPE" '.[] | select(.name==$n) | .id' | head -n1)

if [[ -z "$SCOPE_ID" ]]; then
  api "POST" "/client-scopes" "$(jq -n --arg n "$CERT_API_SCOPE" '{
    name: $n,
    protocol: "openid-connect",
    description: "Carries the subdomain + aud:openhost-cert-api claims for per-instance cert-api tokens.",
    attributes: { "include.in.token.scope": "true", "display.on.consent.screen": "false" }
  }')"
  [[ "$LAST_STATUS" =~ ^2 ]] || die "create scope -> HTTP $LAST_STATUS: $LAST_BODY"
  api_ok "GET" "/client-scopes"
  SCOPE_ID=$(echo "$LAST_BODY" | jq -r --arg n "$CERT_API_SCOPE" '.[] | select(.name==$n) | .id' | head -n1)
  info "created scope '$CERT_API_SCOPE' ($SCOPE_ID)"
else
  info "scope '$CERT_API_SCOPE' already exists ($SCOPE_ID)"
fi

# Existing mappers on the scope (so we only add the missing ones).
api_ok "GET" "/client-scopes/$SCOPE_ID/protocol-mappers/models"
EXISTING_MAPPERS="$LAST_BODY"

ensure_mapper() {
  # ensure_mapper NAME JSON_BODY
  local name="$1" body="$2"
  if echo "$EXISTING_MAPPERS" | jq -e --arg n "$name" 'any(.[]; .name==$n)' >/dev/null; then
    info "mapper '$name' already present"
    return
  fi
  api "POST" "/client-scopes/$SCOPE_ID/protocol-mappers/models" "$body"
  [[ "$LAST_STATUS" =~ ^2 ]] || die "create mapper '$name' -> HTTP $LAST_STATUS: $LAST_BODY"
  info "created mapper '$name'"
}

ensure_mapper "subdomain" "$(jq -n '{
  name: "subdomain",
  protocol: "openid-connect",
  protocolMapper: "oidc-usermodel-attribute-mapper",
  config: {
    "user.attribute": "subdomain",
    "claim.name": "subdomain",
    "jsonType.label": "String",
    "access.token.claim": "true",
    "id.token.claim": "false",
    "userinfo.token.claim": "false",
    "multivalued": "false"
  }
}')"

ensure_mapper "cert-api-audience" "$(jq -n --arg aud "$CERT_API_AUDIENCE" '{
  name: "cert-api-audience",
  protocol: "openid-connect",
  protocolMapper: "oidc-audience-mapper",
  config: {
    "included.custom.audience": $aud,
    "access.token.claim": "true",
    "id.token.claim": "false"
  }
}')"

# ---------------------------------------------------------------- client helpers
find_client_uuid() {
  # find_client_uuid CLIENT_ID  -> echoes uuid or empty
  api_ok "GET" "/clients?clientId=$1"
  echo "$LAST_BODY" | jq -r '.[0].id // empty'
}

client_secret() {
  # client_secret UUID -> echoes the secret
  api_ok "GET" "/clients/$1/client-secret"
  echo "$LAST_BODY" | jq -r '.value // empty'
}

# ---------------------------------------------------------------- 2. provisioner client
step "Admin client '$PROVISIONER_CLIENT_ID' (vm-manager -> admin REST API)"

PROV_UUID="$(find_client_uuid "$PROVISIONER_CLIENT_ID")"
if [[ -z "$PROV_UUID" ]]; then
  api "POST" "/clients" "$(jq -n --arg id "$PROVISIONER_CLIENT_ID" '{
    clientId: $id,
    protocol: "openid-connect",
    description: "Trusted provisioning client: vm-manager mints/revokes per-instance clients via the admin REST API.",
    publicClient: false,
    serviceAccountsEnabled: true,
    standardFlowEnabled: false,
    directAccessGrantsEnabled: false,
    implicitFlowEnabled: false,
    redirectUris: []
  }')"
  [[ "$LAST_STATUS" =~ ^2 ]] || die "create $PROVISIONER_CLIENT_ID -> HTTP $LAST_STATUS: $LAST_BODY"
  PROV_UUID="$(find_client_uuid "$PROVISIONER_CLIENT_ID")"
  info "created client '$PROVISIONER_CLIENT_ID' ($PROV_UUID)"
else
  info "client '$PROVISIONER_CLIENT_ID' already exists ($PROV_UUID)"
fi

# Assign realm-management roles to its service-account user.
api_ok "GET" "/clients/$PROV_UUID/service-account-user"
SA_USER_ID=$(echo "$LAST_BODY" | jq -r '.id')

api_ok "GET" "/clients?clientId=realm-management"
RM_UUID=$(echo "$LAST_BODY" | jq -r '.[0].id // empty')
[[ -n "$RM_UUID" ]] || die "realm-management client not found in realm '$REALM'"

# Build the role-representation array for the roles we want, then POST (idempotent;
# Keycloak ignores roles already mapped).
ROLES_JSON="[]"
for role in manage-clients manage-users; do
  api_ok "GET" "/clients/$RM_UUID/roles/$role"
  ROLES_JSON=$(echo "$ROLES_JSON" | jq --argjson r "$LAST_BODY" '. + [$r]')
done
api "POST" "/users/$SA_USER_ID/role-mappings/clients/$RM_UUID" "$ROLES_JSON"
[[ "$LAST_STATUS" =~ ^2 ]] || die "assign realm-management roles -> HTTP $LAST_STATUS: $LAST_BODY"
info "ensured roles: manage-clients, manage-users"

PROV_SECRET="$(client_secret "$PROV_UUID")"

# ---------------------------------------------------------------- 3. hosted-spaces client
HS_SECRET=""
HS_CREATED="skipped"
if [[ -z "$HOSTED_SPACES_BASE_URL" ]]; then
  step "Public site client '$HOSTED_SPACES_CLIENT_ID' — SKIPPED (set HOSTED_SPACES_BASE_URL to create it)"
else
  HOSTED_SPACES_BASE_URL="${HOSTED_SPACES_BASE_URL%/}"
  REDIRECT_URI="$HOSTED_SPACES_BASE_URL/auth/callback"
  step "Public site client '$HOSTED_SPACES_CLIENT_ID' (auth-code login for $HOSTED_SPACES_BASE_URL)"

  HS_UUID="$(find_client_uuid "$HOSTED_SPACES_CLIENT_ID")"
  if [[ -z "$HS_UUID" ]]; then
    api "POST" "/clients" "$(jq -n \
      --arg id "$HOSTED_SPACES_CLIENT_ID" \
      --arg redirect "$REDIRECT_URI" \
      --arg origin "$HOSTED_SPACES_BASE_URL" '{
      clientId: $id,
      protocol: "openid-connect",
      description: "imbue-hosted-spaces public site: OIDC authorization-code login.",
      publicClient: false,
      standardFlowEnabled: true,
      directAccessGrantsEnabled: false,
      implicitFlowEnabled: false,
      serviceAccountsEnabled: false,
      redirectUris: [$redirect],
      webOrigins: [$origin],
      attributes: { "post.logout.redirect.uris": "+" }
    }')"
    [[ "$LAST_STATUS" =~ ^2 ]] || die "create $HOSTED_SPACES_CLIENT_ID -> HTTP $LAST_STATUS: $LAST_BODY"
    HS_UUID="$(find_client_uuid "$HOSTED_SPACES_CLIENT_ID")"
    HS_CREATED="created"
    info "created client '$HOSTED_SPACES_CLIENT_ID' ($HS_UUID)"
  else
    # Merge the desired redirect URI / web origin into the existing client so a
    # re-run heals drift without clobbering manually-added entries.
    api_ok "GET" "/clients/$HS_UUID"
    UPDATED=$(echo "$LAST_BODY" | jq \
      --arg redirect "$REDIRECT_URI" \
      --arg origin "$HOSTED_SPACES_BASE_URL" '
      .redirectUris = ((.redirectUris // []) + [$redirect] | unique) |
      .webOrigins   = ((.webOrigins   // []) + [$origin]   | unique)')
    api "PUT" "/clients/$HS_UUID" "$UPDATED"
    [[ "$LAST_STATUS" =~ ^2 ]] || die "update $HOSTED_SPACES_CLIENT_ID -> HTTP $LAST_STATUS: $LAST_BODY"
    HS_CREATED="updated"
    info "client '$HOSTED_SPACES_CLIENT_ID' already existed; ensured redirect URI '$REDIRECT_URI'"
  fi
  HS_SECRET="$(client_secret "$HS_UUID")"
fi

# ---------------------------------------------------------------- summary
ISSUER="$KC_URL/realms/$REALM"
cat >&2 <<EOF

================================================================
 Keycloak setup complete for realm '$REALM'
 Issuer: $ISSUER
================================================================
EOF

cat <<EOF

# ---- openhost-vm-manager (secrets service, or env in local/dev) ----
KEYCLOAK_ADMIN_CLIENT_ID=$PROVISIONER_CLIENT_ID
KEYCLOAK_ADMIN_CLIENT_SECRET=$PROV_SECRET
# vm-manager Settings: keycloak_url=$KC_URL  keycloak_realm=$REALM  cert_api_scope=$CERT_API_SCOPE

EOF

if [[ "$HS_CREATED" != "skipped" ]]; then
cat <<EOF
# ---- imbue-hosted-spaces ($HS_CREATED) ----
IMBUE_OIDC_ISSUER=$ISSUER
IMBUE_OIDC_CLIENT_ID=$HOSTED_SPACES_CLIENT_ID
IMBUE_OIDC_CLIENT_SECRET=$HS_SECRET

EOF
fi

cat <<EOF
# ---- openhost-cert-api ----
# No client to copy: cert-api only validates tokens. It requires every instance
# token to carry aud=$CERT_API_AUDIENCE and a subdomain claim, both supplied by
# the '$CERT_API_SCOPE' scope above (attached per-instance by vm-manager).
EOF
