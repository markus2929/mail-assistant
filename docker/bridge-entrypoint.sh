#!/bin/sh
# Proton Mail Bridge in a container.
#   run  (default): Bridge without a UI; its IMAP port (127.0.0.1:1143 inside the container) is made reachable
#                   for the other containers on port 1144 of the compose network - never published on the host.
#   init          : Bridge's command line, to log in once (`login`) and show the IMAP password (`info`).
# The keychain is `pass` with a GPG key without passphrase, both in the /home/bridge volume - that volume holds
# your Proton session: keep it on an encrypted disk and back it up like a secret.
set -eu
export GNUPGHOME="$HOME/.gnupg"
if [ ! -f "$HOME/.password-store/.gpg-id" ]; then
    echo "first start: creating the keychain (GPG key + pass store) in the volume"
    mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"
    gpg --batch --passphrase '' --quick-gen-key 'mail-assistant bridge keychain' default default never
    pass init 'mail-assistant bridge keychain'
fi
BRIDGE=/usr/lib/protonmail/bridge/bridge       # the bridge itself (the launcher would self-update)
case "${1:-run}" in
    init)
        exec "$BRIDGE" --cli
        ;;
    run)
        socat TCP-LISTEN:1144,fork,reuseaddr TCP:127.0.0.1:1143 &
        exec "$BRIDGE" --noninteractive
        ;;
    *)
        exec "$@"
        ;;
esac
