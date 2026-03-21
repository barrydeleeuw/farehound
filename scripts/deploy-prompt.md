# FareHound Deployment Prompt

> Give this prompt to the Claude Code agent in the `home-assistant` project.
> It has HA MCP tools + SSH access to the HA server.

---

## Prompt

You need to deploy the **FareHound** Home Assistant add-on. It's a flight fare monitoring service that runs as an HA add-on container. The source code is at `https://github.com/barrydeleeuw/farehound` (private repo).

### What FareHound does
- Polls Google Flights via SerpAPI on a schedule (every 4 hours)
- Monitors community deal feeds (RSS + Telegram channels)
- Scores deals with Claude AI
- Sends notifications to Barry's phone via HA + optional Telegram bot
- Publishes HA sensors: `sensor.farehound_{route_id}_price` (one per route)
- Routes configured: `ams-nrt-oct` (Amsterdam → Tokyo, Oct 2026) and `ams-ist-flex` (Amsterdam → Istanbul, Jun-Sep 2026)

### Step 1 — Install the add-on via SSH

SSH into the HA server and use the Supervisor API to install and configure the add-on.

```bash
# Add the GitHub repo as an add-on repository
curl -sSf -X POST \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"repository": "https://github.com/barrydeleeuw/farehound"}' \
  http://supervisor/store/repositories

# Wait for the store to refresh, then install
sleep 5
curl -sSf -X POST \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  http://supervisor/addons/local_farehound/install

# Configure the add-on options
# IMPORTANT: You need to ask Barry for the actual API key values before running this.
# Do NOT proceed without real keys.
curl -sSf -X POST \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "options": {
      "serpapi_api_key": "ASK_BARRY",
      "anthropic_api_key": "ASK_BARRY",
      "telegram_api_id": "ASK_BARRY_OR_LEAVE_EMPTY",
      "telegram_api_hash": "ASK_BARRY_OR_LEAVE_EMPTY",
      "ha_notify_service": "notify.mobile_app_barry_phone",
      "poll_interval_hours": 4,
      "alert_threshold": 0.75,
      "traveller_name": "Barry",
      "home_airport": "AMS",
      "telegram_bot_token": "",
      "telegram_chat_id": ""
    }
  }' \
  http://supervisor/addons/local_farehound/options

# Start the add-on
curl -sSf -X POST \
  -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
  http://supervisor/addons/local_farehound/start
```

**Note**: The Supervisor API is only accessible from within the HA host. If SSH gives you a shell inside the HA OS, `$SUPERVISOR_TOKEN` is already available. If not, you can find it via `cat /run/os-release` or use the HA CLI instead:

```bash
ha addons repository add https://github.com/barrydeleeuw/farehound
ha addons install local_farehound
ha addons start local_farehound
```

The `ha` CLI may not support setting options directly. In that case, use the curl approach above or edit `/data/addons/local_farehound/options.json` directly.

### Step 2 — Verify the add-on is running

Check the add-on logs via SSH:
```bash
ha addons logs local_farehound | head -20
```

You should see output like:
```
Starting FareHound...
Database initialized at /data/flights.duckdb
Routes synced: ams-nrt-oct, ams-ist-flex
RSS listener started, polling 7 feeds every 300s
Starting poll cycle...
```

If there are errors, check:
- API key issues → reconfigure options
- Network errors → check DNS resolution from the container
- Import errors → the Docker build may have failed, check `ha addons rebuild local_farehound`

### Step 3 — Set up the Lovelace dashboard (via MCP)

After the first poll cycle completes (~1-2 minutes after start), FareHound will create HA sensors. Use the MCP tools to add a dashboard card.

First, verify sensors exist:
- Use `ha_search_entities` to search for `farehound`
- You should find: `sensor.farehound_ams_nrt_oct_price` and `sensor.farehound_ams_ist_flex_price`

Then use `ha_config_set_dashboard` to add a FareHound card to Barry's main dashboard. Use this markdown card config:

```yaml
type: markdown
title: "FareHound — Flight Prices"
content: >
  {% set routes = states.sensor | selectattr('entity_id', 'match', 'sensor.farehound_.*_price') | list %}
  {% for s in routes %}
  **{{ s.attributes.route_name | default(s.name) }}**
  {{ s.state }} {{ s.attributes.currency | default('EUR') }}
  {{ s.attributes.trend | default('') }}
  Last checked: {{ s.attributes.last_checked | default('never') }}
  {% if s.attributes.deal_score is defined and s.attributes.deal_score %}Score: {{ s.attributes.deal_score }}{% endif %}

  {% endfor %}
  {% if routes | length == 0 %}
  _Waiting for first poll cycle..._
  {% endif %}
```

Also add a history graph card:
```yaml
type: history-graph
title: "FareHound — 7-Day Price History"
hours_to_show: 168
entities:
  - entity: sensor.farehound_ams_nrt_oct_price
    name: AMS to NRT
  - entity: sensor.farehound_ams_ist_flex_price
    name: AMS to IST
```

### Step 4 — Create feedback automations (via MCP)

FareHound sends notifications with actionable buttons. Create automations to handle the feedback:

**Automation 1: "Book Now" feedback**
```yaml
alias: "FareHound — Deal Booked"
trigger:
  - platform: event
    event_type: mobile_app_notification_action
    event_data:
      action: FAREHOUND_BOOK
action:
  - service: rest_command.farehound_feedback
    data:
      deal_id: "{{ trigger.event.data.action_data.deal_id }}"
      feedback: "booked"
```

**Automation 2: "Not Interested" feedback**
```yaml
alias: "FareHound — Deal Dismissed"
trigger:
  - platform: event
    event_type: mobile_app_notification_action
    event_data:
      action: FAREHOUND_DISMISS
action:
  - service: rest_command.farehound_feedback
    data:
      deal_id: "{{ trigger.event.data.action_data.deal_id }}"
      feedback: "dismissed"
```

Use `ha_config_set_automation` to create both.

### Step 5 — Validation checklist

Run through these checks and report results:

1. **Add-on running**: `ha addons info local_farehound` — state should be "started"
2. **No errors in logs**: `ha addons logs local_farehound | tail -30` — no tracebacks
3. **Sensors exist**: Use `ha_get_state` for `sensor.farehound_ams_nrt_oct_price` — should have a numeric state
4. **Dashboard card**: Use `ha_config_get_dashboard` to verify the FareHound cards are present
5. **Automations**: Use `ha_config_get_automation` to verify both feedback automations exist
6. **Test notification**: Check logs for "Starting poll cycle" followed by snapshot results

Report back with the status of each check. If any fail, diagnose and fix before moving on.

### Important notes
- **Ask Barry for API keys** before configuring. Never hardcode them.
- The add-on slug might be `local_farehound` or just `farehound` — check `ha addons list` if the install fails.
- Sensors won't exist until after the first successful poll cycle.
- If MCP tools can't modify the dashboard, fall back to SSH and edit the dashboard JSON directly.
