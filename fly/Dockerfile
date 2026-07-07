# Keycloak for Fly.io.
#
# Unlike the OpenHost image (../Dockerfile), this is a *stateless* Keycloak:
# no in-container Postgres and no auth proxy. Postgres lives in a separate
# Fly Managed Postgres cluster (persisted independently and shareable across
# instances); the Keycloak admin console is protected by Keycloak's own
# master-realm login (there is no OpenHost owner-gating layer on Fly).
#
# Stage 1 pre-builds an optimized server configuration (db=postgres, health
# endpoints, local cache) so runtime `start --optimized` is fast. Stage 2 is
# the official Keycloak image with that build copied in plus our realm import.

FROM quay.io/keycloak/keycloak:26.6.3 AS builder

# cache=ispn: distributed Infinispan cache. More than one instance exists
# whenever a zero-downtime deploy overlaps the old and new machine (and Fly
# runs 2 for HA), so a shared cache is required -- with cache=local each node
# has an isolated cache and sessions/logins on the "wrong" node are lost.
#
# The default JGroups discovery stack for ispn is jdbc-ping: nodes register
# in (and discover peers through) the configured Keycloak database, so no
# multicast and no custom cache XML are needed. The cluster transport bind
# address is set at runtime (fly.toml: KC_CACHE_EMBEDDED_NETWORK_BIND_ADDRESS)
# to Fly's private 6PN interface so peers can reach each other.
ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_CACHE=ispn \
    KC_HTTP_RELATIVE_PATH=/

RUN /opt/keycloak/bin/kc.sh build


FROM quay.io/keycloak/keycloak:26.6.3

COPY --from=builder /opt/keycloak/ /opt/keycloak/

# Realms here are imported on startup. Import only happens when the realm
# does not already exist, so admin-console changes persist across deploys.
COPY realms/ /opt/keycloak/data/import/

# Connection (KC_DB_URL/USERNAME/PASSWORD) and the public hostname are
# supplied at runtime via Fly secrets / fly.toml env. --import-realm seeds
# the openhost-customers realm on first boot.
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized", "--import-realm"]
