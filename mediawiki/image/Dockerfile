# ═══════════════════════════════════════════════════════════════════════════
# papaia-addon-mediawiki — MediaWiki image
# by Fidonis GmbH · https://fidonis.de
#
# MediaWiki 1.43 (LTS) with the extensions the add-on relies on baked in, so
# a container start never reaches out to the network:
#   PluggableAuth + OpenIDConnect  Keycloak login with group sync
#   Lockdown                       namespace-level read restrictions
#   OAuth                          per-user sign-in for the MCP server
#   VisualEditor, Cite, ParserFunctions, WikiEditor  bundled with core
#
# Extensions are pinned to a commit on their REL1_43 branch rather than the
# branch head, and both base images to the tag and the digest of their image
# index, so two builds of the same tag start from the same layers. A rebuilt
# upstream tag (new Debian packages under the same MediaWiki release) is
# taken by moving the digest, together with a new add-on version.
#
# The image holds no add-on logic: LocalSettings.php, the entrypoint and the
# OAuth bootstrap live in ../runtime and are mounted read-only by
# docker-compose.yml, so an add-on update takes effect on the next start
# without a new image. A change in this directory needs a new image tag:
# <MediaWiki version>-<add-on version>, see papaia-app.yaml.
# ═══════════════════════════════════════════════════════════════════════════
FROM docker.io/library/composer:2.8.12@sha256:5248900ab8b5f7f880c2d62180e40960cd87f60149ec9a1abfd62ac72a02577c AS composer

FROM docker.io/library/mediawiki:1.43.11@sha256:9692cc01b83b4b0f9d775b0ac16ec681578f6c47fb5e67c59e6a9c204442b9f7

ARG PLUGGABLEAUTH_REF=afec61097c7b08e0c67641cc9cb5ee27b2d627f7
ARG OPENIDCONNECT_REF=975992f2978e11f66b1ad5fd64e005592ba2fec3
ARG LOCKDOWN_REF=acd8edcd542f8b057929908667d4962ee295cc0f
ARG OAUTH_REF=ce3cc800e24142c04654d59597b0f89c107e3dd2

COPY --from=composer /usr/bin/composer /usr/local/bin/composer

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Debian security updates that the base digest does not carry yet, each
# raised to exactly the fixed version (no blanket upgrade). Drop a line once
# the base image ships that version or a newer one.
#
# The build fails on purpose when a pin no longer applies: the Debian security
# mirror keeps only the newest version of a package, and `--only-upgrade` to a
# version older than the one in the base digest would be a downgrade. Both
# break a rebuild of an already released tag (and the compose `build:`
# fallback), instead of silently taking a different package set than the one
# that was scanned. So the pins move with every rebuild: re-pin each line to
# the current fixed version or drop it, and remove the whole block at the next
# wiki base image bump that carries these versions.
ARG DEBIAN_SECURITY_UPGRADES="\
libgssapi-krb5-2=1.21.3-5+deb13u2 \
libk5crypto3=1.21.3-5+deb13u2 \
libkrb5-3=1.21.3-5+deb13u2 \
libkrb5support0=1.21.3-5+deb13u2 \
linux-libc-dev=6.12.118-1"

# hadolint ignore=DL3008
RUN apt-get update \
    && read -r -a upgrades <<< "$DEBIAN_SECURITY_UPGRADES" \
    && apt-get install -y --no-install-recommends --only-upgrade "${upgrades[@]}" \
    && apt-get install -y --no-install-recommends git unzip \
    && fetch() { \
         git init -q "$2" \
         && git -C "$2" fetch -q --depth 1 "https://github.com/wikimedia/mediawiki-extensions-$1.git" "$3" \
         && git -C "$2" checkout -q FETCH_HEAD \
         && rm -rf "$2/.git"; \
       } \
    && fetch PluggableAuth  extensions/PluggableAuth  "$PLUGGABLEAUTH_REF" \
    && fetch OpenIDConnect  extensions/OpenIDConnect  "$OPENIDCONNECT_REF" \
    && fetch Lockdown       extensions/Lockdown       "$LOCKDOWN_REF" \
    && fetch OAuth          extensions/OAuth          "$OAUTH_REF" \
    && for ext in OpenIDConnect OAuth; do \
         COMPOSER_ALLOW_SUPERUSER=1 composer install --working-dir="extensions/$ext" \
           --no-dev --no-interaction --no-progress --optimize-autoloader || exit 1; \
       done \
    && rm /usr/local/bin/composer \
    && apt-get purge -y --auto-remove git unzip \
    && rm -rf /var/lib/apt/lists/* /root/.composer

# Uploads: PHP limits must cover MW_MAX_UPLOAD_MB (MediaWiki enforces the
# lower of its own limit and these).
RUN { \
      echo 'upload_max_filesize = 100M'; \
      echo 'post_max_size = 100M'; \
      echo 'memory_limit = 256M'; \
      echo 'expose_php = Off'; \
    } > /usr/local/etc/php/conf.d/papaia-mediawiki.ini

# The wiki is private, but Apache would serve /images/ to anyone who knows a
# file's URL. Files go through img_auth.php instead ($wgUploadPath in
# LocalSettings.php), which checks read permission per request.
RUN { \
      echo '<Directory /var/www/html/images>'; \
      echo '    Require all denied'; \
      echo '</Directory>'; \
      echo 'ServerTokens Prod'; \
      echo 'ServerSignature Off'; \
    } > /etc/apache2/conf-available/papaia-mediawiki.conf \
    && a2enconf papaia-mediawiki

# The MCP server is published under the wiki's own origin (<MW_SERVER>/mcp),
# so a single host, certificate and proxy entry serve both. Its OAuth
# discovery documents live at the root of that origin. The upstream is
# resolved per request: without the MCP service these paths answer 503 and
# the wiki is unaffected.
COPY mcp-proxy.conf /etc/apache2/conf-available/papaia-mediawiki-mcp.conf
RUN a2enmod proxy proxy_http && a2enconf papaia-mediawiki-mcp

# The published port speaks TLS (see runtime/entrypoint.sh, which provides
# the certificate before Apache starts). Port 80 stays plain http for the
# reverse proxy, the MCP server and the health check on the add-on network.
COPY tls.conf /etc/apache2/sites-available/papaia-mediawiki-tls.conf
RUN a2enmod ssl && a2ensite papaia-mediawiki-tls \
    && mkdir -p /etc/papaia-mediawiki/settings.d /var/lib/papaia-mediawiki \
       /var/lib/papaia-mediawiki-oauth /var/lib/papaia-mediawiki-mcp \
       /var/lib/papaia-mediawiki-tls

LABEL org.opencontainers.image.source="https://github.com/Fidonis/papaia-addons" \
      org.opencontainers.image.description="MediaWiki 1.43 with the extensions of the papaia mediawiki add-on"
