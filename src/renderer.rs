use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use bytes::Bytes;
use tokio::sync::Semaphore;

use crate::config::LayerRoutes;
use crate::metatile::MetaCoord;
use crate::metrics::Metrics;
use crate::tiles::TileCoord;

/// `Clone` because the single-flight caches in `state.rs` hand a failure back to every waiter
/// behind an `Arc`, and the waiter needs an owned error to return. Every variant is a `Duration`,
/// a `String` or a number, so this costs nothing worth avoiding.
#[derive(Debug, Clone, thiserror::Error)]
pub enum RenderError {
    /// Every render slot was busy for longer than the caller is willing to wait. Distinct from a
    /// renderer failure: the tile is probably fine, we just refuse to queue further.
    #[error("timed out waiting {0:?} for a render slot")]
    QueueTimeout(Duration),
    #[error("renderer did not respond within {0:?}")]
    Timeout(Duration),
    #[error("could not reach the renderer: {0}")]
    Transport(String),
    #[error("renderer returned HTTP {0}")]
    Status(u16),
    /// QGIS Server answers 200 with an XML <ServiceException> body when a request is malformed or
    /// the layer is not published for WMTS. Caching that as a tile would poison the basemap for
    /// 120 days, so it is an error here, not a payload.
    #[error("QGIS service exception: {0}")]
    ServiceException(String),
    #[error("renderer returned {0:?}, expected image/png")]
    UnexpectedContentType(String),
    #[error("renderer returned an empty body")]
    EmptyBody,
    /// The circuit breaker is open: recent renders failed at the transport level, so this attempt
    /// failed fast without touching the network. The renderer may well be back; the breaker just
    /// refuses to keep paying the full request timeout per tile until it is.
    #[error("renderer is unreachable: circuit open")]
    RendererDown,
}

impl RenderError {
    /// Whether this failure means the renderer itself is unreachable, as opposed to the renderer
    /// answering with a bad tile. Only the down class may trip the circuit breaker and must skip
    /// the negative cache: a dead renderer should fail every miss fast, not once a minute.
    pub fn is_renderer_down(&self) -> bool {
        matches!(
            self,
            RenderError::Transport(_) | RenderError::Timeout(_) | RenderError::RendererDown
        )
    }
}

/// A tiny circuit breaker. Only transport-level failures count; any response at all proves the
/// renderer is alive and resets everything.
struct Breaker {
    threshold: u32,
    open_duration: Duration,
    failures: AtomicU32,
    open_until: Mutex<Option<Instant>>,
}

impl Breaker {
    fn new(threshold: u32, open_duration: Duration) -> Self {
        Self {
            threshold: threshold.max(1),
            open_duration,
            failures: AtomicU32::new(0),
            open_until: Mutex::new(None),
        }
    }

    /// Whether a render attempt is allowed right now.
    fn allows(&self) -> bool {
        match *self.open_until.lock().expect("breaker mutex poisoned") {
            Some(until) if Instant::now() < until => false,
            _ => true,
        }
    }

    /// Records one transport failure; returns true when this failure opens the circuit.
    fn record_failure(&self) -> bool {
        let failures = self.failures.fetch_add(1, Ordering::Relaxed) + 1;
        if failures >= self.threshold {
            let mut open_until = self.open_until.lock().expect("breaker mutex poisoned");
            *open_until = Some(Instant::now() + self.open_duration);
            true
        } else {
            false
        }
    }

    /// Any response from the renderer proves it is alive; clear all state.
    fn record_success(&self) {
        self.failures.store(0, Ordering::Relaxed);
        *self.open_until.lock().expect("breaker mutex poisoned") = None;
    }
}

pub struct Renderer {
    client: reqwest::Client,
    base_url: String,
    permits: Arc<Semaphore>,
    queue_timeout: Duration,
    metrics: Arc<Metrics>,
    /// Resolved per request, not per coordinate: the routing table only ever affects the outbound
    /// WMTS `LAYER`, never the cache key, so it lives here rather than on `TileCoord`.
    routes: LayerRoutes,
    /// Stops a dead renderer from costing one full `request_timeout` per tile: after
    /// `failure_threshold` transport failures every miss fails fast until the circuit closes.
    breaker: Breaker,
}

