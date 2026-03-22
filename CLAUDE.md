# FareHound — Project Instructions

## Deployment to Home Assistant

FareHound runs as an HA add-on on Barry's Raspberry Pi 4. After committing and pushing a release:

### Connection Details
- **SSH:** `ssh barry@homeassistant.local` (port 22, user `barry`)
- **Docker:** requires `sudo` (e.g., `sudo docker ps`)
- **HA CLI:** run via `sudo docker exec hassio_cli ha <command>`
- **Add-on slug:** `30bba4a3_farehound`
- **Docker image:** `30bba4a3/aarch64-addon-farehound:<version>`
- **Architecture:** aarch64

### Deployment Steps

1. **Bump version** in `farehound/config.yaml` before committing (e.g., `2.1.0` → `2.1.1`)

2. **Commit and push** to `main`

3. **SSH into HA and deploy:**
```bash
# Stop the running add-on
sudo docker exec hassio_cli ha apps stop 30bba4a3_farehound

# Clean up old build artifacts
sudo rm -rf /tmp/farehound-build

# Clone fresh from GitHub
sudo git clone --depth 1 https://github.com/barrydeleeuw/farehound.git /tmp/farehound-build

# Build the Docker image from repo root
# (HA Supervisor can't build from farehound/ subdir because src/ is at root)
cd /tmp/farehound-build
sudo docker build -f Dockerfile.ha -t 30bba4a3/aarch64-addon-farehound:<VERSION> .

# Start the add-on
sudo docker exec hassio_cli ha apps start 30bba4a3_farehound

# Wait ~15 seconds, then check logs
sudo docker exec hassio_cli ha apps logs 30bba4a3_farehound | tail -40
```

4. **Verify in logs:**
   - `Database schema initialized` — DB is up
   - `Scheduled polling every X hours` — poller running
   - `Scheduled daily digest at HH:MM` — digest scheduled
   - `TripBot polling started` — Telegram bot active
   - `Starting poll cycle` — first poll kicked off
   - No `ERROR` lines (except Telegram channel auth — that's pre-existing)

### Important Notes

- The `Dockerfile.ha` lives at the repo root and builds from root context (so it can access `src/`). It references `farehound/app-config.yaml` and `farehound/rootfs/` from the repo root.
- The HA Supervisor's built-in `ha apps update` doesn't work because the Supervisor uses `farehound/` as the Docker build context, which doesn't contain `src/`. We build manually instead.
- The add-on's persistent data is at `/data/` inside the container (mapped by HA).
- Config options are set in the HA UI under Settings → Add-ons → FareHound → Configuration.

### Dockerfile.ha

If `Dockerfile.ha` doesn't exist in the repo, create it at root:
```dockerfile
FROM debian:bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip jq && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --break-system-packages .

COPY farehound/app-config.yaml ./config.yaml
COPY config/ ./config/

COPY farehound/rootfs /
```
