"""
FareHound Route Manager — lightweight web UI for managing flight routes.

Usage:
    python3 scripts/routes.py

Opens a browser to http://localhost:8080 where you can add/edit/remove routes.
Click "Save & Push" to send routes to your Home Assistant instance.

Requires: pip install fastapi uvicorn
"""
from __future__ import annotations

import json
import subprocess
import webbrowser
from pathlib import Path
from threading import Timer

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

ROUTES_FILE = Path(__file__).parent.parent / "data" / "routes.json"
HA_HOST = "barry@homeassistant.local"

# Ensure data dir exists
ROUTES_FILE.parent.mkdir(exist_ok=True)


def _load_routes() -> list[dict]:
    if ROUTES_FILE.exists():
        return json.loads(ROUTES_FILE.read_text())
    # Seed from config.yaml defaults
    return [
        {
            "id": "ams-nrt-oct",
            "origin": "AMS",
            "destination": "NRT",
            "trip_type": "round_trip",
            "earliest_departure": "2026-10-01",
            "latest_return": "2026-10-31",
            "date_flexibility_days": 3,
            "max_stops": 1,
            "passengers": 2,
            "notes": "Japan trip — autumn colours",
        },
        {
            "id": "ams-ist-flex",
            "origin": "AMS",
            "destination": "IST",
            "trip_type": "round_trip",
            "earliest_departure": "2026-06-01",
            "latest_return": "2026-09-30",
            "date_flexibility_days": 7,
            "max_stops": 0,
            "passengers": 2,
            "notes": "Istanbul — flexible timing",
        },
    ]


