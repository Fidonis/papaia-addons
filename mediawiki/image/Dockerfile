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
# branch head, so two builds of the same tag produce the same image.
# ═══════════════════════════════════════════════════════════════════════════
FROM docker.io/library/composer:2.8.12 AS composer

FROM docker.io/library/mediawiki:1.43.11

ARG PLUGGABLEAUTH_REF=afec61097c7b08e0c67641cc9cb5ee27b2d627f7
ARG OPENIDCONNECT_REF=975992f2978e11f66b1ad5fd64e005592ba2fec3
ARG LOCKDOWN_REF=acd8edcd542f8b057929908667d4962ee295cc0f
ARG OAUTH_REF=ce3cc800e24142c04654d59597b0f89c107e3dd2

COPY --from=composer /usr/bin/composer /usr/local/bin/composer

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# hadolint ignore=DL3008
RUN apt-get update \
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

# Copied into place by the entrypoint after install.php, which refuses to
# run while a LocalSettings.php exists.
COPY LocalSettings.php /usr/local/share/papaia-mediawiki/LocalSettings.php
COPY EnsureOAuthConsumer.php /usr/local/share/papaia-mediawiki/EnsureOAuthConsumer.php
COPY entrypoint.sh /usr/local/bin/papaia-mediawiki-entrypoint
RUN chmod 0755 /usr/local/bin/papaia-mediawiki-entrypoint \
    && mkdir -p /etc/papaia-mediawiki/settings.d /var/lib/papaia-mediawiki \
       /var/lib/papaia-mediawiki-oauth /var/lib/papaia-mediawiki-mcp

ENTRYPOINT ["/usr/local/bin/papaia-mediawiki-entrypoint"]
CMD ["apache2-foreground"]
