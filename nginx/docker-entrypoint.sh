#!/bin/sh
set -e

export API_HOST=${API_HOST:-api}
export API_PORT=${API_PORT:-3000}
export BASEMAP_PUBLIC_URL=${BASEMAP_PUBLIC_URL:-http://localhost:8080}
# Trailing slashes would produce "…//tiles/" in the snippets people copy.
BASEMAP_PUBLIC_URL=$(echo "$BASEMAP_PUBLIC_URL" | sed 's:/*$::')
export BASEMAP_PUBLIC_URL

echo "dark-basemap-xyz reverse proxy"
echo "  api:        ${API_HOST}:${API_PORT}"
echo "  public url: ${BASEMAP_PUBLIC_URL}"

# Rate limits key on the client address. Behind a CDN that address is the edge's, so recover the
# real one from X-Forwarded-For — but only for hops we were explicitly told to trust, since
# believing that header from anyone makes the limits trivially bypassable.
: > /etc/nginx/conf.d/real_ip.inc
if [ -n "${TRUSTED_PROXY_CIDR:-}" ]; then
  for cidr in ${TRUSTED_PROXY_CIDR}; do
    echo "set_real_ip_from ${cidr};" >> /etc/nginx/conf.d/real_ip.inc
  done
  echo "real_ip_header X-Forwarded-For;" >> /etc/nginx/conf.d/real_ip.inc
  echo "real_ip_recursive on;" >> /etc/nginx/conf.d/real_ip.inc
  echo "  trusting proxies: ${TRUSTED_PROXY_CIDR}"
else
  echo "  trusting proxies: none (set TRUSTED_PROXY_CIDR if behind a CDN or load balancer)"
fi

envsubst '${API_HOST} ${API_PORT}' \
  < /etc/nginx/conf.d/default.conf.template \
  > /etc/nginx/conf.d/default.conf

# Naming the variables explicitly is load-bearing: a bare `envsubst` would eat every $identifier in
# the site's CSS and JavaScript too.
mkdir -p /var/www/site
for file in /var/www/site.template/*; do
  name=$(basename "$file")
  case "$name" in
    *.html|*.css|*.js)
      envsubst '${BASEMAP_PUBLIC_URL}' < "$file" > "/var/www/site/$name"
      ;;
    *)
      cp "$file" "/var/www/site/$name"
      ;;
  esac
done

nginx -t
exec nginx -g "daemon off;"
