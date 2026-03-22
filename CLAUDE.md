# FareHound — Project Instructions

## Deployment to Home Assistant

FareHound runs as an HA add-on on Barry's Raspberry Pi 4.

### Connection Details
- **SSH:** `ssh barry@homeassistant.local` (port 22, user `barry`)
- **Docker:** requires `sudo` (e.g., `sudo docker ps`)
- **HA CLI:** run via `sudo docker exec hassio_cli ha <command>`
- **Add-on slug:** `30bba4a3_farehound`
- **Docker image:** `30bba4a3/aarch64-addon-farehound:<version>`
- **Architecture:** aarch64 (Raspberry Pi 4)

### Deployment Steps

After a release is committed and signed off:

1. **Bump version** in `farehound/config.yaml` (e.g., `2.1.0` → `2.1.1`)

2. **Sync `farehound/src/`** — copy root `src/` into `farehound/src/` so the HA Supervisor
   build context has the latest code. The Supervisor builds from the `farehound/` subdirectory,
   so `src/` must exist there.
   ```
   rm -rf farehound/src && cp -r src/ farehound/src/
   ```

3. **Commit and push** to `main`

4. **SSH into HA and deploy:**
   ```bash
   # Stop the add-on
   sudo docker exec hassio_cli ha apps stop 30bba4a3_farehound

   # Reload the store so the Supervisor fetches the new version from GitHub
   sudo docker exec hassio_cli ha store reload

   # Verify it sees the update
   sudo docker exec hassio_cli ha apps info 30bba4a3_farehound | grep -E 'version|update'

   # Update via Supervisor (builds from farehound/ context)
   sudo docker exec hassio_cli ha apps update 30bba4a3_farehound

   # Start the add-on
   sudo docker exec hassio_cli ha apps start 30bba4a3_farehound

   # Wait ~15 seconds, then check logs
   sleep 15
   sudo docker exec hassio_cli ha apps logs 30bba4a3_farehound | tail -40
   ```

5. **Verify in logs:**
   - `Database schema initialized` — DB is up
   - `Scheduled polling every X hours` — poller running
   - `Scheduled daily digest at HH:MM` — digest scheduled
   - `TripBot polling started` — Telegram bot active
   - `Starting poll cycle` — first poll kicked off
   - No `ERROR` lines (except Telegram channel auth — pre-existing)

### Important Notes

- `farehound/src/` is a copy of root `src/` needed for the HA Supervisor Docker build context.
  Always sync it before pushing a release. The Supervisor builds from `farehound/` as context.
- Config options are set in the HA UI: Settings → Add-ons → FareHound → Configuration.
- Persistent data lives at `/data/` inside the container (mapped by HA).
- The `Dockerfile.ha` at repo root is a fallback for manual builds — the normal path uses
  `farehound/Dockerfile` via the Supervisor.
