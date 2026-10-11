# One Caddy lock and build path for application gates and the shared TLS proxy.
FROM golang:1.27.2-alpine@sha256:85dc1069ac644ea3c527b177303a406eb3358192816cd7f9e5848eb658851673 AS build
WORKDIR /build
COPY helpers/caddy.go.mod go.mod
COPY helpers/caddy.go.sum go.sum
COPY helpers/caddy-main.go main.go
COPY helpers/caddy-build.sh build.sh
RUN CGO_ENABLED=0 sh build.sh

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab
ARG SOURCE_REVISION
LABEL org.opencontainers.image.source="https://github.com/nicodes/tools"
LABEL org.opencontainers.image.revision=$SOURCE_REVISION
COPY --from=build /build/caddy /usr/bin/caddy
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
# The host owns the listener, config, certificate volumes and capability policy.
USER 0:0
EXPOSE 80 443 443/udp 2019
ENTRYPOINT ["/usr/bin/caddy"]
CMD ["run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