impl Renderer {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        base_url: String,
        concurrency: usize,
        queue_timeout: Duration,
        request_timeout: Duration,
        failure_threshold: u32,
        circuit_open: Duration,
        metrics: Arc<Metrics>,
        routes: LayerRoutes,
    ) -> Self {
        let client = reqwest::Client::builder()
            .timeout(request_timeout)
            // The renderer is one host we hit constantly; keeping connections warm avoids a
            // handshake per tile on the miss path.
            .pool_max_idle_per_host(concurrency.max(1) * 2)
            .pool_idle_timeout(Duration::from_secs(90))
            .user_agent(concat!("dark-basemap-xyz/", env!("CARGO_PKG_VERSION")))
            .build()
            .expect("reqwest client should build with a static configuration");

        Self {
            client,
            base_url,
            permits: Arc::new(Semaphore::new(concurrency)),
            queue_timeout,
            metrics,
            routes,
            breaker: Breaker::new(failure_threshold, circuit_open),
        }
    }

    fn ows_url(&self) -> String {
        format!("{}/ows/", self.base_url)
    }

    pub fn available_permits(&self) -> usize {
        self.permits.available_permits()
    }

    /// Renders one tile.
    ///
    /// The semaphore is the real throttle: QGIS Server runs a small fixed pool of FCGI processes,
    /// and letting more requests than that through just moves the queue somewhere we cannot
    /// observe or time out.
    pub async fn fetch(&self, coord: &TileCoord) -> Result<Bytes, RenderError> {
        let upstream = self.routes.resolve(&coord.layer, coord.z).to_string();
        let started = Instant::now();
        let body = self.request(&coord.to_string(), coord.wmts_query(&upstream)).await?;

        Metrics::bump(&self.metrics.renders);
        Metrics::add(&self.metrics.render_millis_total, started.elapsed().as_millis() as u64);
        tracing::debug!(%coord, upstream, ms = started.elapsed().as_millis(), bytes = body.len(), "rendered tile");
        Ok(body)
    }

    /// Renders a whole `n x n` block of tiles as one image, buffer included.
    ///
    /// This is a WMS `GetMap` rather than a WMTS `GetTile` because only `GetMap` takes an arbitrary
    /// extent. Everything else — the semaphore, the breaker, the body checks — is identical to
    /// `fetch`, and deliberately so: a metatile is just a bigger render, and it must fail in the
    /// same shapes so `state.rs` can treat both the same way.
    pub async fn fetch_metatile(&self, meta: &MetaCoord, buffer_px: u32) -> Result<Bytes, RenderError> {
        let upstream = self.routes.resolve(&meta.layer, meta.z).to_string();
        let started = Instant::now();
        let body = self.request(&meta.to_string(), meta.wms_query(&upstream, buffer_px)).await?;

        Metrics::bump(&self.metrics.renders);
        Metrics::bump(&self.metrics.metatile_renders);
        Metrics::add(&self.metrics.render_millis_total, started.elapsed().as_millis() as u64);
        tracing::debug!(
            %meta,
            upstream,
            px = meta.size_px(buffer_px),
            ms = started.elapsed().as_millis(),
            bytes = body.len(),
            "rendered metatile"
        );
        Ok(body)
    }

    /// The part `fetch` and `fetch_metatile` share: queue for a slot, make the request, and judge
    /// the answer. `what` names the unit of work for logs only.
    async fn request(
        &self,
        what: &str,
        query: Vec<(&'static str, String)>,
    ) -> Result<Bytes, RenderError> {
        // Fail fast while the circuit is open: the caller turns this into a 503, and the state
        // layer deliberately does not negative-cache it, so every miss keeps answering promptly.
        if !self.breaker.allows() {
            return Err(RenderError::RendererDown);
        }

        let _permit = match tokio::time::timeout(self.queue_timeout, self.permits.acquire()).await {
            Ok(Ok(permit)) => permit,
            Ok(Err(_)) => unreachable!("the semaphore is never closed"),
            Err(_) => {
                Metrics::bump(&self.metrics.queue_timeouts);
                return Err(RenderError::QueueTimeout(self.queue_timeout));
            }
        };

        let started = Instant::now();
        let response = self
            .client
            .get(self.ows_url())
            .query(&query)
            .send()
            .await
            .map_err(|err| {
                let failure = if err.is_timeout() {
                    RenderError::Timeout(started.elapsed())
                } else {
                    RenderError::Transport(err.to_string())
                };
                if self.breaker.record_failure() {
                    Metrics::bump(&self.metrics.circuit_opens);
                    tracing::warn!(
                        target = what,
                        failures = self.breaker.threshold,
                        "renderer transport failures reached threshold; circuit open"
                    );
                }
                failure
            })?;

        // The renderer answered something — a tile, an HTTP error, a ServiceException, anything.
        // It is alive, so the breaker resets regardless of how the body is judged below.
        self.breaker.record_success();

        let status = response.status();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_ascii_lowercase();

        if !status.is_success() {
            // A 404 here is worth calling out: the renderer image's own nginx answers an overrun
            // FastCGI read that way (its error_page target does not exist), so this usually means
            // the render outlived `fastcgi_read_timeout` rather than that anything is malformed.
            if status.as_u16() == 404 {
                tracing::warn!(
                    target = what,
                    "renderer returned 404; if this is a slow render, raise fastcgi_read_timeout in the renderer image"
                );
            }
            return Err(RenderError::Status(status.as_u16()));
        }

        let body = response
            .bytes()
            .await
            .map_err(|err| RenderError::Transport(err.to_string()))?;

        // Check the body, not just the header: QGIS labels exception documents text/xml, but a
        // misconfigured proxy in between can relabel anything.
        if content_type.contains("xml") || body.starts_with(b"<") {
            return Err(RenderError::ServiceException(summarise_exception(&body)));
        }
        if !content_type.starts_with("image/png") {
            return Err(RenderError::UnexpectedContentType(content_type));
        }
        if body.is_empty() {
            return Err(RenderError::EmptyBody);
        }
        Ok(body)
    }

    /// Cheap liveness probe for `/health`. Uses GetCapabilities rather than a tile so it stays
    /// meaningful even when the project publishes no layer at all.
    pub async fn probe(&self) -> Result<(), RenderError> {
        // Respect the open circuit: the API's /health reports the renderer as down the moment the
        // breaker trips, without paying a network round trip per poll.
        if !self.breaker.allows() {
            return Err(RenderError::RendererDown);
        }

        let response = self
            .client
            .get(self.ows_url())
            .query(&[("SERVICE", "WMTS"), ("VERSION", "1.0.0"), ("REQUEST", "GetCapabilities")])
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|err| {
                let failure = RenderError::Transport(err.to_string());
                if self.breaker.record_failure() {
                    Metrics::bump(&self.metrics.circuit_opens);
                }
                failure
            })?;

        self.breaker.record_success();

        if response.status().is_success() {
            Ok(())
        } else {
            Err(RenderError::Status(response.status().as_u16()))
        }
    }
}

