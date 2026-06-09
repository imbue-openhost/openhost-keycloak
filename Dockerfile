# Keycloak + Postgres in a single OpenHost app container.
#
# Stage 1 takes the official Keycloak distribution and pre-builds an
# optimized server configuration (db=postgres, health endpoints enabled)
# so runtime startup is as fast as possible -- OpenHost's readiness probe
# only waits 60 seconds after container start.
#
# The final stage is Ubuntu Noble (via eclipse-temurin, Keycloak 26.x
# requires Java 21) with Postgres 16 from apt, the Keycloak dist copied
# in, and a small stdlib-only Python auth proxy fronting everything on
# the OpenHost-routed port.

FROM quay.io/keycloak/keycloak:26.6.3 AS keycloak-dist

ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_HTTP_RELATIVE_PATH=/

RUN /opt/keycloak/bin/kc.sh build


FROM eclipse-temurin:21-jre-noble

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-16 \
        postgresql-client-16 \
        python3 \
        gosu \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=keycloak-dist /opt/keycloak /opt/keycloak

# The postgres user/group come from the postgresql-16 package. Create a
# dedicated unprivileged user for the Keycloak server process.
RUN useradd --system --user-group --home-dir /opt/keycloak --shell /usr/sbin/nologin keycloak \
    && chown -R keycloak:keycloak /opt/keycloak \
    && mkdir -p /var/run/postgresql \
    && chown postgres:postgres /var/run/postgresql

# Realms in this directory are imported on startup (existing realms are
# left untouched, so admin-console changes persist across restarts).
COPY realms/ /opt/keycloak/data/import/
RUN chown -R keycloak:keycloak /opt/keycloak/data/import

COPY auth_proxy.py /app/auth_proxy.py
COPY start.sh /app/start.sh
RUN chmod 0755 /app/auth_proxy.py /app/start.sh

EXPOSE 8080

CMD ["/app/start.sh"]
