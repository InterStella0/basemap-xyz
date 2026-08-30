# Built from the repo root (see compose.yaml) so that site/ can stay a top-level deliverable
# rather than hiding inside the proxy's build context.
FROM nginx:1.27-alpine

COPY nginx/nginx.base.conf /etc/nginx/nginx.conf
COPY nginx/nginx.conf /etc/nginx/conf.d/default.conf.template

# The single-page site ships inside this image. index.html is a template too, so the public URL in
# every copy-paste snippet is set at deploy time rather than hardcoded.
COPY site/ /var/www/site.template/

COPY nginx/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
