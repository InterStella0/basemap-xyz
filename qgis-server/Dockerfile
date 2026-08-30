# Pinned to match the QGIS Desktop version that authored project.qgz. `ltr` was 3.44 while the
# project was saved by 4.2.1, and QGIS warns "Problems may occur" when loading a project from a
# newer version — a silently mis-rendered basemap is worse than a build that needs one bump.
# Bump this together with the desktop version you save the project from.
ARG QGIS_VERSION=4.2.1
FROM qgis/qgis-server:${QGIS_VERSION}

ENV TZ=UTC
ENV QGIS_PREFIX_PATH=/usr
ENV QGIS_SERVER_LOG_STDERR=1
ENV QGIS_SERVER_LOG_LEVEL=2

# The project is baked into the image rather than mounted, so a deployed tag is a reproducible
# pairing of cartography and server version.
COPY project/project.qgz /home/qgis/project/project.qgz

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
# The image's own script brings up Xvfb (QGIS needs a display even headless), spawn-fcgi on :9993
# and an internal nginx on :80. We use that nginx's /ows/ endpoint, so no SKIP_NGINX here.
CMD ["/usr/local/bin/start-xvfb-nginx.sh"]
