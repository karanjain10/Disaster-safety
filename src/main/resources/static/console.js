/* Safe Route Console — talks to the safety engine (directly, or through the Spring gateway at /engine). */
(function () {
  "use strict";
  const API = (typeof window.ENGINE_BASE === "string") ? window.ENGINE_BASE : "/engine";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function api(path, opts = {}) {
    const res = await fetch(API + path, {
      headers: { "Content-Type": opts.raw ? "application/xml" : "application/json" },
      credentials: "same-origin", ...opts,
      body: opts.raw ? opts.raw : (opts.body ? JSON.stringify(opts.body) : undefined),
    });
    if (!res.ok) {
      let msg = res.status + " " + res.statusText;
      try { const j = await res.json(); msg = j.detail || msg; } catch (e) { /* ignore */ }
      throw new Error(msg);
    }
    return res.json();
  }

  // ------------------------------------------------------------------ map
  const map = L.map("map", { zoomControl: true, preferCanvas: true, zoomSnap: 0.25 });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  const routeLayer = L.layerGroup().addTo(map);
  const markLayer = L.layerGroup().addTo(map);
  const tripLayer = L.layerGroup().addTo(map);
  map.createPane("heat");
  map.getPane("heat").style.zIndex = 350;          // above tiles, below routes and markers
  map.getPane("heat").style.pointerEvents = "none";
  let heatLayer = null;

  const st = { o: null, d: null, r: null, askLocation: null, mode: "route", status: null, plan: null, trip: null, since: 0, closurePts: null, heatSeq: 0, heatT: null, tripUrg: null };

  const fmt = (t) => new Date(t * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
  const mins = (s) => s == null ? null : Math.max(0, Math.round(s / 60));

  // ------------------------------------------------------------------ tabs
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + b.dataset.tab));
    st.mode = b.dataset.tab;
    if (st.mode === "admin") loadPending();
    if (st.mode === "sms") smsRefresh();
  }));

  // ------------------------------------------------------------------ status
  async function loadStatus() {
    const s = await api("/status");
    st.status = s;
    $("modeNote").textContent = s.synthetic
      ? "Simulated storm on synthetic terrain (demo). Hazards are forecasts, not real conditions."
      : "Live mode: forecasts from current weather data.";
    const lr = s.last_revalidation;
    $("foot").textContent = `Hazard data v${s.hazard_version}, ${mins(s.hazard_age_s)} min old · ${s.slices} hazard areas · ` +
      `${s.active_trips} active trips` + (lr ? ` · last update re-checked ${lr.re_evaluated} of ${lr.active_trips} trips` : "") +
      ` · routing: ${s.engine} · assistant: ${s.assistant}`;
    $("clockBox").hidden = !s.synthetic;
    if (s.synthetic && s.scenario) $("clockTxt").textContent = `${fmt(s.now)} IST · storm minute ${s.scenario.elapsed_min}`;
    $("departOut").textContent = offsetLabel();
    return s;
  }
  async function refreshAll() { await loadStatus(); if (st.trip) await pollTrip(); }

  // ------------------------------------------------------------------ picking points
  map.on("click", (e) => {
    const ll = [+e.latlng.lng.toFixed(6), +e.latlng.lat.toFixed(6)];
    if (st.mode === "report") { st.r = ll; $("rTxt").textContent = `${ll[1].toFixed(4)}, ${ll[0].toFixed(4)}`; $("reportBtn").disabled = false; drawMarks(); return; }
    if (st.mode === "ask") {
      st.askLocation = ll;
      $("askLocationText").textContent = `Selected map location: ${ll[1].toFixed(4)}, ${ll[0].toFixed(4)}`;
      drawMarks();
      return;
    }
    if (st.mode === "admin" && st.closurePts) {
      st.closurePts.push(ll);
      if (st.closurePts.length === 2) $("closureSave").disabled = false;
      drawMarks(); return;
    }
    if (!st.o || (st.o && st.d)) { st.o = ll; st.d = null; } else st.d = ll;
    $("oTxt").textContent = st.o ? `Start ${st.o[1].toFixed(4)}, ${st.o[0].toFixed(4)}` : "Start not set";
    $("dTxt").textContent = st.d ? `Destination ${st.d[1].toFixed(4)}, ${st.d[0].toFixed(4)}` : "Destination not set";
    $("planBtn").disabled = !(st.o && st.d);
    drawMarks();
  });
  function drawMarks() {
    markLayer.clearLayers();
    const pin = (ll, c) => L.circleMarker([ll[1], ll[0]], { radius: 8, color: "#fff", weight: 2, fillColor: c, fillOpacity: 1 }).addTo(markLayer);
    if (st.o) pin(st.o, "#1b2a33");
    if (st.d) pin(st.d, "#237a4b");
    if (st.r) pin(st.r, "#b77f00");
    if (st.askLocation) pin(st.askLocation, "#b77f00");
    (st.closurePts || []).forEach((p) => pin(p, "#b8322a"));
  }
  $("clearBtn").addEventListener("click", () => {
    st.o = st.d = null; st.plan = null; routeLayer.clearLayers(); $("planOut").innerHTML = ""; $("planBtn").disabled = true;
    $("oTxt").textContent = "Start not set"; $("dTxt").textContent = "Destination not set"; drawMarks();
  });

  // ------------------------------------------------------------------ planning
  $("planBtn").addEventListener("click", async () => {
    $("planBtn").disabled = true; $("planOut").innerHTML = `<p class="hint">Checking routes against the forecast…</p>`;
    try {
      const p = await api("/plan", { method: "POST", body: { origin: st.o, destination: st.d, vehicle: $("vehicle").value, depart_in_min: +$("depart").value } });
      st.plan = p; renderPlan(p);
    } catch (e) { $("planOut").innerHTML = `<p class="hint">Could not plan the route: ${esc(e.message)}</p>`; }
    finally { $("planBtn").disabled = false; }
  });

  function renderPlan(p) {
    routeLayer.clearLayers();
    const chosen = p.routes.find((r) => r.chosen);
    p.routes.slice().sort((a, b) => (a.chosen ? 1 : 0) - (b.chosen ? 1 : 0)).forEach((r) => {
      const latlngs = r.geometry.coordinates.map((c) => [c[1], c[0]]);
      const style = r.chosen ? { color: "#237a4b", weight: 7, opacity: 0.95 }
        : r.feasible ? { color: "#2f6fb0", weight: 4, opacity: 0.7 }
        : { color: "#b8322a", weight: 4, opacity: 0.75, dashArray: "8 7" };
      L.polyline(latlngs, style).addTo(routeLayer).bindTooltip(routeLabel(r));
      r.blocked.forEach((b) => L.circleMarker([b.at[1], b.at[0]], { radius: 6, color: "#b8322a", weight: 2, fillColor: "#fff", fillOpacity: 1 })
        .addTo(routeLayer).bindTooltip(`${b.class === "flood" ? "Water" : b.class} here at ${fmt(b.arrival)} (${Math.round(b.p * 100)}% likely)`));
    });
    if (p.routes.length) map.fitBounds(routeLayer.getLayers().filter((l) => l.getBounds).map((l) => l.getBounds()).reduce((a, b) => a.extend(b)), { padding: [30, 30] });

    const veh = p.vehicle.label.toLowerCase();
    let html = "";
    if (chosen) {
      const m = mins(chosen.margin_s);
      html += `<div class="verdict"><h3>Safe route for a ${esc(veh)}</h3>
        <div class="margin">${m == null ? "No hazard forecast" : m + " min to spare"}</div>
        <p>${m == null ? "Nothing is predicted on this route in the next two hours." : "before the first predicted hazard on this route reaches you."}</p>
        <p>${(chosen.distance_m / 1000).toFixed(1)} km · about ${Math.round(chosen.duration_s / 60)} min · ${Math.round(chosen.p_safe * 100)}% safety probability${chosen.source === "detour" ? " · detour around blocked roads" : ""}</p></div>
        <div id="leaveBy" aria-live="polite"></div>
        <div class="row"><button class="primary" id="startTrip">Start trip and watch for changes</button></div>`;
    } else {
      html += `<div class="verdict none"><h3>No safe route for a ${esc(veh)} right now</h3>
        <p>Every option crosses water, a landslide area or strong wind when you would get there. Wait somewhere on high ground, try a larger vehicle profile, or ask for the nearest shelter.</p></div>`;
    }
    if (p.baseline) {
      const b = p.baseline;
      if (b.route_id && b.m3_feasible === false) html += `<p class="baseline">A map that only checks today's flooded areas would send you on route ${esc(b.route_id)}. <strong>It becomes unsafe before you get there.</strong></p>`;
      else if (!b.route_id && chosen) html += `<p class="baseline">A map that only checks today's flooded areas would find no route at all; this one sees which areas will have cleared or are passable for your vehicle.</p>`;
    }
    html += `<ul class="routes">` + p.routes.map((r) => `<li data-id="${r.id}"><span class="sw2" style="background:${r.chosen ? "#237a4b" : r.feasible ? "#2f6fb0" : "#b8322a"}"></span>
      <span>${r.chosen ? "<strong>Recommended</strong>" : r.feasible ? "Also safe" : "Unsafe"} · ${Math.round(r.duration_s / 60)} min, ${(r.distance_m / 1000).toFixed(1)} km${r.source === "detour" ? " (detour)" : ""}
      <span class="why">${r.feasible ? (r.margin_s == null ? "no hazard forecast" : mins(r.margin_s) + " min margin") : esc(r.reason)}</span></span>
      <span class="code" title="Short code to share this route by SMS">${esc(r.route_code)}</span></li>`).join("") + `</ul>`;
    if (p.stale) html += `<p class="hint">Hazard data is ${mins(p.hazard_data_age_s)} min old, so the safety check is stricter than usual.</p>`;
    if (p.decision_record) {
      const dr = p.decision_record;
      const cf = (dr.counterfactual_departures || []).map((x) =>
        `${x.depart_offset_min ? "+" + x.depart_offset_min + " min" : "now"}: ${x.feasible ? "feasible" : "blocked"} (${Math.round(x.p_safe * 100)}%)`).join(" · ");
      const blk = (dr.blockers || []).map((b) => `<li>${esc(b.summary)} at ${esc(b.arrival_time)}</li>`).join("");
      html += `<details class="explain"><summary>Why this decision? (auditable record)</summary>
        <p class="expl-narr">${esc(dr.narrative)}</p>
        ${blk ? `<ul class="expl-blk">${blk}</ul>` : ""}
        ${cf ? `<p class="hint"><strong>If you leave later:</strong> ${esc(cf)}</p>` : ""}
        ${dr.baseline_present_tense && dr.baseline_present_tense.feasible && !dr.feasible ? `<p class="baseline">${esc(dr.baseline_present_tense.note)}</p>` : ""}
        <p class="hint">Method: ${dr.method.chunks_evaluated} route segments × time windows · hazard v${dr.method.hazard_version}</p></details>`;
    }
    html += `<button class="ghost" id="packBtn">Save offline pack for this area</button>`;
    $("planOut").innerHTML = html;
    if (chosen) { $("startTrip").addEventListener("click", () => startTrip(chosen.id)); renderLeaveBy(p.leave_by, "leaveBy", false); }
    $("packBtn").addEventListener("click", savePack);
  }
  function routeLabel(r) {
    return r.feasible ? `${Math.round(r.duration_s / 60)} min · ${Math.round(r.p_safe * 100)}% safe` : `Unsafe: ${r.reason}`;
  }

  async function savePack() {
    try {
      const pk = await api(`/offline/pack?lon=${st.o[0]}&lat=${st.o[1]}&radius_km=6&hours=3`);
      const blob = new Blob([JSON.stringify(pk)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = "offline-pack.json"; a.click();
      toast(`Offline pack saved: ${pk.slices.length} hazard areas, ${pk.shelters.length} shelters, valid until ${fmt(pk.valid_until)}.`, "good");
    } catch (e) { toastErr(e); }
  }

  // ------------------------------------------------------------------ trips
  async function startTrip(routeId) {
    try {
      const t = await api("/trips", { method: "POST", body: { route_id: routeId } });
      st.trip = t.id; st.since = 0; st.tripUrg = null; await pollTrip();
      toast("Trip started. You'll be re-routed if the forecast changes on the road ahead.", "good");
    } catch (e) { toastErr(e); }
  }
  async function pollTrip() {
    if (!st.trip) return;
    const t = await api(`/trips/${st.trip}?since=${st.since}`);
    tripLayer.clearLayers();
    L.polyline(t.geometry.coordinates.map((c) => [c[1], c[0]]), { color: "#237a4b", weight: 7 }).addTo(tripLayer);
    L.circleMarker([t.position[1], t.position[0]], { radius: 9, color: "#fff", weight: 3, fillColor: "#1b2a33", fillOpacity: 1 }).addTo(tripLayer).bindTooltip("Your vehicle");
    t.events.forEach((e) => {
      st.since = Math.max(st.since, e.seq);
      if (e.type === "reroute") { routeLayer.clearLayers(); toast(`Re-routed: ${e.reason}. New route has ${mins(e.margin_s) ?? "no"} min margin.`, "bad"); }
      if (e.type === "no_safe_route") toast(`No safe way ahead: ${e.advice}`, "bad");
      if (e.type === "arrived") toast("You have arrived.", "good");
    });
    $("tripOut").innerHTML = `<p class="hint">Trip ${esc(t.id)}: ${esc(t.status)}${t.reroutes ? `, re-routed ${t.reroutes}×` : ""}. ` +
      `Arrival about ${fmt(t.eta)}. ${t.status === "active" ? `<button class="ghost" id="cancelTrip">End trip</button>` : ""}</p>`;
    if (t.status === "active") {
      $("tripOut").insertAdjacentHTML("beforeend", `<div id="tripLeaveBy" aria-live="polite"></div>`);
      try {
        const lb = await api(`/trips/${t.id}/leave-by`);
        renderLeaveBy(lb, "tripLeaveBy", true);
        if (lb.urgency === "red" && st.tripUrg !== "red") toast(lb.message, "bad");
        st.tripUrg = lb.urgency;
      } catch (e) { /* the trip card still works without the countdown */ }
    }
    if ($("cancelTrip")) $("cancelTrip").onclick = async () => { await api(`/trips/${st.trip}`, { method: "DELETE" }); st.trip = null; tripLayer.clearLayers(); $("tripOut").innerHTML = ""; };
    if (t.status !== "active") st.trip = null;
  }
  setInterval(() => { if (st.trip) pollTrip().catch(() => {}); }, 4000);

  // ------------------------------------------------------------------ assistant
  document.querySelectorAll(".chip").forEach((c) => c.addEventListener("click", () => { $("question").value = c.dataset.q; $("askBtn").click(); }));
  $("askBtn").addEventListener("click", async () => {
    const q = $("question").value.trim();
    if (!q) return;
    $("askBtn").disabled = true; $("askOut").innerHTML = `<p class="hint">Checking…</p>`;
    try {
      const lang = $("lang").value;
      const location = st.askLocation || st.o;
      const horizonMin = 60;
      const a = await api("/assistant/ask", { method: "POST", body: { question: q, lang, location, destination: st.d, vehicle: $("vehicle").value, horizon_min: horizonMin } });
      const localTypes = new Set(["hazard_warning", "route_recommendation", "no_safe_route", "shelter_recommendation", "hazard_report"]);
      const hasLocalResult = a.blocks.some((b) => localTypes.has(b.type));
      if (hasLocalResult && location) map.setView([location[1], location[0]], Math.max(map.getZoom(), 13));
      const blocks = a.blocks.map((b) => `<div class="blk ${esc(b.type)} ${lang}" lang="${lang}">${esc(b.text)}
        <span class="ids">${b.type === "general_information"
          ? '<span class="hint">General information · not checked against local data</span>'
          : b.type === "needs_location"
            ? ""
            : b.type === "place_distance" || b.type === "place_lookup"
              ? b.verified ? '<span class="check">OpenStreetMap place lookup</span>' : '<span class="hint">Place lookup did not complete</span>'
            : b.type === "instruction"
              ? '<span class="check">Safety guidance</span>'
              : a.synthetic && localTypes.has(b.type)
            ? '<strong class="demo-warning">SIMULATED DEMO DATA — NOT a real hazard, shelter, or route report</strong>'
              : b.verified ? '<span class="check">Checked against current app data</span>' : '<span class="hint">Not verified against local data</span>'}
          ${b.ids.length ? " · " + b.ids.map(esc).join(", ") : ""}</span></div>`).join("");
      const demoNotice = a.synthetic && hasLocalResult
        ? '<p class="demo-warning" role="note">This app is in simulation mode. Hazard, shelter, and route results below are generated from demo data; they do not describe real current conditions. Do not use them for emergency decisions.</p>'
        : "";
      const ex = a.explanation ? `<p class="expl">AI explanation (not safety-checked): ${esc(a.explanation)}</p>` : "";
      const note = a.refused ? `<p class="hint">The assistant could not verify an answer, so only general guidance is shown.</p>` : (a.attempts > 1 ? `<p class="hint">Corrected after ${a.attempts - 1} failed check(s).</p>` : "");
      $("askOut").innerHTML = `<div class="answer">${demoNotice}${note}${blocks}${ex}</div>`;
    } catch (e) { $("askOut").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
    finally { $("askBtn").disabled = false; }
  });

  // ------------------------------------------------------------------ reports
  $("reportBtn").addEventListener("click", async () => {
    try {
      const r = await api("/reports", { method: "POST", body: { lon: st.r[0], lat: st.r[1], hazard: $("rHazard").value, depth: $("rDepth").value, text: $("rText").value } });
      $("reportOut").innerHTML = r.duplicate ? `<p class="hint">You already reported this spot recently; we refreshed your report.</p>`
        : `<p class="hint">Report ${esc(r.evidence_id)} sent${r.auto_approved ? " and confirmed by other reports nearby — routes are being updated." : ". It will affect routes once it is reviewed or confirmed by others."}</p>`;
    } catch (e) { toastErr(e); }
  });

  // ------------------------------------------------------------------ admin
  async function loadPending() {
    try {
      const items = await api("/admin/pending");
      $("pendingOut").innerHTML = items.length ? items.map((p) => `<div class="pending"><strong>${esc(p.class)}</strong> · ${Math.round(p.probability * 100)}% confidence · ${fmt(p.created_at)}
        <div class="hint">${esc(p.note)}</div><div class="row"><button class="primary" data-ok="${p.id}">Approve</button><button class="ghost" data-no="${p.id}">Reject</button></div></div>`).join("")
        : `<p class="hint">Nothing waiting for review.</p>`;
      $("pendingOut").querySelectorAll("[data-ok],[data-no]").forEach((b) => b.addEventListener("click", async () => {
        const id = b.dataset.ok || b.dataset.no;
        const r = await api(`/admin/slices/${id}/${b.dataset.ok ? "approve" : "reject"}`, { method: "POST" });
        toast(`${b.dataset.ok ? "Approved" : "Rejected"} ${id}. Re-checked ${r.revalidation ? r.revalidation.re_evaluated : 0} affected trips.`, "good");
        loadPending(); refreshAll();
      }));
    } catch (e) { $("pendingOut").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
  }
  $("refreshPending").addEventListener("click", loadPending);
  $("closureStart").addEventListener("click", () => { st.closurePts = []; $("closureSave").disabled = true; toast("Click two points along the road to close.", ""); });
  $("closureSave").addEventListener("click", async () => {
    try {
      const r = await api("/admin/closures", { method: "POST", body: { geometry: { type: "LineString", coordinates: st.closurePts }, hazard: "closure", hours: 6, note: "closed from console" } });
      toast(`Road closed (${r.slice_id}). Re-checked ${r.revalidation.re_evaluated} trips.`, "good");
      st.closurePts = null; $("closureSave").disabled = true; drawMarks(); refreshAll();
    } catch (e) { toastErr(e); }
  });
  $("capBtn").addEventListener("click", async () => {
    try {
      const r = await api("/admin/cap", { method: "POST", raw: $("capXml").value });
      $("adminOut").innerHTML = `<p class="hint">Loaded ${r.ingested} alert area(s); hazard forecast refreshed.</p>`; refreshAll();
    } catch (e) { $("adminOut").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
  });

  // ------------------------------------------------------------------ what-if time, risk heatmap
  const slider = $("depart");
  function offsetLabel() {
    const m = +slider.value;
    if (!m) return "now";
    const base = st.status ? st.status.now : Date.now() / 1000;
    return `in ${m} min (${fmt(base + m * 60)})`;
  }
  function heatColor(v) {          // pale yellow -> orange -> red; transparent below 5 %
    const lerp = (a, b, t) => Math.round(a + (b - a) * t);
    const [r, g, b] = v < 0.5
      ? [lerp(255, 247, v / 0.5), lerp(214, 148, v / 0.5), lerp(102, 29, v / 0.5)]
      : [lerp(247, 184, (v - 0.5) / 0.5), lerp(148, 50, (v - 0.5) / 0.5), lerp(29, 42, (v - 0.5) / 0.5)];
    return [r, g, b, Math.round(255 * (0.2 + 0.55 * v))];
  }
  function heatDataUrl(h) {        // grid row 0 is the southern edge, canvas row 0 the northern one
    const cv = document.createElement("canvas");
    cv.width = h.nx; cv.height = h.ny;
    const ctx = cv.getContext("2d");
    const img = ctx.createImageData(h.nx, h.ny);
    for (let r = 0; r < h.ny; r++) {
      for (let c = 0; c < h.nx; c++) {
        const v = h.values[r][c];
        if (v < 0.05) continue;
        const i = ((h.ny - 1 - r) * h.nx + c) * 4;
        const px = heatColor(Math.min(1, v));
        img.data[i] = px[0]; img.data[i + 1] = px[1]; img.data[i + 2] = px[2]; img.data[i + 3] = px[3];
      }
    }
    ctx.putImageData(img, 0, 0);
    return cv.toDataURL();
  }
  async function drawHeat() {
    const seq = ++st.heatSeq;
    if (!$("heatOn").checked) {
      if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
      $("heatNote").textContent = "";
      return;
    }
    const q = new URLSearchParams({ offset_min: slider.value, vehicle: $("vehicle").value });
    if ($("heatCls").value) q.set("hazard_class", $("heatCls").value);
    try {
      const h = await api("/hazards/heatmap?" + q);
      if (seq !== st.heatSeq) return;                       // a newer request is already on its way
      const bounds = [[h.bbox[1], h.bbox[0]], [h.bbox[3], h.bbox[2]]];
      const url = heatDataUrl(h);
      if (heatLayer) { heatLayer.setUrl(url); heatLayer.setBounds(bounds); }
      else heatLayer = L.imageOverlay(url, bounds, { pane: "heat", interactive: false, className: "heat-img" }).addTo(map);
      const veh = $("vehicle").selectedOptions[0] ? $("vehicle").selectedOptions[0].textContent.split(" (")[0].toLowerCase() : "vehicle";
      $("heatNote").textContent = h.max < 0.05
        ? `Leaving ${offsetLabel()}: no hazard is forecast for a ${veh} in this area.`
        : `Leaving ${offsetLabel()}: ${Math.round(h.share_at_risk * 100)}% of the area is likely hazardous for a ${veh}; peak likelihood ${Math.round(h.max * 100)}%.`
          + (h.synthetic ? " (simulated storm)" : "");
    } catch (e) { $("heatNote").textContent = "Heatmap unavailable: " + e.message; }
  }
  slider.addEventListener("input", () => {
    $("departOut").textContent = offsetLabel();
    clearTimeout(st.heatT);
    st.heatT = setTimeout(drawHeat, 120);
  });
  slider.addEventListener("change", () => { if (st.o && st.d && !$("planBtn").disabled) $("planBtn").click(); });   // what-if: re-plan for that departure
  ["heatOn", "heatCls", "vehicle"].forEach((id) => $(id).addEventListener("change", drawHeat));

  // ------------------------------------------------------------------ simulated storm clock
  $("advBtn").addEventListener("click", async () => {
    $("advBtn").disabled = true;
    try {
      await api("/scenario/advance?minutes=15", { method: "POST" });
      await refreshAll(); await drawHeat(); await refreshLeaveBy();
      toast("Simulated storm moved forward 15 minutes.", "");
    } catch (e) { toastErr(e); } finally { $("advBtn").disabled = false; }
  });

  // ------------------------------------------------------------------ point of no return (leave-by)
  function renderLeaveBy(lb, target, trip) {
    const el = $(target);
    if (!el || !lb) return;
    if (lb.status === "inactive") { el.innerHTML = ""; return; }
    const now = st.status ? st.status.now : lb.start_t;
    let title, big, sub, fill;
    if (lb.status === "countdown") {
      const left = Math.max(0, Math.round((lb.leave_by_t - now) / 60));
      fill = Math.max(4, Math.min(100, ((lb.leave_by_t - now) / 3600) * 100));
      if (trip) { title = "Delay budget"; big = `${Math.max(0, Math.round(lb.seconds_left / 60))} min of delay left`; sub = `Road ahead predicted cut off around ${fmt(lb.leave_by_t)}`; }
      else { title = "Point of no return"; big = left <= 0 ? "Leave now" : `Leave by ${fmt(lb.leave_by_t)}`; sub = left <= 0 ? "The route is about to be cut off" : `${left} min from now`; }
    } else if (lb.status === "clear") {
      title = trip ? "Delay budget" : "Point of no return"; big = "No cut-off forecast"; sub = `Checked for the next ${Math.round(lb.window_s / 60)} min`; fill = 100;
    } else {
      title = trip ? "Delay budget" : "Point of no return"; big = "Blocked now";
      sub = lb.reopens_in_s ? `May reopen in about ${Math.round(lb.reopens_in_s / 60)} min` : "No reopening inside the forecast window"; fill = 0;
    }
    const cls = lb.urgency === "none" ? "green" : lb.urgency;
    el.innerHTML = `<div class="leaveby ${esc(cls)}"><h4>${esc(title)}</h4><div class="big">${esc(big)}</div><p class="sub">${esc(sub)}</p>
      <div class="bar" role="img" aria-label="${esc(big)}"><b style="width:${fill}%"></b></div>
      <p class="msg" lang="en">${esc(lb.message)}</p>${lb.forecast_limited ? `<p class="hint">Only the next ${Math.round(lb.window_s / 60)} min of forecast could be checked.</p>` : ""}</div>`;
  }
  async function refreshLeaveBy() {
    const c = st.plan && st.plan.routes.find((r) => r.chosen);
    if (!c || !$("leaveBy")) return;
    try { renderLeaveBy(await api(`/routes/${c.id}/leave-by`), "leaveBy", false); } catch (e) { /* route may have expired */ }
  }

  // ------------------------------------------------------------------ SMS fallback (simulated phone)
  const scriptLang = (t) => /[ऀ-ॿ]/.test(t) ? "hi" : /[஀-௿]/.test(t) ? "ta" : "en";
  function renderPhone(msgs) {
    $("smsHeadNum").textContent = $("smsPhone").value.trim() || "Simulated phone";
    if (!msgs.length) { $("phoneMsgs").innerHTML = `<p class="hint">No messages yet.</p>`; return; }
    $("phoneMsgs").innerHTML = msgs.map((m) => {
      const out = m.direction === "out";
      const badge = !out ? "" : m.verified === false ? ` · <span class="warn">could not verify, safe fallback text</span>` : ` · <span class="ok">verified</span>`;
      return `<div class="bubble ${out ? "out" : "in"}${out && m.verified === false ? " unverified" : ""}" lang="${scriptLang(m.body)}">${esc(m.body)}
        <span class="meta">${fmt(m.t)} · ${esc(m.encoding)} · ${m.segments} segment${m.segments > 1 ? "s" : ""}${badge}</span></div>`;
    }).join("");
    $("phoneMsgs").scrollTop = $("phoneMsgs").scrollHeight;
  }
  async function smsRefresh() {
    try {
      const r = await api("/sms/outbox?limit=60&phone=" + encodeURIComponent($("smsPhone").value.trim()));
      renderPhone(r.messages);
    } catch (e) { $("phoneMsgs").innerHTML = `<p class="hint">${esc(e.message)}</p>`; }
  }
  async function smsSend(kind) {
    const body = { to: $("smsPhone").value.trim(), lang: $("smsLang").value, kind, vehicle: $("vehicle").value };
    if (kind === "route") {
      const c = st.plan && st.plan.routes.find((r) => r.chosen);
      if (!c) { toast("Find a safe route first (Route tab).", "bad"); return; }
      body.route_id = c.id;
    } else if (kind === "trip") {
      if (!st.trip) { toast("Start a trip first (Route tab).", "bad"); return; }
      body.trip_id = st.trip;
    } else {
      const ll = st.o || st.askLocation;
      if (!ll) { toast("Click the map to set a start point first.", "bad"); return; }
      body.lon = ll[0]; body.lat = ll[1];
    }
    try {
      const r = await api("/sms/send", { method: "POST", body });
      if (r.suppressed) toast("That number replied STOP, so nothing was sent.", "bad");
      await smsRefresh();
    } catch (e) { toastErr(e); }
  }
  async function smsReply(text) {
    if (!text.trim()) return;
    try {
      await api("/sms/inbound", { method: "POST", body: { from: $("smsPhone").value.trim(), text } });
      await smsRefresh();
    } catch (e) { toastErr(e); }
  }
  $("smsRoute").addEventListener("click", () => smsSend("route"));
  $("smsTrip").addEventListener("click", () => smsSend("trip"));
  $("smsShelter").addEventListener("click", () => smsSend("shelter"));
  $("smsReplyForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("smsReply").value; $("smsReply").value = ""; smsReply(v); });
  $("smsPhone").addEventListener("change", smsRefresh);
  document.querySelectorAll(".schip").forEach((c) => c.addEventListener("click", () => {
    if (c.id === "chipRoute") {
      const ch = st.plan && st.plan.routes.find((r) => r.chosen);
      $("smsReply").value = "ROUTE " + (ch ? ch.route_code : ""); $("smsReply").focus();
    } else smsReply(c.dataset.reply);
  }));

  // ------------------------------------------------------------------ toast
  function toast(msg, kind) {
    const el = document.createElement("div");
    el.className = "t " + (kind || ""); el.textContent = msg;
    $("toast").appendChild(el);
    setTimeout(() => el.remove(), 7000);
  }
  function toastErr(e) { toast(e.message || String(e), "bad"); }

  // ------------------------------------------------------------------ boot
  (async function boot() {
    try {
      const veh = await api("/vehicles");
      $("vehicle").innerHTML = veh.map((v) => `<option value="${v.key}" ${v.key === "car" ? "selected" : ""}>${esc(v.label)} (water up to ${Math.round(v.max_water_m * 100)} cm)</option>`).join("");
      const s = await loadStatus();
      const bb = s.region.bbox;
      map.fitBounds([[bb[1], bb[0]], [bb[3], bb[2]]]);
      L.rectangle([[bb[1], bb[0]], [bb[3], bb[2]]], { color: "#1b2a33", weight: 1, fill: false, dashArray: "4 4", interactive: false }).addTo(map);
      drawHeat();
    } catch (e) {
      $("modeNote").textContent = "Cannot reach the safety engine: " + e.message;
    }
  })();
})();
