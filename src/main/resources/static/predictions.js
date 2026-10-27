(function () {
  "use strict";
  const API = (typeof window.ENGINE_BASE === "string") ? window.ENGINE_BASE : "/engine";
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const map = L.map("map", { zoomControl: true }).setView([20.5937, 78.9629], 5);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18, attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);
  const selection = L.layerGroup().addTo(map);
  let point = null;
  let regionBounds = null;
  let synthetic = true;

  async function api(path) {
    const res = await fetch(API + path, { credentials: "same-origin" });
    if (!res.ok) {
      let message = `${res.status} ${res.statusText}`;
      try { const body = await res.json(); message = body.detail || message; } catch (_) {}
      throw new Error(message);
    }
    return res.json();
  }

  function showPoint(lon, lat) {
    point = [lon, lat];
    selection.clearLayers();
    L.circleMarker([lat, lon], { radius: 8, color: "#fff", weight: 2, fillColor: "#b8322a", fillOpacity: 1 }).addTo(selection);
    $("locationText").textContent = `Selected point: ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
    $("predictBtn").disabled = false;
  }

  function renderForecast(fc) {
    const probability = fc.probabilities.flood_depth_ge_15cm;
    $("result").innerHTML = `<section class="forecast">
      <h3>Model estimates · ${fc.horizon_min}-minute horizon</h3>
      <p class="notice synthetic">SYNTHETIC DEMO OUTPUT — not trained on the NASA dataset and not a real-world prediction.</p>
      <div class="metric">
        <div class="metric-row"><span>Flood probability (water ≥15 cm)</span><strong>${Math.round(probability * 100)}%</strong></div>
        <div class="bar"><span style="width:${Math.round(probability * 100)}%"></span></div>
      </div>
      <p>Estimated water depth: ${fc.physics_lite_depth_m} m</p>
      <p class="hint">Issued ${new Date(fc.issued_at * 1000).toLocaleString()} · valid to ${new Date(fc.valid_until * 1000).toLocaleString()}</p>
      ${((fc.attribution && fc.attribution.flood) || []).map((f) =>
        `<p class="hint"><strong>Flood driver:</strong> ${esc(f.label)} (${esc(f.direction)} risk)</p>`).join("") || '<p class="hint">No leading flood feature identified.</p>'}
    </section>`;
  }

  $("predictBtn").addEventListener("click", async () => {
    if (!point) return;
    $("predictBtn").disabled = true;
    $("result").innerHTML = '<p class="hint">Calculating point forecast…</p>';
    try {
      const horizon = +$("horizon").value;
      const fc = await api(`/forecast/point?lon=${point[0]}&lat=${point[1]}&horizon_min=${horizon}`);
      renderForecast(fc);
    } catch (e) {
      $("result").innerHTML = `<p class="notice">${esc(e.message)}. The selected point must be inside the outlined configured model region.</p>`;
    } finally {
      $("predictBtn").disabled = false;
    }
  });

  map.on("click", (e) => showPoint(e.latlng.lng, e.latlng.lat));

  (async function boot() {
    try {
      const [status, metrics] = await Promise.all([api("/status"), api("/model/metrics")]);
      synthetic = status.synthetic;
      const bb = status.region.bbox;
      regionBounds = [[bb[1], bb[0]], [bb[3], bb[2]]];
      L.rectangle(regionBounds, { color: "#1b2a33", weight: 2, fill: false, dashArray: "6 5", interactive: false }).addTo(map);
      map.fitBounds(regionBounds);
      const modeNote = synthetic
        ? `Current engine mode: ${esc(status.mode)}. The loaded forecast model uses synthetic training data.`
        : `Current engine mode: ${esc(status.mode)}. Review the model's training-data provenance before interpreting results.`;
      $("modeBanner").className = synthetic ? "notice synthetic" : "notice";
      $("modeBanner").textContent = modeNote;
      $("datasetStatus").textContent = synthetic
        ? `NASA sources not connected; current model metadata: ${metrics.data || "not reported"}.`
        : `NASA Global Flood Database / GPM IMERG not confirmed; current model metadata: ${metrics.data || "not reported"}.`;
      $("predictBtn").disabled = !synthetic;
      $("unavailableMessage").textContent = synthetic
        ? "Real-data predictions are unavailable. The button runs the existing synthetic model only; NASA sources are not connected."
        : "Prediction unavailable: no verified model trained with the selected NASA flood data and no NASA rainfall pipeline are configured.";
      showPoint(status.region.lon0, status.region.lat0);
    } catch (e) {
      $("modeBanner").className = "notice synthetic";
      $("modeBanner").textContent = `Prediction service unavailable: ${e.message}`;
      $("datasetStatus").textContent = "Cannot verify data-source connection.";
    }
  })();
})();
