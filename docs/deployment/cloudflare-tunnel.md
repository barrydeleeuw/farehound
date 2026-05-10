# Cloudflare Tunnel — exposing the Mini Web App

The Mini Web App ships in v0.10.0 listening on `localhost:8081` inside the FareHound add-on container. Telegram WebApps require **HTTPS**, so we need a public URL with a valid TLS cert. **Cloudflare Tunnel** is the cleanest path on a Pi: no port forwarding, no static IP, free for personal use, automatic TLS.

This doc is a recipe — Barry runs the commands himself. The R8 release ships the code; the deploy is a one-time setup once the code is on `main`.

---

## Prerequisites

- A Cloudflare account (free tier is fine).
- A domain on Cloudflare. Either:
  - A real domain you own (e.g. `farehound.barrydeleeuw.com`) with its DNS managed by Cloudflare, OR
  - A free `*.trycloudflare.com` URL (Quick Tunnels — fine for testing, not stable for daily use because the URL changes on restart).

For ongoing use, **use your own domain**. The bot's `MINIAPP_URL` env var has to point somewhere stable; a rotating Quick Tunnel URL means re-deploying the bot every restart.

## One-time tunnel setup

### 1. Install `cloudflared` on the Pi

The HA host runs Linux. SSH in and install via the official APT repo:

```bash
ssh barry@homeassistant.local
# On the Pi:
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /tmp/cloudflared
sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
cloudflared --version
```

### 2. Authenticate to your Cloudflare account

```bash
cloudflared tunnel login
```

Opens a browser-flow auth URL. Pick your domain. A cert is written to `~/.cloudflared/cert.pem`.

### 3. Create the tunnel

```bash
cloudflared tunnel create farehound
```

Records a tunnel ID. Files: `~/.cloudflared/<tunnel-id>.json` (credentials).

### 4. Configure ingress

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <tunnel-id>
credentials-file: /home/barry/.cloudflared/<tunnel-id>.json

ingress:
  - hostname: farehound.barrydeleeuw.com
    service: http://localhost:8081
  - service: http_status:404
```

Replace `farehound.barrydeleeuw.com` with your chosen subdomain.

### 5. Route DNS

```bash
cloudflared tunnel route dns farehound farehound.barrydeleeuw.com
```

This creates the proxied DNS CNAME on Cloudflare automatically.

### 6. Test the tunnel manually

```bash
cloudflared tunnel run farehound
```

In another terminal (or another machine), open `https://farehound.barrydeleeuw.com/` — should hit FareHound's `/routes` page. If the FareHound add-on isn't running, you'll get a 502 — start the add-on first.

### 7. Run the tunnel as a systemd service

Stop the manual run with Ctrl-C, then:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared
```

The service auto-restarts on reboot.

---

## Telling FareHound about the URL

Once the tunnel is live and reachable:

1. Open Home Assistant → Settings → Add-ons → FareHound → Configuration.
2. Set **`miniapp_url`** to your tunnel URL (e.g. `https://farehound.barrydeleeuw.com`). No trailing slash.
3. Save → Restart the add-on.
4. Tail the logs:
   ```bash
   sudo docker exec hassio_cli ha apps logs 30bba4a3_farehound | tail -20
   ```
   You should see:
   - `Mini Web App listening on port 8081`
   - The first Telegram alert thereafter uses the thin format with `📊 Open in FareHound` button.

5. Send `/status` to the bot. If the bot starts using thin alerts immediately, the flag is wired correctly.

To revert to the v0.9.0 rich Telegram format: clear the `miniapp_url` field, restart the add-on. No code change needed.

---

## Telegram bot configuration

For the `web_app` button to launch the Mini Web App **inside Telegram** (rather than punting to the user's browser), the bot must be registered with a Mini Web App URL via @BotFather:

1. Message @BotFather on Telegram.
2. `/mybots` → pick your bot → **Bot Settings** → **Configure Mini App** → **Edit Web App URL**.
3. Paste your tunnel URL (e.g. `https://farehound.barrydeleeuw.com`).
4. Done. New `web_app` buttons in alerts now open in-app.

If you skip this step, the buttons still work but open in the user's browser instead of the Telegram in-app webview. The page is identical either way.

---

## Verification checklist

- [ ] `https://<your-tunnel-url>/` loads (you'll get 401 — that's correct, no `initData` outside Telegram).
- [ ] Tap the `📊 Open in FareHound` button on a Telegram alert → page opens, shows the deal.
- [ ] `/status` from the bot still works (this is unrelated to the tunnel — sanity check).
- [ ] After the next deal alert, the message is the thin format (2-line ping, not the v0.9.0 rich body).
- [ ] HA logs show no `ERROR` lines on FareHound startup.

If the page loads but the data looks empty, check:
- The Telegram WebApp `initData` is being passed correctly (open browser devtools, look for the `x-telegram-init-data` header on `/api/...` calls).
- The bot's `TELEGRAM_BOT_TOKEN` env var is exported (HMAC validation needs it).

---

## Costs

Cloudflare Tunnel is **free** for personal use up to fairly generous limits (50 tunnels, no bandwidth cap on the free plan as of 2026). Daily traffic for FareHound is well under any limit.

Domain registration is the only ongoing cost (~€10/year for a `.com`). Existing domains work too — just pick a subdomain.

---

## Troubleshooting

**`502 Bad Gateway` from the tunnel URL**
The FareHound add-on isn't listening. Check `sudo docker exec hassio_cli ha apps info 30bba4a3_farehound | grep state` — should be `running`. Check the add-on logs for `Mini Web App listening on port 8081`.

**`401 initData invalid` on every request**
Either `TELEGRAM_BOT_TOKEN` isn't set in the add-on, OR the page is being opened outside Telegram. The 401 is expected when you visit the URL in a regular browser — Telegram is the auth mechanism.

For local testing without Telegram, set `FAREHOUND_WEB_DEV_BYPASS_AUTH=1` in the add-on config (or just on the Pi shell). **Do not leave this on in production** — it disables HMAC validation entirely.

**Tunnel runs but DNS doesn't resolve**
DNS propagation can take up to 60 seconds the first time. After that it's instant. If still broken, check Cloudflare's dashboard → DNS for the CNAME record.

**The bot still sends rich Telegram messages**
Check that `MINIAPP_URL` is exported in the running container: `sudo docker exec $(sudo docker ps | grep farehound | awk '{print $1}') env | grep MINIAPP_URL`. If empty, the HA option didn't propagate — restart the add-on.
