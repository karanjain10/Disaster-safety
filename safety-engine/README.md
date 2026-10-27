# Safety engine (modules M1–M6)

Python 3.11+ · FastAPI · Shapely · scikit-learn. Runs fully offline in *scenario* mode.

```bash
pip install -r requirements.txt
uvicorn safe_engine.api:app --port 8000      # console at http://localhost:8000, API docs at /docs
pytest -q                                    # regression tests
python -m safe_engine.train                  # retrain M1 (≈45 s) -> models/hazard_model.pkl
python -m evaluation.run_eval                # benchmarks -> evaluation/results/RESULTS.md
```

## Modules and where they live

| Module | File | What it does | Measured effect (simulation) |
|---|---|---|---|
| Evidence fusion | `fusion.py` | SACHET/CAP 1.2 alerts, crowd reports, water-level sensors; time-decayed confidence; one vote per reporter per 250 m cell; Beta reputation; noisy-OR; bounded log-odds update of M1 | fewer false closures from single reports |
| **M1** | `hazard_model.py`, `train.py` | Two-stage model (terrain susceptibility prior → gradient-boosted temporal trigger) with isotonic calibration; per cell × horizon (0–120 min) for flood and landslide; storm from forecast wind distribution | AUC-PR and calibration vs the original Random Forest (E1) |
| **M2** | `geometry.py` | Class-specific transforms from probability to time-sliced polygons: flood via DEM-depression depth with a probability-dependent level; landslide via probability-dependent susceptibility cut-off + run-out buffer; storm via probability-scaled buffer. Banded, conservative | routable hazard geometry |
| Index / store | `slices.py` | Versioned hazard store (model slices auto-approved; crowd / LLM slices pending; admin slices approved); raster diff gives the exact changed region per time bucket; time-bucketed STRtree with prepared geometries | geometry tests per request (E2) |
| **M3** | `feasibility.py` | Per-chunk arrival interval with uncertainty; only slices overlapping in space **and** time are tested; vehicle thresholds (`vehicles.py`); bridges exempt from ponding; hard block / chance constraint P_safe ≥ α; safety margin in minutes | unsafe-recommendation and false-refusal rates (E2) |
| **M4** | `detour.py` | Via-waypoints from the convex hull of the buffered first obstruction, snapped to the network; re-query the unchanged engine; iterate with a larger buffer under a time budget | detour success, latency (E3) |
| **M5** | `trips.py`, `service.on_new_version` | Reverse index cell → (trip, time window); only trips whose remaining path meets a changed part **during its time window** are re-evaluated; hysteresis on re-routing; segment-hour load counters for capacity-aware selection | share of trips re-evaluated (E4), peak load (E5) |
| **M6** | `assistant/` | LLM (Groq, tool calling) or the offline rule planner handles general educational geography / hazard questions separately from local route, shelter, and hazard lookups; `gate.py` verifies local claims and labels general information as unverified, re-assesses routes now, checks shelters at arrival time, enforces TTLs, renders safety text from `templates.py` (en/hi/ta), strips imperative advice from free text | wrong claims released with vs without gate (E6) |
| **M7** | `explain.py` | Auditable **decision records**: M3 blockers, counterfactual departures, baseline B1 comparison, local M1 feature attribution at blockers; `GET /routes/{id}/explain`, `GET /forecast/point`; included in `POST /plan` for the chosen route | reviewer audit / regulatory traceability |

Ask accepts general educational geography and hazard questions without a selected map location. For local hazard questions, select the Ask tab and click the map to set the location; the answer uses the app's local hazard data and may be simulated. Place lookups (for example, "Where is Mumbai?") and distances between named places work without a map selection. With `GROQ_API_KEY`, the Groq planner can answer broader free-form questions; without it, the offline planner supports common built-in topics and prompts for location when a question requires local conditions. General answers are explicitly marked as not verified against local/live data. Current hazard, route, and shelter answers still depend on the configured engine data (synthetic in scenario mode).

