/* ============================================================
   FareHound Mini Web App — minimal client glue
   - Telegram WebApp SDK init (theme + BackButton + MainButton)
   - Cost-breakdown row expansion
   - Sparkline rendering from inline data
   - Snooze chip confirm/undo
   - Outside-Telegram fallback so each page works in plain HTTP
   ============================================================ */

(function () {
  "use strict";

  const TG = window.Telegram && window.Telegram.WebApp;
  const inTelegram = !!(TG && TG.initData);
  if (inTelegram) document.body.classList.add("in-telegram");

  // ---------- Telegram bootstrap ----------

  function bootTelegram() {
    if (!TG) return;
    try {
      TG.ready();
      TG.expand();
      // BackButton wires to history.back() on every page that wants one
      const wantsBack = document.body.dataset.back === "true";
      if (wantsBack) {
        TG.BackButton.show();
        TG.BackButton.onClick(() => history.back());
      } else {
        TG.BackButton.hide();
      }
      // Honor enableClosingConfirmation per page
      if (document.body.dataset.confirmClose === "true") {
        TG.enableClosingConfirmation();
      }
    } catch (e) {
      console.warn("Telegram WebApp init failed", e);
    }
  }

  function setMainButton(text, onClick) {
    if (!TG) return;
    TG.MainButton.setText(text.toUpperCase());
    TG.MainButton.show();
    // Replace any prior handler
    TG.MainButton.offClick && TG.MainButton.offClick();
    TG.MainButton.onClick(onClick);
  }

  function alertOrToast(msg) {
    if (TG && TG.showAlert) TG.showAlert(msg);
    else console.log("[toast]", msg);
  }

  function hapticImpact(style) {
    if (TG && TG.HapticFeedback) TG.HapticFeedback.impactOccurred(style || "light");
  }

  // ---------- Cost breakdown row expansion ----------

  function wireCostBreakdown() {
    document.querySelectorAll(".ledger tr.expandable").forEach((row) => {
      const next = row.nextElementSibling;
      if (!next || !next.classList.contains("expansion")) return;
      next.style.display = "none";
      row.addEventListener("click", () => {
        const open = next.style.display !== "none";
        next.style.display = open ? "none" : "table-row";
        hapticImpact("light");
      });
    });
  }

  // ---------- Sparkline ----------
  /*
   Reads data from a script tag with id="price-history" containing a JSON array
   of [iso_date, price] pairs. Also reads typical_low/high from the SVG's data-*
   attributes. Renders a path + a translucent band + a current-point dot.
  */

  function renderSparkline() {
    const svg = document.querySelector(".sparkline");
    if (!svg) return;
    const dataNode = document.getElementById("price-history");
    if (!dataNode) return;

    let series;
    try {
      series = JSON.parse(dataNode.textContent);
    } catch (e) {
      console.warn("invalid price-history json", e);
      return;
    }
    if (!Array.isArray(series) || series.length < 2) return;

    const w = svg.clientWidth || 320;
    const h = 64;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);

    const lo = +svg.dataset.typicalLow;
    const hi = +svg.dataset.typicalHigh;

    const prices = series.map((d) => +d[1]);
    const yMin = Math.min(...prices, isFinite(lo) ? lo : Infinity) * 0.985;
    const yMax = Math.max(...prices, isFinite(hi) ? hi : -Infinity) * 1.015;

    const xAt = (i) => (i / (series.length - 1)) * (w - 4) + 2;
    const yAt = (p) => {
      const t = (p - yMin) / (yMax - yMin);
      return h - 4 - t * (h - 8);
    };

    // Typical-range band
    if (isFinite(lo) && isFinite(hi)) {
      const yLo = yAt(hi);   // visually the band-top is the higher price
      const yHi = yAt(lo);
      const band = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      band.setAttribute("class", "band");
      band.setAttribute("x", "0");
      band.setAttribute("y", String(yLo));
      band.setAttribute("width", String(w));
      band.setAttribute("height", String(yHi - yLo));
      svg.appendChild(band);

      // Edges
      [yLo, yHi].forEach((y) => {
        const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
        line.setAttribute("class", "band-edge");
        line.setAttribute("x1", "0");
        line.setAttribute("x2", String(w));
        line.setAttribute("y1", String(y));
        line.setAttribute("y2", String(y));
        svg.appendChild(line);
      });
    }

    // Polyline
    const points = series.map((d, i) => `${xAt(i).toFixed(1)},${yAt(+d[1]).toFixed(1)}`).join(" ");
    const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.setAttribute("class", "line");
    line.setAttribute("points", points);
    svg.appendChild(line);

    // Current point — last entry
    const last = series[series.length - 1];
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("class", "current-dot");
    dot.setAttribute("cx", String(xAt(series.length - 1)));
    dot.setAttribute("cy", String(yAt(+last[1])));
    dot.setAttribute("r", "3.5");
    svg.appendChild(dot);
  }

  // ---------- Authenticated fetch helper ----------

  async function api(method, path, body) {
    const headers = { "content-type": "application/json" };
    if (TG && TG.initData) headers["x-telegram-init-data"] = TG.initData;
    const init = { method, headers };
    if (body !== undefined) init.body = JSON.stringify(body);
    const resp = await fetch(path, init);
    if (!resp.ok) {
      let msg = `HTTP ${resp.status}`;
      try { const j = await resp.json(); if (j.detail) msg = j.detail; } catch (_) {}
      throw new Error(msg);
    }
    if (resp.status === 204) return null;
    return resp.json().catch(() => null);
  }

  function confirmAsync(text) {
    return new Promise((resolve) => {
      if (TG && TG.showConfirm) TG.showConfirm(text, (ok) => resolve(!!ok));
      else resolve(window.confirm(text));
    });
  }

  // ---------- Route actions: snooze / unsnooze / remove ----------

  function wireRouteActions() {
    document.querySelectorAll("[data-snooze]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const days = parseInt(btn.dataset.snooze, 10);
        const card = btn.closest(".route-card");
        const routeId = card?.dataset?.routeId;
        if (!card || !routeId) return;
        try {
          await api("POST", `/api/routes/${routeId}/snooze`, { days });
          card.classList.add("is-snoozed");
          alertOrToast(`Snoozed ${days}d.`);
          hapticImpact("medium");
        } catch (err) {
          alertOrToast(`Snooze failed: ${err.message}`);
        }
      });
    });

    document.querySelectorAll("[data-unsnooze]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const card = btn.closest(".route-card");
        const routeId = card?.dataset?.routeId;
        if (!card || !routeId) return;
        try {
          await api("POST", `/api/routes/${routeId}/unsnooze`);
          card.classList.remove("is-snoozed");
          alertOrToast("Resumed.");
          hapticImpact("medium");
        } catch (err) {
          alertOrToast(`Resume failed: ${err.message}`);
        }
      });
    });

    document.querySelectorAll("[data-remove]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        // Diagnostics — surface every short-circuit as a toast so we can see
        // where clicks die. Will be removed once the action is verified working.
        const card = btn.closest(".route-card");
        if (!card) {
          alertOrToast("DEBUG: no .route-card ancestor — DOM structure issue");
          return;
        }
        const routeId = card.dataset.routeId;
        if (!routeId) {
          alertOrToast(`DEBUG: card has no data-route-id (data attrs: ${JSON.stringify(card.dataset)})`);
          return;
        }
        const name = card.querySelector(".name")?.textContent?.trim() || "this route";
        let ok;
        try {
          ok = await confirmAsync(`Remove ${name}?\nFareHound will stop monitoring this trip.`);
        } catch (e) {
          alertOrToast(`DEBUG: confirm threw: ${e.message}`);
          return;
        }
        alertOrToast(`DEBUG: confirm returned ${ok} for ${routeId}`);
        if (!ok) return;
        try {
          const resp = await api("DELETE", `/api/routes/${routeId}`);
          alertOrToast(`Removed (server: ${JSON.stringify(resp)})`);
          card.style.transition = "opacity 200ms ease, height 200ms ease";
          card.style.opacity = "0";
          setTimeout(() => card.remove(), 220);
          hapticImpact("medium");
        } catch (err) {
          alertOrToast(`Remove failed: ${err.message}`);
        }
      });
    });
  }

  // ---------- Deal page actions ----------

  function wireDealActions() {
    const bookUrl = document.body.dataset.bookUrl;
    const dealId = document.body.dataset.dealId;
    if (bookUrl) {
      // Telegram MainButton primary action
      setMainButton("Book now", () => {
        if (TG && TG.openLink) TG.openLink(bookUrl);
        else window.open(bookUrl, "_blank");
      });
    }

    document.querySelectorAll("[data-action]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const a = btn.dataset.action;
        if (!dealId) return;
        if (a === "watch") {
          try {
            await api("POST", `/api/deals/${dealId}/feedback`, { feedback: "watching" });
            alertOrToast("Marked as watching. We'll keep monitoring quietly.");
            hapticImpact("light");
          } catch (err) {
            alertOrToast(`Failed: ${err.message}`);
          }
        } else if (a === "skip") {
          const ok = await confirmAsync("Skip this route for 7 days?");
          if (!ok) return;
          // Skip route = mark deal dismissed + snooze the route 7d.
          // Pull route_id from the body data attr (set by the deal template).
          const routeId = document.body.dataset.routeId;
          try {
            await api("POST", `/api/deals/${dealId}/feedback`, { feedback: "dismissed" });
            if (routeId) {
              await api("POST", `/api/routes/${routeId}/snooze`, { days: 7 });
            }
            alertOrToast("Skipped for 7 days.");
            hapticImpact("medium");
          } catch (err) {
            alertOrToast(`Failed: ${err.message}`);
          }
        }
      });
    });
  }

  // ---------- Init ----------

  function init() {
    bootTelegram();
    wireCostBreakdown();
    renderSparkline();
    wireRouteActions();
    wireDealActions();

    // Wide diagnostic: log every click on the page so we can verify clicks
    // register at all in the Telegram WebApp. Removes after the bug is fixed.
    document.addEventListener("click", (e) => {
      const target = e.target;
      const tag = target.tagName;
      const cls = target.className || "";
      const dataAttrs = Object.keys(target.dataset || {}).join(",") || "(none)";
      alertOrToast(`CLICK: ${tag}.${cls} data=[${dataAttrs}]`);
    }, { capture: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
