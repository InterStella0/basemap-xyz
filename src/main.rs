use std::sync::Arc;

use poem::listener::TcpListener;
use poem::middleware::Tracing;
use poem::{EndpointExt, Route, Server, get};

mod cache;
mod config;
mod metrics;
mod renderer;
mod routers;
mod state;
mod tiles;

use cache::TileCache;
use config::Config;
use metrics::Metrics;
use renderer::Renderer;
use state::{AppState, spawn_janitor};

/// Longer than the default 60-second renderer timeout, so a tile already being rendered can
/// finish and write its cache entry before the orchestrator stops the process. Compose gives the
/// container another five seconds beyond this deadline before resorting to SIGKILL.
const SHUTDOWN_GRACE: std::time::Duration = std::time::Duration::from_secs(70);

fn build_app(state: AppState) -> impl poem::Endpoint {
    Route::new()
        // `:y` carries the `.png` suffix; the handler strips it. Keeping the extension inside the
        // parameter rather than in the route means one handler serves both URL forms.
        .at("/tiles/:layer/:z/:x/:y", get(routers::tiles::get_tile))
        .at("/health", get(routers::health::health))
        .at("/health/live", get(routers::health::live))
        .at("/stats", get(routers::health::stats))
        .with(Tracing)
        .data(state)
}

async fn run() {
    let config = Arc::new(Config::from_env());
    let metrics = Arc::new(Metrics::default());

    if let Err(err) = tokio::fs::create_dir_all(&config.cache_dir).await {
        // Fail at boot rather than turning every tile into a 502 later. A cache we cannot write is
        // a misconfigured mount, not a transient condition.
        panic!("cannot create tile cache dir {}: {err}", config.cache_dir.display());
    }

    let cache = Arc::new(TileCache::new(config.cache_dir.clone(), config.tile_ttl));

    match &config.project_version {
        Some(version) => match cache.sync_project_version(version).await {
            Ok(true) => tracing::info!(version, "project.qgz version changed; tile cache flushed"),
            Ok(false) => {
                tracing::info!(version, "project.qgz version unchanged; tile cache retained")
            }
            // A half-wiped cache from an interrupted sync is worse than refusing to start, so this
            // fails at boot rather than serving tiles under an unknown mix of old and new renders.
            Err(err) => panic!(
                "failed to sync project version marker in {}: {err}",
                config.cache_dir.display()
            ),
        },
        None => tracing::info!(
            "PROJECT_VERSION not set; skipping version-based cache flush (TTL-only invalidation)"
        ),
    }

    let renderer = Arc::new(Renderer::new(
        config.renderer_url.clone(),
        config.render_concurrency,
        config.render_queue_timeout,
        config.render_timeout,
        config.render_failure_threshold,
        config.render_circuit_open,
        metrics.clone(),
        config.layer_routes.clone(),
    ));

    let state = AppState::new(config.clone(), cache, renderer, metrics);
    spawn_janitor(state.clone());

    tracing::info!(
        renderer = %config.renderer_url,
        cache = %config.cache_dir.display(),
        ttl_days = config.tile_ttl.as_secs() / 86_400,
        zoom = format!("{}..={}", config.min_zoom, config.max_zoom),
        render_concurrency = config.render_concurrency,
        layer_routes = ?config.layer_routes,
        "dark-basemap-xyz starting"
    );

    let app = build_app(state);
    let listener = TcpListener::bind(format!("0.0.0.0:{}", config.port));
    Server::new(listener)
        .run_with_graceful_shutdown(app, shutdown_signal(), Some(SHUTDOWN_GRACE))
        .await
        .expect("server failed");
    tracing::info!("in-flight requests drained; dark-basemap-xyz stopped");
}

/// Docker and Swarm stop containers with SIGTERM. Listening only for Ctrl+C leaves this process,
/// which is PID 1 in the image, alive until Docker's grace period expires and it is SIGKILLed in
/// the middle of any active tile renders.
async fn shutdown_signal() {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                .expect("failed to install SIGTERM handler");

        tokio::select! {
            result = tokio::signal::ctrl_c() => {
                result.expect("failed to listen for Ctrl+C");
                tracing::info!(
                    signal = "SIGINT",
                    "shutdown signal received; draining in-flight requests"
                );
            }
            _ = terminate.recv() => {
                tracing::info!(
                    signal = "SIGTERM",
                    "shutdown signal received; draining in-flight requests"
                );
            }
        }
    }

    #[cfg(not(unix))]
    {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to listen for Ctrl+C");
        tracing::info!(
            signal = "SIGINT",
            "shutdown signal received; draining in-flight requests"
        );
    }
}