def _save_routes(routes: list[dict]) -> None:
    ROUTES_FILE.write_text(json.dumps(routes, indent=2))


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FareHound — Route Manager</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #f5f5f5; color: #333; padding: 2rem; max-width: 900px; margin: 0 auto; }
  h1 { font-size: 1.5rem; margin-bottom: 1.5rem; }
  .route-card { background: white; border-radius: 8px; padding: 1.25rem;
                margin-bottom: 1rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
  .route-header { display: flex; justify-content: space-between; align-items: center;
                  margin-bottom: 1rem; }
  .route-title { font-size: 1.1rem; font-weight: 600; }
  .route-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
  .route-grid.three { grid-template-columns: 1fr 1fr 1fr; }
  label { display: block; font-size: 0.8rem; color: #666; margin-bottom: 0.25rem; }
  input, select { width: 100%; padding: 0.5rem; border: 1px solid #ddd; border-radius: 4px;
                  font-size: 0.9rem; }
  input:focus, select:focus { outline: none; border-color: #2196F3; }
  .field { margin-bottom: 0.5rem; }
  .field.full { grid-column: 1 / -1; }
  .btn { padding: 0.6rem 1.2rem; border: none; border-radius: 6px; font-size: 0.9rem;
         cursor: pointer; font-weight: 500; }
  .btn-primary { background: #2196F3; color: white; }
  .btn-primary:hover { background: #1976D2; }
  .btn-danger { background: none; color: #e53935; font-size: 0.85rem; }
  .btn-danger:hover { text-decoration: underline; }
  .btn-add { background: white; color: #2196F3; border: 2px dashed #2196F3;
             width: 100%; padding: 1rem; border-radius: 8px; font-size: 0.95rem;
             cursor: pointer; margin-bottom: 1rem; }
  .btn-add:hover { background: #E3F2FD; }
  .actions { display: flex; gap: 1rem; justify-content: flex-end; margin-top: 1rem; }
  .status { padding: 0.75rem 1rem; border-radius: 6px; margin-top: 1rem; display: none; }
  .status.success { display: block; background: #E8F5E9; color: #2E7D32; }
  .status.error { display: block; background: #FFEBEE; color: #C62828; }
</style>
</head>
<body>
<h1>FareHound — Route Manager</h1>
<div id="routes"></div>
<button class="btn-add" onclick="addRoute()">+ Add Route</button>
<div class="actions">
  <button class="btn btn-primary" onclick="saveAndPush()">Save &amp; Push to Home Assistant</button>
</div>
<div id="status" class="status"></div>

<script>
let routes = [];

async function loadRoutes() {
  const res = await fetch('/api/routes');
  routes = await res.json();
  render();
}

function render() {
  const container = document.getElementById('routes');
  container.innerHTML = routes.map((r, i) => `
    <div class="route-card">
      <div class="route-header">
        <span class="route-title">${r.origin || '???'} &rarr; ${r.destination || '???'}</span>
        <button class="btn btn-danger" onclick="removeRoute(${i})">Remove</button>
      </div>
      <div class="route-grid three">
        <div class="field">
          <label>Origin (IATA)</label>
          <input value="${r.origin || ''}" onchange="upd(${i},'origin',this.value)"
                 placeholder="AMS" maxlength="3" style="text-transform:uppercase">
        </div>
        <div class="field">
          <label>Destination (IATA)</label>
          <input value="${r.destination || ''}" onchange="upd(${i},'destination',this.value)"
                 placeholder="NRT" maxlength="3" style="text-transform:uppercase">
        </div>
        <div class="field">
          <label>Trip Type</label>
          <select onchange="upd(${i},'trip_type',this.value)">
            <option value="round_trip" ${r.trip_type==='round_trip'?'selected':''}>Round Trip</option>
            <option value="one_way" ${r.trip_type==='one_way'?'selected':''}>One Way</option>
          </select>
        </div>
      </div>
      <div class="route-grid">
        <div class="field">
          <label>Earliest Departure</label>
          <input type="date" value="${r.earliest_departure || ''}"
                 onchange="upd(${i},'earliest_departure',this.value)">
        </div>
        <div class="field">
          <label>Latest Return</label>
          <input type="date" value="${r.latest_return || ''}"
                 onchange="upd(${i},'latest_return',this.value)">
        </div>
      </div>
      <div class="route-grid three">
        <div class="field">
          <label>Date Flexibility (days)</label>
          <input type="number" value="${r.date_flexibility_days ?? 3}" min="0" max="14"
                 onchange="upd(${i},'date_flexibility_days',+this.value)">
        </div>
        <div class="field">
          <label>Max Stops</label>
          <input type="number" value="${r.max_stops ?? 1}" min="0" max="3"
                 onchange="upd(${i},'max_stops',+this.value)">
        </div>
        <div class="field">
          <label>Passengers</label>
          <input type="number" value="${r.passengers ?? 2}" min="1" max="9"
                 onchange="upd(${i},'passengers',+this.value)">
        </div>
      </div>
      <div class="field full" style="margin-top:0.5rem">
        <label>Notes</label>
        <input value="${r.notes || ''}" onchange="upd(${i},'notes',this.value)"
               placeholder="e.g. Japan trip — autumn colours">
      </div>
    </div>
  `).join('');
}

function upd(i, key, val) {
  routes[i][key] = typeof val === 'string' ? val.trim() : val;
  // Auto-generate ID from origin + destination
  if (key === 'origin' || key === 'destination') {
    const o = (routes[i].origin || '').toLowerCase();
    const d = (routes[i].destination || '').toLowerCase();
    if (o && d) routes[i].id = `${o}-${d}`;
  }
}

function addRoute() {
  routes.push({
    id: '', origin: '', destination: '', trip_type: 'round_trip',
    earliest_departure: '', latest_return: '',
    date_flexibility_days: 3, max_stops: 1, passengers: 2, notes: ''
  });
  render();
}

function removeRoute(i) {
  routes.splice(i, 1);
  render();
}

async function saveAndPush() {
  const status = document.getElementById('status');
  status.className = 'status';
  status.style.display = 'none';

  // Validate
  for (const r of routes) {
    if (!r.origin || !r.destination) {
      status.textContent = 'Error: All routes need an origin and destination.';
      status.className = 'status error';
      return;
    }
  }

  try {
    const res = await fetch('/api/routes', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(routes),
    });
    const data = await res.json();
    if (data.ok) {
      status.textContent = 'Routes saved and pushed to Home Assistant. FareHound will pick them up on next poll cycle.';
      status.className = 'status success';
    } else {
      status.textContent = 'Error: ' + (data.error || 'Unknown error');
      status.className = 'status error';
    }
  } catch (e) {
    status.textContent = 'Error: ' + e.message;
    status.className = 'status error';
  }
}

loadRoutes();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/routes")
async def get_routes():
    return JSONResponse(_load_routes())


@app.post("/api/routes")
async def save_routes(request: Request):
    routes = await request.json()

    # Validate
    for r in routes:
        if not r.get("origin") or not r.get("destination"):
            return JSONResponse({"ok": False, "error": "Missing origin or destination"}, status_code=400)
        r["origin"] = r["origin"].upper()
        r["destination"] = r["destination"].upper()
        if not r.get("id"):
            r["id"] = f"{r['origin'].lower()}-{r['destination'].lower()}"

    # Save locally
    _save_routes(routes)

    # Push to HA via SCP — the add-on mounts /data as persistent storage
    # We use the HA REST API to update the add-on config's routes field instead
    try:
        routes_json = json.dumps(routes)
        # Update via HA API using the long-lived token
        token_file = Path(__file__).parent.parent / ".ha_token"
        if not token_file.exists():
            return JSONResponse({
                "ok": False,
                "error": "No .ha_token file found. Create one with your HA long-lived access token."
            }, status_code=500)

        token = token_file.read_text().strip()

        # Get current add-on config, update routes field, push back
        import httpx
        base = "http://homeassistant.local:8123"

        # Use the HA services API to fire an event that FareHound can listen to
        # Actually, simplest: update the add-on options via Supervisor proxy
        r = httpx.post(
            f"{base}/api/services/hassio/addon_stdin",
            headers={"Authorization": f"Bearer {token}"},
            json={"addon": "30bba4a3_farehound", "input": routes_json},
            timeout=10,
        )

        if r.status_code != 200:
            # Fallback: just save locally, user can manually restart
            return JSONResponse({
                "ok": True,
                "warning": f"Saved locally but couldn't push to HA (HTTP {r.status_code}). Restart the add-on to pick up changes."
            })

        return JSONResponse({"ok": True})

    except Exception as e:
        # Still saved locally
        return JSONResponse({
            "ok": True,
            "warning": f"Saved locally but couldn't push to HA: {e}. Restart the add-on to pick up changes."
        })


def _open_browser():
    webbrowser.open("http://localhost:8080")


if __name__ == "__main__":
    print("Starting FareHound Route Manager...")
    print("Opening browser to http://localhost:8080")
    print("Press Ctrl+C to stop.\n")
    Timer(1.0, _open_browser).start()
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning")
