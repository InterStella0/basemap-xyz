#!/bin/sh
# Regenerates PROJECT_VERSION in .env from the current qgis-server/project/project.qgz, so the API
# flushes its tile cache on next start instead of waiting out TILE_TTL_DAYS. Run this after
# replacing project.qgz, then: docker compose up -d
set -eu

repo_root=$(cd "$(dirname "$0")/.." && pwd)
project_file="$repo_root/qgis-server/project/project.qgz"
env_file="$repo_root/.env"

if [ ! -f "$project_file" ]; then
    echo "sync-project-version: $project_file not found" >&2
    exit 1
fi

if [ ! -f "$env_file" ]; then
    echo "sync-project-version: $env_file not found; run 'cp default.env .env' first" >&2
    exit 1
fi

if command -v sha256sum >/dev/null 2>&1; then
    version=$(sha256sum "$project_file" | cut -d' ' -f1)
elif command -v shasum >/dev/null 2>&1; then
    version=$(shasum -a 256 "$project_file" | cut -d' ' -f1)
else
    echo "sync-project-version: need sha256sum or shasum on PATH" >&2
    exit 1
fi

tmp_file="$env_file.tmp.$$"
if grep -q '^PROJECT_VERSION=' "$env_file"; then
    sed "s/^PROJECT_VERSION=.*/PROJECT_VERSION=${version}/" "$env_file" > "$tmp_file"
else
    cp "$env_file" "$tmp_file"
    printf 'PROJECT_VERSION=%s\n' "$version" >> "$tmp_file"
fi
mv "$tmp_file" "$env_file"

echo "PROJECT_VERSION set to $version"
echo "Now run: docker compose up -d"