The routing console map is kept focused on route selection and routing results. Flood point forecasts are in the separate Prediction Lab at `/predictions.html`; it calls `GET /forecast/point` and is limited to the configured model region. The proposed flood-data pipeline uses NASA's Global Flood Database for historical flood extent labels and NASA GPM IMERG for precipitation observations. These NASA sources are not connected in the current app: the default model is trained on synthetic scenarios, and the Prediction Lab marks those forecasts as simulated. GPM observations alone do not provide future rainfall; a forecast feed is also needed for forward-looking predictions. The historical flood database is for training/evaluation, not itself a live forecast feed. Do not treat demo output as a real-world forecast.

Place-distance questions use OpenStreetMap Nominatim to geocode place names and the configured OSRM service for an approximate driving distance; straight-line distance is calculated from the returned coordinates. Results state that road distance is not live traffic. The default public Nominatim service is rate-limited (the app caches results and spaces requests); for production, configure an appropriate provider or self-host geocoding/routing rather than relying on public demo services. Set `GEOCODER_URL`, `GEOCODER_USER_AGENT`, and `OSRM_URL` to configure the services. OpenStreetMap data is © OpenStreetMap contributors.
| Offline | `offline.py`, `service.offline_pack` | Region pack (hazards, shelters, SOP text, road graph) and < 40-character route codes for SMS / mesh | pack size, code length (E7) |
| Routing | `routing/` | `LocalGraphEngine` (built-in, offline, marks bridges) and `OsrmEngine` (self-hosted or public OSRM, per-segment durations) | — |

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `ENGINE_MODE` | `scenario` | `scenario` (simulated storm, synthetic terrain) or `live` |
| `ROUTING_ENGINE` | `local` | `local` or `osrm` (`OSRM_URL`) |
| `DEM_PATH` | – | ESRI ASCII grid for live mode (`gdal_translate -of AAIGrid srtm.tif dem.asc`) |
| `SHELTERS_PATH` | – | GeoJSON points with `name`, `capacity` |
| `REGION_LON0`, `REGION_LAT0`, `REGION_WIDTH_M`, `REGION_HEIGHT_M` | Chennai demo box | Region |
| `GROQ_API_KEY`, `GROQ_MODEL` | – | Enables the LLM; without it the rule planner is used |
| `ENGINE_KEY` | – | Shared secret required from the gateway |
| `HAZARD_TTL_S`, `OBJECT_TTL_S` | 1800, 900 | Freshness limits used by M3 (stricter α) and the gate |
| `DEMO_ADMIN` | `1` | In scenario mode, allow admin actions without the gateway role. Set `0` in any shared deployment |

## Moving from simulation to real data

1. **Terrain:** CartoDEM / SRTM 30 m → resample to 30–100 m → `DEM_PATH`.
2. **Roads:** OSRM with the OSM India extract; carry `bridge=yes` / `layer>0` into `Route.elevated`
   (needs a small OSRM annotation or a lookup against OSM ways).
3. **Rain:** IMD AWS / radar for observations; nowcast API for forecasts (`LiveWeather` implements the interface).
4. **Labels for M1:** recorded road-flooding (municipal complaints, Waze/crowd reports, news-geocoded events,
   sensor depths) and landslide inventories (GSI). Replace `generate_dataset` and keep the event-wise split.
5. **SOP text:** replace the paraphrases in `sop.py` with verbatim NDMA / SDMA clauses; get Hindi and Tamil reviewed.
6. **Vehicle thresholds:** calibrate `vehicles.py` with local guidance.

## API (all JSON; see `/docs`)

`GET /status` · `POST /plan` · `GET /routes/{id}/explain` · `GET /forecast/point` · `GET /hazards?offset_min=&window_min=` · `GET /hazards/truth` (scenario) ·
`POST /trips` · `GET /trips/{id}?since=` · `DELETE /trips/{id}` · `POST /reports` · `POST /assistant/ask` ·
`GET /shelters` · `GET /offline/pack` · `GET /routes/decode/{code}` · `GET /model/metrics` ·
admin: `GET /admin/pending` · `POST /admin/slices/{id}/approve|reject` · `POST /admin/closures` ·
`POST /admin/cap` (CAP XML body) · `GET /admin/audit` · scenario: `POST /scenario/advance?minutes=` · `POST /scenario/reset?seed=`
