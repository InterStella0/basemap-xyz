use poem::handler;
use poem::web::{Data, Json};
use serde_json::json;

use crate::state::AppState;

/// Liveness only: is this process up and serving? Deliberately touches nothing downstream.
///
/// The container healthcheck polls this rather than `/health`, because `/health` probes QGIS and a
/// 10-second poll would spend one of the renderer's few FCGI slots forever, for no information the
/// orchestrator can act on.
#[handler]
pub async fn live() -> &'static str {
    "ok"
}

/// Always 200, even when the renderer is down — same contract as zegraph-web's `/health`. A probe
/// that flips to 5xx would take the container out of rotation for a dependency failure it cannot
/// fix, while cache hits are still being served perfectly well by nginx.
#[handler]
pub async fn health(Data(state): Data<&AppState>) -> Json<serde_json::Value> {
    let renderer = match state.renderer.probe().await {
        Ok(()) => json!({ "status": "up" }),
        Err(err) => json!({ "status": "down", "error": err.to_string() }),
    };
    let degraded = renderer["status"] == "down";

    Json(json!({
        "status": if degraded { "degraded" } else { "ok" },
        "version": env!("CARGO_PKG_VERSION"),
        "renderer": renderer,
        "cache": {
            "dir": state.cache.root().to_string_lossy(),
            "ttl_days": state.config.tile_ttl.as_secs() / 86_400,
            "memory_entries": state.memory_entry_count(),
        },
        "render_slots": {
            "total": state.config.render_concurrency,
            "available": state.renderer.available_permits(),
        },
        "zoom": { "min": state.config.min_zoom, "max": state.config.max_zoom },
    }))
}

#[handler]
pub async fn stats(Data(state): Data<&AppState>) -> Json<serde_json::Value> {
    let mut snapshot = state.metrics.snapshot();
    snapshot["memory_entries"] = json!(state.memory_entry_count());
    snapshot["render_slots_available"] = json!(state.renderer.available_permits());
    Json(snapshot)
}
