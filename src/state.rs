use std::sync::Arc;

use bytes::Bytes;
use moka::future::Cache;

use crate::cache::TileCache;
use crate::config::Config;
use crate::metrics::Metrics;
use crate::renderer::{RenderError, Renderer};
use crate::tiles::TileCoord;

#[derive(Debug, thiserror::Error)]
pub enum TileFailure {
    #[error(transparent)]
    Render(#[from] RenderError),
    /// This tile failed recently and the negative cache is still holding the door shut. Reported
    /// separately so a burst against a broken tile shows up as one render error, not thousands.
    #[error("tile failed recently; not retrying yet")]
    RecentlyFailed,
}

#[derive(Clone)]
pub struct AppState {
    pub config: Arc<Config>,
    pub cache: Arc<TileCache>,
    pub renderer: Arc<Renderer>,
    pub metrics: Arc<Metrics>,
    /// Hot tier *and* single-flight gate. `try_get_with` runs the init future exactly once per key
    /// across all concurrent callers, which is what stops a cold popular tile from spawning one
    /// QGIS render per waiting request.
    memory: Cache<TileCoord, Bytes>,
    negative: Cache<TileCoord, ()>,
}

impl AppState {
    pub fn new(config: Arc<Config>, cache: Arc<TileCache>, renderer: Arc<Renderer>, metrics: Arc<Metrics>) -> Self {
        let memory = Cache::builder()
            .max_capacity(config.memory_capacity)
            .time_to_live(config.memory_ttl)
            .build();
        let negative = Cache::builder()
            .max_capacity(config.memory_capacity)
            .time_to_live(config.negative_ttl)
            .build();
        Self { config, cache, renderer, metrics, memory, negative }
    }

    pub fn memory_entry_count(&self) -> u64 {
        self.memory.entry_count()
    }

    /// Resolve a tile: memory, then disk, then render. At most one render happens per coordinate
    /// no matter how many callers arrive at once.
    pub async fn get_or_render(&self, coord: TileCoord) -> Result<Bytes, Arc<TileFailure>> {
        if let Some(bytes) = self.memory.get(&coord).await {
            Metrics::bump(&self.metrics.memory_hits);
            return Ok(bytes);
        }

        let disk = self.cache.clone();
        let renderer = self.renderer.clone();
        let metrics = self.metrics.clone();
        let negative = self.negative.clone();
        // `TileCoord` now carries a `String` layer, so it is no longer `Copy`: clone one instance
        // for the single-flight key and move the other into the closure below.
        let key = coord.clone();

        self.memory
            .try_get_with(key, async move {
                if negative.get(&coord).await.is_some() {
                    Metrics::bump(&metrics.negative_hits);
                    return Err(TileFailure::RecentlyFailed);
                }

                // Re-check the disk inside the single-flight section. nginx normally serves hits
                // before we ever see them, but this keeps the endpoint correct when it is called
                // directly, and catches the tile another worker just wrote.
                if let Some(bytes) = disk.read_fresh(&coord).await {
                    Metrics::bump(&metrics.disk_hits);
                    return Ok(bytes);
                }

                let bytes = match renderer.fetch(&coord).await {
                    Ok(bytes) => bytes,
                    Err(err) => {
                        Metrics::bump(&metrics.render_errors);
                        tracing::warn!(%coord, error = %err, "render failed");
                        // Only per-tile failures are negative-cached. A renderer-down failure must
                        // not hold the door shut: while the service is unreachable every miss keeps
                        // answering 503, and retries are cheap because the circuit breaker fails
                        // them fast. Caching a dead renderer's errors would turn the 503 contract
                        // into a mix of 502s depending on which request tripped the cache first.
                        if !err.is_renderer_down() {
                            negative.insert(coord, ()).await;
                        }
                        return Err(TileFailure::Render(err));
                    }
                };

                // A failed write is not a failed request: the client gets its tile, we just did
                // not manage to keep it. Losing the cache is a capacity problem, not a 500.
                if let Err(err) = disk.store(&coord, &bytes).await {
                    tracing::error!(%coord, error = %err, "rendered tile but could not cache it");
                }

                Ok(bytes)
            })
            .await
    }

    /// Test seam: lets a test verify the negative cache without waiting for a real failure.
    #[cfg(test)]
    pub async fn mark_failed(&self, coord: TileCoord) {
        self.negative.insert(coord, ()).await;
    }
}

/// Runs at boot and then on a fixed interval, enforcing the TTL that nginx cannot enforce on its
/// own. A tile can therefore survive up to `tile_ttl + sweep_interval`, which is fine for a
/// basemap and is documented in the README.
pub fn spawn_janitor(state: AppState) {
    tokio::spawn(async move {
        loop {
            match state.cache.sweep().await {
                Ok(report) => {
                    Metrics::bump(&state.metrics.sweeps);
                    state
                        .metrics
                        .last_sweep_files
                        .store(report.files_live, std::sync::atomic::Ordering::Relaxed);
                    state
                        .metrics
                        .last_sweep_bytes
                        .store(report.bytes_live, std::sync::atomic::Ordering::Relaxed);
                    state
                        .metrics
                        .last_sweep_removed
                        .store(report.files_removed, std::sync::atomic::Ordering::Relaxed);
                    tracing::info!(
                        live = report.files_live,
                        live_mb = report.bytes_live / 1_048_576,
                        removed = report.files_removed,
                        freed_mb = report.bytes_removed / 1_048_576,
                        dirs_pruned = report.dirs_pruned,
                        tmp_removed = report.tmp_removed,
                        "cache sweep complete"
                    );
                }
                Err(err) => tracing::error!(error = %err, "cache sweep failed"),
            }
            tokio::time::sleep(state.config.sweep_interval).await;
        }
    });
}
