# Proton Mail Bridge, headless, from Proton's OFFICIAL Debian package (no third-party image).
# The package is pinned by version and SHA-256; that checksum was taken from a download whose signature was
# verified against Proton's Bridge signing key D51E 64D3 E63E DC3E EF78 64CE E2C7 5D68 E623 4B07
# (bridge_pubkey.gpg from github.com/ProtonMail/proton-bridge releases). To update: download the new .deb and
# .deb.sig, `gpg --verify`, then change both ARGs.
FROM debian:bookworm-slim

ARG BRIDGE_VERSION=3.27.2-1
ARG BRIDGE_SHA256=cf08332dcc4a332c3592476855e2007e6daadbcdac11fab6af83470a3255a8dc

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg pass socat \
 && curl -fsSL -o /tmp/bridge.deb "https://proton.me/download/bridge/protonmail-bridge_${BRIDGE_VERSION}_amd64.deb" \
 && echo "${BRIDGE_SHA256}  /tmp/bridge.deb" | sha256sum -c - \
 && apt-get install -y --no-install-recommends /tmp/bridge.deb \
 && rm /tmp/bridge.deb \
 && apt-get purge -y curl && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 bridge
COPY docker/bridge-entrypoint.sh /usr/local/bin/bridge-entrypoint.sh
RUN chmod 755 /usr/local/bin/bridge-entrypoint.sh
USER bridge
WORKDIR /home/bridge
ENTRYPOINT ["/usr/local/bin/bridge-entrypoint.sh"]
CMD ["run"]
