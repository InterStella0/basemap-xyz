use poem::http::{header, StatusCode};
use poem::web::{Data, Path};
use poem::{handler, Body, IntoResponse, Response};

use crate::metrics::Metrics;
use crate::renderer::RenderError;
use crate::state::{AppState, TileFailure};
use crate::tiles::TileCoord;

/// The miss path. In production nginx serves hits off the shared volume with `try_files` and only
/// falls back here, so most of what this handler sees is genuinely uncached. It still checks the
/// cache itself, so hitting the API directly (no proxy) behaves identically.
#[handler]
pub async fn get_tile(
    Path((layer, z, x, y)): Path<(String, String, String, String)>,
    Data(state): Data<&AppState>,
) -> Response {
    Metrics::bump(&state.metrics.requests);

    // Parsed by hand rather than through the extractor so that anything unservable — a bad zoom, a
    // non-numeric segment, a tile off the edge of the pyramid, an unsafe layer name — is a uniform
    // 404 instead of a mix of 400s and 404s. Crawlers produce a lot of these.
    let Some(coord) = parse_coord(&layer, &z, &x, &y) else {
        Metrics::bump(&state.metrics.rejected);
        return not_found("malformed tile coordinate");
    };
    if let Err(err) = coord.validate(state.config.min_zoom, state.config.max_zoom) {
        Metrics::bump(&state.metrics.rejected);
        return not_found(&err.to_string());
    }

    match state.get_or_render(coord).await {
        Ok(bytes) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "image/png")
            .header(header::CACHE_CONTROL, format!("public, max-age={}", state.config.client_max_age))
            // Required for MapLibre/OpenLayers and anything that reads tiles back off a canvas.
            // nginx sets this too; duplicated here so a direct-to-API deployment is not broken.
            .header(header::ACCESS_CONTROL_ALLOW_ORIGIN, "*")
            .header("X-Tile-Cache", "MISS")
            .body(Body::from_bytes(bytes)),
        Err(failure) => error_response(&failure, &state.metrics),
    }
}

fn parse_coord(layer: &str, z: &str, x: &str, y: &str) -> Option<TileCoord> {
    // Both `/tiles/{layer}/{z}/{x}/{y}.png` and the extension-less form are accepted; some
    // desktop clients build the URL without a suffix.
    let y = y.strip_suffix(".png").unwrap_or(y);
    Some(TileCoord::new(layer, z.parse().ok()?, x.parse().ok()?, y.parse().ok()?))
}

fn not_found(message: &str) -> Response {
    let mut response = (StatusCode::NOT_FOUND, message.to_string()).into_response();
    // A 404 here is a property of the coordinate, not of the moment, so it is safe to let clients
    // remember it for a while rather than re-asking on every pan.
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("public, max-age=3600"),
    );
    response
}

fn error_response(failure: &TileFailure, metrics: &Metrics) -> Response {
    let (status, retry_after) = match failure {
        // Backpressure, not breakage: every render slot is busy. Tell the client to come back.
        TileFailure::Render(RenderError::QueueTimeout(_)) => (StatusCode::SERVICE_UNAVAILABLE, Some(5)),
        // The renderer itself is unreachable, or the circuit breaker has declared it down. A 503
        // tells clients the tile may exist later; the breaker makes these answers fast and the
        // negative cache deliberately does not suppress them, so the contract holds for every
        // miss while the renderer is gone.
        TileFailure::Render(err) if err.is_renderer_down() => {
            Metrics::bump(&metrics.renderer_unavailable);
            (StatusCode::SERVICE_UNAVAILABLE, Some(30))
        }
        TileFailure::RecentlyFailed => (StatusCode::BAD_GATEWAY, Some(30)),
        TileFailure::Render(_) => (StatusCode::BAD_GATEWAY, Some(10)),
    };

    let mut response = (status, failure.to_string()).into_response();
    // Errors must never be cached as if they were tiles — by the browser, by a CDN, or by us.
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        header::HeaderValue::from_static("no-store"),
    );
    if let Some(seconds) = retry_after {
        response
            .headers_mut()
            .insert(header::RETRY_AFTER, header::HeaderValue::from(seconds));
    }
    response
}