/// Pulls the human-readable part out of a QGIS ServiceException document, falling back to a
/// truncated dump. Kept short because this ends up in log lines, one per failing tile.
fn summarise_exception(body: &[u8]) -> String {
    let text = String::from_utf8_lossy(body);
    let text: &str = &text;

    // Anchored on the closing tag and scanned backwards, because a forward search for
    // "<ServiceException" matches the "<ServiceExceptionReport>" wrapper QGIS puts around it.
    let inner = text.find("</ServiceException>").and_then(|end| {
        let head = &text[..end];
        let open = head.rfind("<ServiceException")?;
        let content = open + head[open..].find('>')? + 1;
        Some(&text[content..end])
    });

    let summary = inner.unwrap_or(text).trim();
    let summary: String = summary.split_whitespace().collect::<Vec<_>>().join(" ");
    if summary.len() > 300 {
        format!("{}…", &summary[..300])
    } else {
        summary
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Shared builder so a signature change in `Renderer::new` touches one line here instead of
    /// every test.
    fn renderer(base_url: &str, concurrency: usize, metrics: Arc<Metrics>) -> Renderer {
        Renderer::new(
            base_url.to_string(),
            concurrency,
            Duration::from_secs(1),
            Duration::from_secs(2),
            2,
            Duration::from_secs(60),
            metrics,
            LayerRoutes::default(),
        )
    }

    #[test]
    fn extracts_the_message_from_a_service_exception() {
        let body = br#"<?xml version="1.0"?>
<ServiceExceptionReport>
  <ServiceException code="LayerNotDefined">Layer "darkbasemap" not found</ServiceException>
</ServiceExceptionReport>"#;
        assert_eq!(summarise_exception(body), r#"Layer "darkbasemap" not found"#);
    }

    #[test]
    fn falls_back_to_the_raw_body_when_there_is_no_exception_element() {
        assert_eq!(summarise_exception(b"<html>  nope\n  </html>"), "<html> nope </html>");
    }

    #[test]
    fn truncates_a_pathologically_long_exception() {
        let long = format!("<ServiceException>{}</ServiceException>", "x".repeat(5000));
        let summary = summarise_exception(long.as_bytes());
        assert!(summary.len() <= 304, "got {} chars", summary.len());
        assert!(summary.ends_with('…'));
    }

    #[tokio::test]
    async fn queue_timeout_fires_when_every_permit_is_held() {
        // A short queue timeout so the test does not wait the default second.
        let renderer = Renderer::new(
            "http://127.0.0.1:1".to_string(),
            1,
            Duration::from_millis(50),
            Duration::from_secs(1),
            2,
            Duration::from_secs(60),
            Arc::new(Metrics::default()),
            LayerRoutes::default(),
        );
        let held = renderer.permits.clone().acquire_owned().await.unwrap();
        assert_eq!(renderer.available_permits(), 0);

        let err = renderer.fetch(&TileCoord::new("layer", 0, 0, 0)).await.unwrap_err();
        assert!(matches!(err, RenderError::QueueTimeout(_)), "got {err:?}");
        drop(held);
    }

    #[tokio::test]
    async fn an_unreachable_renderer_is_a_transport_error_not_a_panic() {
        let renderer = renderer("http://127.0.0.1:1", 2, Arc::new(Metrics::default()));
        assert!(renderer.fetch(&TileCoord::new("layer", 0, 0, 0)).await.is_err());
        assert!(renderer.probe().await.is_err());
    }

    #[test]
    fn breaker_opens_on_threshold_and_resets_on_any_response() {
        let breaker = Breaker::new(2, Duration::from_secs(60));
        assert!(breaker.allows());
        assert!(!breaker.record_failure());
        assert!(breaker.allows(), "one failure below threshold must not open the circuit");
        assert!(breaker.record_failure());
        assert!(!breaker.allows(), "two failures must open the circuit");
        breaker.record_success();
        assert!(breaker.allows(), "a response must reset the breaker");
    }

    #[tokio::test]
    async fn transport_failures_open_the_circuit_and_fail_fast() {
        let metrics = Arc::new(Metrics::default());
        let renderer = renderer("http://127.0.0.1:1", 2, metrics.clone());

        // Two transport failures trip the breaker...
        for _ in 0..2 {
            assert!(renderer.fetch(&TileCoord::new("layer", 0, 0, 0)).await.is_err());
        }
        assert_eq!(Metrics::get(&metrics.circuit_opens), 1);

        // ...and further attempts fail fast with RendererDown, without touching the network.
        let started = Instant::now();
        let err = renderer.fetch(&TileCoord::new("layer", 0, 0, 1)).await.unwrap_err();
        assert!(matches!(err, RenderError::RendererDown), "got {err:?}");
        assert!(
            started.elapsed() < Duration::from_millis(100),
            "circuit-open fetch must fail fast, took {:?}",
            started.elapsed()
        );
        assert!(matches!(renderer.probe().await.unwrap_err(), RenderError::RendererDown));
    }

    /// An HTTP answer — even a failure status — is proof of life: it must reset the breaker, never
    /// trip it. A tiny HTTP server answers every request with 500.
    #[tokio::test]
    async fn an_http_response_resets_the_breaker_instead_of_tripping_it() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            loop {
                let (mut socket, _) = listener.accept().await.unwrap();
                tokio::spawn(async move {
                    use tokio::io::{AsyncReadExt, AsyncWriteExt};
                    let mut buf = [0u8; 1024];
                    let _ = socket.read(&mut buf).await;
                    socket
                        .write_all(
                            b"HTTP/1.1 500 Internal Server Error\r\ncontent-length: 0\r\nconnection: close\r\n\r\n",
                        )
                        .await
                        .ok();
                });
            }
        });

        let metrics = Arc::new(Metrics::default());
        // Threshold 1: a single transport failure would open the circuit. The 500 answers instead.
        let renderer = Renderer::new(
            format!("http://{addr}"),
            2,
            Duration::from_secs(1),
            Duration::from_secs(2),
            1,
            Duration::from_secs(60),
            metrics.clone(),
            LayerRoutes::default(),
        );

        for _ in 0..3 {
            let err = renderer.fetch(&TileCoord::new("layer", 0, 0, 0)).await.unwrap_err();
            assert!(matches!(err, RenderError::Status(500)), "got {err:?}");
        }
        assert_eq!(Metrics::get(&metrics.circuit_opens), 0, "answers must never trip the breaker");
    }

    #[test]
    fn ows_url_survives_a_trailing_slash_in_config() {
        let make = |base: &str| {
            Renderer::new(
                base.trim_end_matches('/').to_string(),
                1,
                Duration::from_secs(1),
                Duration::from_secs(1),
                2,
                Duration::from_secs(30),
                Arc::new(Metrics::default()),
                LayerRoutes::default(),
            )
            .ows_url()
        };
        assert_eq!(make("http://renderer"), "http://renderer/ows/");
        assert_eq!(make("http://renderer/"), "http://renderer/ows/");
    }
}
