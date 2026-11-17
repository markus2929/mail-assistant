# Desktop setup (Arch): Bridge, Ollama, Docker

## 1. Proton Bridge
Install `protonmail-bridge` (AUR) and a secret-service keyring (`gnome-keyring` or `pass`).
Log in once, then open the account's IMAP/SMTP settings and note the **Bridge-generated IMAP
password** (not your Proton password). Make Bridge start with your session (autostart entry).
Check `protonmail-bridge --help` for a no-window/background flag on your version.
Verify: `ss -ltn | grep 1143` shows it listening on 127.0.0.1 only. Leave it that way.

## 2. Ollama
Keep the default `127.0.0.1:11434`. Verify: `curl -s http://127.0.0.1:11434/api/tags`
lists `qwen3:14b` (`ollama pull qwen3:14b` if not).

## 3. Docker
`sudo systemctl enable --now docker` so the stack comes back after a reboot
(`restart: unless-stopped`). Bridge only starts once you log in, so the app must tolerate
"IMAP unavailable" at boot (Phase 2 handles this with retries).

## Why host networking
Bridge and Ollama listen on the desktop's loopback only. A container on a normal Docker network
cannot reach that, so the app containers use `network_mode: host`. The trade-off is weaker
network isolation for those containers. They publish nothing, bind the dashboard to
127.0.0.1 by default, and drop all capabilities. The alternative is running the Python
services natively (uv + systemd --user); say so if you prefer that.