fn main() {
    dotenv::dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info,poem=warn".into()),
        )
        .init();

    tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("failed to build the tokio runtime")
        .block_on(run());
}

#[cfg(test)]
mod route_tests {
    use super::*;
    use crate::tiles::TileCoord;
    use poem::http::StatusCode;
    use poem::test::TestClient;
    use std::time::Duration;

    /// A state whose renderer points at a closed port. Anything that reaches the renderer fails
    /// fast, which is exactly what the routing and validation tests want: a request that gets a
    /// 503 (renderer down) provably went past validation, and a 404 provably did not.
    fn test_state() -> AppState {
        test_state_with_renderer("http://127.0.0.1:1".to_string(), config::LayerRoutes::default())
    }

    fn test_state_with_routes(layer_routes: config::LayerRoutes) -> AppState {
        test_state_with_renderer("http://127.0.0.1:1".to_string(), layer_routes)
    }

    fn test_state_with_renderer(renderer_url: String, layer_routes: config::LayerRoutes) -> AppState {
        let dir = std::env::temp_dir().join(format!(
            "dark-basemap-routes-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();

        let config = Arc::new(Config {
            port: 0,
            renderer_url,
            cache_dir: dir.clone(),
            tile_ttl: Duration::from_secs(120 * 86_400),
            sweep_interval: Duration::from_secs(3600),
            client_max_age: 604_800,
            min_zoom: 0,
            max_zoom: 20,
            render_concurrency: 2,
            render_queue_timeout: Duration::from_millis(200),
            render_timeout: Duration::from_millis(500),
            render_failure_threshold: 2,
            render_circuit_open: Duration::from_secs(30),
            memory_capacity: 64,
            memory_ttl: Duration::from_secs(60),
            negative_ttl: Duration::from_secs(60),
            project_version: None,
            layer_routes,
        });
        let metrics = Arc::new(Metrics::default());
        let cache = Arc::new(TileCache::new(dir, config.tile_ttl));
        let renderer = Arc::new(Renderer::new(
            config.renderer_url.clone(),
            config.render_concurrency,
            config.render_queue_timeout,
            config.render_timeout,
            config.render_failure_threshold,
            config.render_circuit_open,
            metrics.clone(),
            config.layer_routes.clone(),
        ));
        AppState::new(config, cache, renderer, metrics)
    }

    #[tokio::test]
    async fn unservable_coordinates_are_404_and_never_reach_the_renderer() {
        let state = test_state();
        let cli = TestClient::new(build_app(state.clone()));

        for path in [
            "/tiles/countries/21/0/0.png",    // past MAX_ZOOM
            "/tiles/countries/0/1/0.png",     // z0 has exactly one tile
            "/tiles/countries/1/0/2.png",     // y off the edge at z1
            "/tiles/countries/abc/0/0.png",   // non-numeric
            "/tiles/countries/9/271/171.jpg", // wrong extension -> y unparseable
            "/tiles/bad@layer/9/271/171.png", // unsafe layer segment
        ] {
            let resp = cli.get(path).send().await;
            resp.assert_status(StatusCode::NOT_FOUND);
        }

        assert_eq!(
            Metrics::get(&state.metrics.renders) + Metrics::get(&state.metrics.render_errors),
            0,
            "rejected coordinates must be filtered before any render is attempted"
        );
        assert_eq!(Metrics::get(&state.metrics.rejected), 6);
    }

    #[tokio::test]
    async fn a_valid_coordinate_with_a_dead_renderer_is_503_and_uncacheable() {
        let cli = TestClient::new(build_app(test_state()));
        let resp = cli.get("/tiles/countries/9/271/171.png").send().await;
        resp.assert_status(StatusCode::SERVICE_UNAVAILABLE);
        resp.assert_header("cache-control", "no-store");
        resp.assert_header("retry-after", "30");
    }

    /// The renderer-down class must not be negative-cached: while the renderer is gone, every
    /// miss answers 503, instead of the first request tripping the cache and the rest getting a
    /// mixed 502. Retries stay cheap because the circuit breaker fails them fast.
    #[tokio::test]
    async fn a_dead_renderer_keeps_answering_503_instead_of_negative_caching() {
        let state = test_state();
        let cli = TestClient::new(build_app(state.clone()));

        for _ in 0..2 {
            cli.get("/tiles/countries/9/271/171.png")
                .send()
                .await
                .assert_status(StatusCode::SERVICE_UNAVAILABLE);
        }

        assert_eq!(
            Metrics::get(&state.metrics.render_errors),
            2,
            "down-class errors must not be negative-cached"
        );
        assert_eq!(Metrics::get(&state.metrics.renderer_unavailable), 2);
    }

    /// A fake QGIS Server that answers every request with a ServiceException document.
    #[poem::handler]
    async fn fake_qgis_service_exception() -> poem::Response {
        poem::Response::builder()
            .status(poem::http::StatusCode::OK)
            .header("content-type", "text/xml")
            .body(r#"<ServiceExceptionReport><ServiceException>Layer "nope" not found</ServiceException></ServiceExceptionReport>"#)
    }

    async fn fake_qgis() -> (String, tokio::task::JoinHandle<()>) {
        use poem::listener::{Acceptor, Listener};
        let listener = poem::listener::TcpListener::bind("127.0.0.1:0".to_string());
        let acceptor = listener.into_acceptor().await.unwrap();
        let local_addrs = acceptor.local_addr();
        let addr = local_addrs[0].as_socket_addr().unwrap();
        let app = poem::Route::new().at("/ows/", poem::get(fake_qgis_service_exception));
        let handle = tokio::spawn(async move {
            let _ = poem::Server::new_with_acceptor(acceptor)
                .run_with_graceful_shutdown(app, std::future::pending::<()>(), None)
                .await;
        });
        (format!("http://{addr}"), handle)
    }

    /// Guards the split between the two failure classes: a tile the renderer *answered* badly is
    /// negative-cached and stays a 502, while a renderer that is simply gone is a 503.
    #[tokio::test]
    async fn a_service_exception_is_negative_cached_and_stays_a_502() {
        let (renderer_url, _server) = fake_qgis().await;
        let state = test_state_with_renderer(renderer_url, config::LayerRoutes::default());
        let cli = TestClient::new(build_app(state.clone()));

        for _ in 0..2 {
            cli.get("/tiles/countries/9/271/171.png")
                .send()
                .await
                .assert_status(StatusCode::BAD_GATEWAY);
        }

        assert_eq!(
            Metrics::get(&state.metrics.render_errors),
            1,
            "a broken tile must be negative-cached"
        );
        assert_eq!(
            Metrics::get(&state.metrics.renderer_unavailable),
            0,
            "an answered request is not renderer-down"
        );
        assert_eq!(
            Metrics::get(&state.metrics.circuit_opens),
            0,
            "an answered request must not trip the breaker"
        );
    }

    #[tokio::test]
    async fn a_cached_tile_is_served_without_touching_the_renderer() {
        let state = test_state();
        let coord = TileCoord::new("countries", 9, 271, 171);
        let payload = bytes::Bytes::from_static(b"\x89PNG\r\n\x1a\ncached");
        state.cache.store(&coord, &payload).await.unwrap();

        let cli = TestClient::new(build_app(state.clone()));
        let resp = cli.get("/tiles/countries/9/271/171.png").send().await;
        resp.assert_status_is_ok();
        resp.assert_header("content-type", "image/png");
        resp.assert_header("access-control-allow-origin", "*");
        resp.assert_header("cache-control", "public, max-age=604800");
        resp.assert_bytes(payload).await;

        assert_eq!(Metrics::get(&state.metrics.render_errors), 0, "the dead renderer was never called");
        assert_eq!(Metrics::get(&state.metrics.disk_hits), 1);
    }

    /// Two layers at the same z/x/y are different tiles: neither the disk cache nor the
    /// single-flight key may collapse them.
    #[tokio::test]
    async fn different_layers_at_the_same_coordinate_are_independent() {
        let state = test_state();
        let countries = bytes::Bytes::from_static(b"\x89PNG\r\n\x1a\ncountries");
        let roads = bytes::Bytes::from_static(b"\x89PNG\r\n\x1a\nroads");
        state.cache.store(&TileCoord::new("countries", 9, 271, 171), &countries).await.unwrap();
        state.cache.store(&TileCoord::new("roads", 9, 271, 171), &roads).await.unwrap();

        let cli = TestClient::new(build_app(state));
        cli.get("/tiles/countries/9/271/171.png").send().await.assert_bytes(countries).await;
        cli.get("/tiles/roads/9/271/171.png").send().await.assert_bytes(roads).await;
    }

    /// Routing must change only where the pixels come from, never where they are stored. The disk
    /// layout is a contract with nginx's `try_files` (which knows nothing about routes), so a
    /// routed tile has to remain reachable under the name in the URL.
    #[tokio::test]
    async fn a_routed_layer_still_caches_under_the_requested_name() {
        let state = test_state_with_routes("countries@0-5=simple-countries".parse().unwrap());
        let payload = bytes::Bytes::from_static(b"\x89PNG\r\n\x1a\nrouted");
        // Stored under the *public* name, which is what cache_path() and nginx both use.
        state.cache.store(&TileCoord::new("countries", 3, 4, 3), &payload).await.unwrap();

        let cli = TestClient::new(build_app(state.clone()));
        let resp = cli.get("/tiles/countries/3/4/3.png").send().await;
        resp.assert_status_is_ok();
        resp.assert_bytes(payload).await;

        assert_eq!(Metrics::get(&state.metrics.disk_hits), 1);
        assert_eq!(
            Metrics::get(&state.metrics.render_errors),
            0,
            "the tile was cached under the requested name, so the dead renderer was never called"
        );
    }

    #[tokio::test]
    async fn the_extension_is_optional() {
        let state = test_state();
        let coord = TileCoord::new("countries", 4, 3, 2);
        state.cache.store(&coord, &bytes::Bytes::from_static(b"png")).await.unwrap();

        let cli = TestClient::new(build_app(state));
        cli.get("/tiles/countries/4/3/2").send().await.assert_status_is_ok();
    }

    /// The property the whole design rests on: many simultaneous callers for one cold tile must
    /// produce exactly one upstream attempt. Uses the dead renderer, so "one attempt" is counted
    /// as one render *error* rather than one success.
    #[tokio::test]
    async fn concurrent_requests_for_one_tile_collapse_into_a_single_render() {
        let state = test_state();
        let app = Arc::new(build_app(state.clone()));

        let mut handles = Vec::new();
        for _ in 0..50 {
            let app = app.clone();
            handles.push(tokio::spawn(async move {
                TestClient::new(app.as_ref()).get("/tiles/countries/12/2048/1362.png").send().await;
            }));
        }
        for handle in handles {
            handle.await.unwrap();
        }

        assert_eq!(
            Metrics::get(&state.metrics.render_errors),
            1,
            "50 concurrent requests must dedupe to one upstream attempt; got {}",
            Metrics::get(&state.metrics.render_errors)
        );
        assert_eq!(Metrics::get(&state.metrics.requests), 50);
    }

    #[tokio::test]
    async fn a_failed_tile_is_negative_cached_rather_than_retried() {
        let state = test_state();
        let coord = TileCoord::new("countries", 6, 20, 20);
        state.mark_failed(coord).await;

        let cli = TestClient::new(build_app(state.clone()));
        cli.get("/tiles/countries/6/20/20.png").send().await.assert_status(StatusCode::BAD_GATEWAY);

        assert_eq!(Metrics::get(&state.metrics.negative_hits), 1);
        assert_eq!(Metrics::get(&state.metrics.render_errors), 0, "must not re-attempt a known-bad tile");
    }

    /// Guards the split between the two health endpoints: the liveness probe must not reach the
    /// renderer, or the container healthcheck permanently occupies one of its FCGI slots.
    #[tokio::test]
    async fn liveness_is_cheap_and_never_touches_the_renderer() {
        let state = test_state();
        let cli = TestClient::new(build_app(state.clone()));

        for _ in 0..5 {
            cli.get("/health/live").send().await.assert_status_is_ok();
        }
        assert_eq!(
            Metrics::get(&state.metrics.render_errors),
            0,
            "the liveness probe must not attempt an upstream request"
        );
    }

    #[tokio::test]
    async fn health_stays_200_with_the_renderer_down() {
        let cli = TestClient::new(build_app(test_state()));
        let resp = cli.get("/health").send().await;
        resp.assert_status_is_ok();

        let body: serde_json::Value = resp.json().await.value().deserialize();
        assert_eq!(body["status"], "degraded");
        assert_eq!(body["renderer"]["status"], "down");
        assert_eq!(body["zoom"]["max"], 20);
    }

    #[tokio::test]
    async fn stats_reports_the_counters() {
        let cli = TestClient::new(build_app(test_state()));
        cli.get("/tiles/countries/99/0/0.png").send().await;

        let resp = cli.get("/stats").send().await;
        resp.assert_status_is_ok();
        let body: serde_json::Value = resp.json().await.value().deserialize();
        assert_eq!(body["rejected"], 1);
        assert_eq!(body["renders"], 0);
        assert!(body["avg_render_ms"].is_null(), "no renders yet, so there is no average");
    }
}
