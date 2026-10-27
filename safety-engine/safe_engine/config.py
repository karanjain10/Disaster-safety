"""Runtime configuration (environment variables with safe defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # --- mode -------------------------------------------------------------
    # "scenario": synthetic terrain + simulated storm (works fully offline, used for demo/eval)
    # "live": DEM file + live rainfall API + OSRM (needs internet / data files)
    mode: str = field(default_factory=lambda: _env("ENGINE_MODE", "scenario"))
    region_lon0: float = field(default_factory=lambda: float(_env("REGION_LON0", "80.150")))
    region_lat0: float = field(default_factory=lambda: float(_env("REGION_LAT0", "12.950")))
    region_width_m: float = field(default_factory=lambda: float(_env("REGION_WIDTH_M", "16000")))
    region_height_m: float = field(default_factory=lambda: float(_env("REGION_HEIGHT_M", "16500")))
    cell_m: float = field(default_factory=lambda: float(_env("CELL_M", "100")))
    dem_path: str = field(default_factory=lambda: _env("DEM_PATH", ""))  # ESRI ASCII grid (.asc)

    # --- routing ----------------------------------------------------------
    routing_engine: str = field(default_factory=lambda: _env("ROUTING_ENGINE", "local"))  # local | osrm
    osrm_url: str = field(default_factory=lambda: _env("OSRM_URL", "https://router.project-osrm.org"))
    k_alternatives: int = field(default_factory=lambda: int(_env("K_ALTERNATIVES", "3")))

    # --- forecasting / hazard geometry ------------------------------------
    horizons_min: tuple = (0, 15, 30, 60, 90, 120)
    index_bucket_s: int = 900

    # --- feasibility --------------------------------------------------------
    alpha: float = field(default_factory=lambda: float(_env("SAFETY_ALPHA", "0.97")))
    p_hard_block: float = field(default_factory=lambda: float(_env("P_HARD_BLOCK", "0.30")))
    eta_rel_uncertainty: float = 0.15
    eta_abs_uncertainty_s: float = 60.0
    chunk_m: float = 120.0

    # --- detours ------------------------------------------------------------
    detour_max_iter: int = 4
    detour_initial_buffer_m: float = 150.0
    detour_time_budget_s: float = 2.5

    # --- trips --------------------------------------------------------------
    trip_cell_m: float = 250.0
    reroute_margin_min_s: float = 600.0
    reroute_hysteresis: float = 0.05
    capacity_veh_per_hr: float = 600.0
    capacity_gamma: float = 1.5

    # --- data freshness -----------------------------------------------------
    hazard_ttl_s: float = field(default_factory=lambda: float(_env("HAZARD_TTL_S", "1800")))
    object_ttl_s: float = field(default_factory=lambda: float(_env("OBJECT_TTL_S", "900")))

    # --- assistant ----------------------------------------------------------
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY", ""))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "llama-3.3-70b-versatile"))
    llm_max_attempts: int = 3

    # --- live data sources --------------------------------------------------
    cap_feed_url: str = field(default_factory=lambda: _env("CAP_FEED_URL", ""))
    rainfall_api_url: str = field(default_factory=lambda: _env("RAINFALL_API_URL", "https://api.open-meteo.com/v1/forecast"))
    geocoder_url: str = field(default_factory=lambda: _env("GEOCODER_URL", "https://nominatim.openstreetmap.org"))
    geocoder_user_agent: str = field(default_factory=lambda: _env(
        "GEOCODER_USER_AGENT", "RoadManagementSafetyEngine/1.0 (educational prototype)"
    ))

    # --- security -----------------------------------------------------------
    engine_key: str = field(default_factory=lambda: _env("ENGINE_KEY", ""))  # shared secret with gateway

    model_dir: str = field(default_factory=lambda: _env("MODEL_DIR", os.path.join(os.path.dirname(__file__), "..", "models")))
    seed: int = field(default_factory=lambda: int(_env("SCENARIO_SEED", "7")))


settings = Settings()
