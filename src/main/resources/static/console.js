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

  const st = { o: null, d: null, r: null, askLocation: null, mode: "route", status: null, plan: null, trip: null, since: 0, closurePts: null };

  const fmt = (t) => new Date(t * 1000).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
  const mins = (s) => s == null ? null : Math.max(0, Math.round(s / 60));

  // ------------------------------------------------------------------ tabs
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", x === b));
    document.querySelectorAll(".tab").forEach((t) => (t.hidden = t.id !== "tab-" + b.dataset.tab));
    st.mode = b.dataset.tab;
    if (st.mode === "admin") loadPending();
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
    st.o = st.d = null; routeLayer.clearLayers(); $("planOut").innerHTML = ""; $("planBtn").disabled = true;
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
    if (chosen) $("startTrip").addEventListener("click", () => startTrip(chosen.id));
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
      st.trip = t.id; st.since = 0; await pollTrip();
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
    } catch (e) {
      $("modeNote").textContent = "Cannot reach the safety engine: " + e.message;
    }
  })();
})();
