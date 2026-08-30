# syntax=docker/dockerfile:1
ARG RUST_VERSION=1.96.1
ARG APP_NAME=dark-basemap-xyz

FROM rust:${RUST_VERSION}-alpine AS build
ARG APP_NAME
WORKDIR /app

# No openssl-dev: reqwest is built without TLS (the renderer is reached over plain http:// inside
# the compose network), which keeps this stage down to the musl toolchain.
RUN apk add --no-cache clang lld musl-dev

RUN --mount=type=bind,source=src,target=src \
    --mount=type=bind,source=Cargo.toml,target=Cargo.toml \
    --mount=type=bind,source=Cargo.lock,target=Cargo.lock \
    --mount=type=cache,target=/app/target/ \
    --mount=type=cache,target=/usr/local/cargo/git/db \
    --mount=type=cache,target=/usr/local/cargo/registry/ \
    cargo build --locked --release && \
    cp ./target/release/$APP_NAME /bin/server

# CI gate, reached only via `--target test`. Nothing on the path to `final` depends on it, so a
# production build never pays for it.
FROM build AS test
RUN --mount=type=bind,source=src,target=src \
    --mount=type=bind,source=Cargo.toml,target=Cargo.toml \
    --mount=type=bind,source=Cargo.lock,target=Cargo.lock \
    --mount=type=cache,target=/app/target/ \
    --mount=type=cache,target=/usr/local/cargo/git/db \
    --mount=type=cache,target=/usr/local/cargo/registry/ \
    cargo test --locked --all-targets

FROM alpine:3.21 AS final

ARG UID=10001
RUN apk add --no-cache curl

RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

# The tile cache is a volume shared with nginx; this container is the only writer.
RUN mkdir -p /var/cache/tiles && chown -R appuser:appuser /var/cache/tiles

USER appuser
WORKDIR /app

COPY --from=build /bin/server /bin/

EXPOSE 3000
CMD ["/bin/server"]
