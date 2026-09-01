#!/bin/sh
# Builds and pushes the three swarm images for compose.swarm.yaml.
#
# `docker stack deploy` never builds: nodes pull the prebuilt images from Docker Hub, so this
# script (or the equivalent) must run before every swarm deploy. Tags must match the `image:`
# lines in compose.swarm.yaml. Run from the repo root.
set -e

QGIS_VERSION=${QGIS_VERSION:-4.2.1}

echo "==> building and pushing interstella0/dark-basemap:api"
docker build --target final -t interstella0/dark-basemap:api .
docker push interstella0/dark-basemap:api

echo "==> building and pushing interstella0/dark-basemap:reverse"
docker build -f nginx/Dockerfile -t interstella0/dark-basemap:reverse .
docker push interstella0/dark-basemap:reverse

echo "==> building and pushing interstella0/dark-basemap:renderer"
docker build --build-arg QGIS_VERSION="${QGIS_VERSION}" \
  -t interstella0/dark-basemap:renderer qgis-server
docker push interstella0/dark-basemap:renderer

echo
echo "Done. On the manager (VPS), with .env present:"
echo "  docker stack deploy -c compose.swarm.yaml dark-basemap"
