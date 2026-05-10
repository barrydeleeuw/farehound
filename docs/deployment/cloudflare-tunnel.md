# Cloudflare Tunnel — exposing the Mini Web App

Telegram WebApps require **HTTPS**, so we need a public URL with a valid TLS cert. Barry already runs **two cloudflared instances** for `bdl-ha.net` (on his Mac as a system LaunchDaemon, and on the Pi as a `NetworkMode: host` Docker container). We reuse the Pi's existing cloudflared by adding one new ingress rule. No new daemon, no new tunnel — just one config edit, one DNS record, one container restart.

This is not the standard "install cloudflared from scratch" recipe. If you're shipping FareHound to someone without an existing cloudflared setup, see [Appendix A](#appendix-a-fresh-install) at the bottom.

## Existing setup (the one this doc assumes)

```
Mac LaunchDaemon: cloudflared tunnel run polyfish (tunnel 31c5f703-...)
  ingress:
    mirofish.bdl-ha.net    → http://localhost:5001  (Mac-local services)
    mirofish-ui.bdl-ha.net → http://localhost:3000

Pi Docker container "cloudflared" (NetworkMode: host, image cloudflare/cloudflared:latest)
  config: /mnt/data/supervisor/share/cloudflared/<...>.yml (mounted to /etc/cloudflared/)
  ingress:
    ssh.bdl-ha.net → SSH (used by `Host ha-tunnel` in ~/.ssh/config)
    [we add: farehound.bdl-ha.net → http://localhost:8081]
```

## Prerequisites — already satisfied

- ✅ `bdl-ha.net` domain on Cloudflare
- ✅ cloudflared running on the Pi as a Docker container with `NetworkMode: host`
- ✅ `cert.pem` already issued and bind-mounted into the Pi cloudflared container
- ✅ HA add-on `ports:` mapping for `8081/tcp` (added in v0.10.2 — the FareHound container's port 8081 is now reachable from the Pi host)

## The 4 steps to flip the Mini Web App on

### 1. Pick a hostname

Suggestion: `farehound.bdl-ha.net` (matches the `mirofish-*.bdl-ha.net` pattern).

### 2. Add an ingress rule to the Pi cloudflared config

SSH into the Pi:

```bash
ssh ha-tunnel  # or `ssh barry@homeassistant.local` on LAN
sudo find /mnt -name '*.yml' -path '*cloudflared*' 2>/dev/null
# Edit the config file the path above points at — likely /mnt/data/supervisor/share/cloudflared/config.yml
sudo nano /mnt/data/supervisor/share/cloudflared/config.yml
```

Add the new hostname **before** the catch-all:

```yaml
ingress:
  # ... your existing ssh.bdl-ha.net rule and any others stay above the catch-all
  - hostname: farehound.bdl-ha.net
    service: http://localhost:8081
  - service: http_status:404
```

`localhost:8081` works because the cloudflared container has `NetworkMode: host`, so it shares the Pi's host network — and the FareHound add-on's port 8081 is mapped to the host (v0.10.2+).

### 3. Add the DNS CNAME

From the Pi (cloudflared container has the cert):

```bash
sudo docker exec cloudflared cloudflared tunnel route dns <tunnel-name-or-id> farehound.bdl-ha.net
```

Get the tunnel name from `sudo docker exec cloudflared cloudflared tunnel list` first if you don't remember it.

Alternatively, do it in the Cloudflare dashboard: DNS → Add a CNAME — `farehound` → `<tunnel-id>.cfargotunnel.com` — Proxied (orange cloud).

### 4. Restart the cloudflared container

```bash
sudo docker restart cloudflared
sleep 5
sudo docker logs --tail 30 cloudflared
```

Look for the new ingress rule in the logs (cloudflared logs each on startup). Then test from any machine:

```bash
curl -I https://farehound.bdl-ha.net/routes
# Expect: HTTP/2 401 — that's correct, no Telegram initData = auth rejected
```

The 401 proves the tunnel reaches the FareHound web app and the auth gate fires.

## Telling FareHound to use the new URL

Settings → Add-ons → FareHound → Configuration → set `miniapp_url: https://farehound.bdl-ha.net` (no trailing slash) → Save → Restart the add-on.

In the add-on logs, the next Telegram alert uses the thin format. To revert: clear `miniapp_url`, restart. v0.9.0 rich format comes back.

## Telegram bot configuration (for in-app launches)

For the `📊 Open in FareHound` button to launch the Mini Web App **inside** Telegram (rather than the user's external browser), register the URL with @BotFather:

1. Message @BotFather.
2. `/mybots` → pick your bot → **Bot Settings** → **Configure Mini App** → **Edit Web App URL**.
3. Paste `https://farehound.bdl-ha.net`.

Without this, `web_app:` buttons fall back to opening in the user's browser. Page renders identically; it's just a worse mobile experience.

## Troubleshooting

**`curl https://farehound.bdl-ha.net/routes` returns 502**
The FareHound add-on isn't running or port 8081 isn't mapped to host. Check `sudo docker exec hassio_cli ha apps info 30bba4a3_farehound | grep -E 'state|version'` — `state` should be `started` and `version` should be `0.10.2` or later (the `ports:` mapping landed in 0.10.2).

**`401 initData invalid` on every request from inside Telegram**
Either `TELEGRAM_BOT_TOKEN` isn't exported in the FareHound add-on container, OR the bot's Mini App URL hasn't been configured in @BotFather (so Telegram doesn't pass `initData`).

**The bot still sends rich Telegram messages**
`miniapp_url` didn't propagate. Check from the Pi:
```bash
sudo docker exec $(sudo docker ps | grep farehound | awk '{print $1}') env | grep MINIAPP_URL
```
If empty, the HA option didn't land — restart the add-on.

**Tunnel was running but stops working after a Pi reboot**
The cloudflared container has `RestartPolicy: unless-stopped`, so it should come back automatically. If not, `sudo docker start cloudflared`.

---

## Appendix A — fresh install (only if no existing cloudflared)

If you're shipping this to someone without Barry's setup, the from-scratch path:

1. Install `cloudflared` (binary on the Pi host, OR the [cloudflared HA add-on](https://github.com/brenner-tobias/addon-cloudflared) — the add-on is easier on HAOS).
2. `cloudflared tunnel login` — opens a browser auth flow against your Cloudflare account.
3. `cloudflared tunnel create farehound` — creates a tunnel, writes credentials to `~/.cloudflared/<id>.json`.
4. Create `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: <tunnel-id>
   credentials-file: /root/.cloudflared/<tunnel-id>.json
   ingress:
     - hostname: farehound.<your-domain>
       service: http://localhost:8081
     - service: http_status:404
   ```
5. `cloudflared tunnel route dns farehound farehound.<your-domain>`
6. Run as a service (`cloudflared service install` for the binary path, or just enable the add-on for HA).
7. From there, follow steps 4 onward of the main recipe ("Telling FareHound to use the new URL").
