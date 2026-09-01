use std::sync::Arc;
use std::time::{Duration, Instant};

use bytes::Bytes;
use tokio::sync::Semaphore;

use crate::config::LayerRoutes;
use crate::metrics::Metrics;
use crate::tiles::TileCoord;

#[derive(Debug, thiserror::Error)]
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
}

impl Renderer {
    pub fn new(
        base_url: String,
        concurrency: usize,
        queue_timeout: Duration,
        request_timeout: Duration,
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
        let _permit = match tokio::time::timeout(self.queue_timeout, self.permits.acquire()).await {
            Ok(Ok(permit)) => permit,
            Ok(Err(_)) => unreachable!("the semaphore is never closed"),
            Err(_) => {
                Metrics::bump(&self.metrics.queue_timeouts);
                return Err(RenderError::QueueTimeout(self.queue_timeout));
            }
        };

        let upstream = self.routes.resolve(&coord.layer, coord.z);
        let started = Instant::now();
        let response = self
            .client
            .get(self.ows_url())
            .query(&coord.wmts_query(upstream))
            .send()
            .await
            .map_err(|err| {
                if err.is_timeout() {
                    RenderError::Timeout(started.elapsed())
                } else {
                    RenderError::Transport(err.to_string())
                }
            })?;

        let status = response.status();
        let content_type = response
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or_default()
            .to_ascii_lowercase();

        if !status.is_success() {
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

        Metrics::bump(&self.metrics.renders);
        Metrics::add(&self.metrics.render_millis_total, started.elapsed().as_millis() as u64);
        tracing::debug!(%coord, upstream, ms = started.elapsed().as_millis(), bytes = body.len(), "rendered tile");
        Ok(body)
    }

    /// Cheap liveness probe for `/health`. Uses GetCapabilities rather than a tile so it stays
    /// meaningful even when the project publishes no layer at all.
    pub async fn probe(&self) -> Result<(), RenderError> {
        let response = self
            .client
            .get(self.ows_url())
            .query(&[("SERVICE", "WMTS"), ("VERSION", "1.0.0"), ("REQUEST", "GetCapabilities")])
            .timeout(Duration::from_secs(5))
            .send()
            .await
            .map_err(|err| RenderError::Transport(err.to_string()))?;

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
        let renderer = Renderer::new(
            "http://127.0.0.1:1".to_string(),
            1,
            Duration::from_millis(50),
            Duration::from_secs(1),
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
        let renderer = Renderer::new(
            // Port 1 is reserved and refuses connections immediately.
            "http://127.0.0.1:1".to_string(),
            2,
            Duration::from_secs(1),
            Duration::from_secs(2),
            Arc::new(Metrics::default()),
            LayerRoutes::default(),
        );
        assert!(renderer.fetch(&TileCoord::new("layer", 0, 0, 0)).await.is_err());
        assert!(renderer.probe().await.is_err());
    }

    #[test]
    fn ows_url_survives_a_trailing_slash_in_config() {
        let make = |base: &str| {
            Renderer::new(
                base.trim_end_matches('/').to_string(),
                1,
                Duration::from_secs(1),
                Duration::from_secs(1),
                Arc::new(Metrics::default()),
                LayerRoutes::default(),
            )
            .ows_url()
        };
        assert_eq!(make("http://renderer"), "http://renderer/ows/");
        assert_eq!(make("http://renderer/"), "http://renderer/ows/");
    }
}
