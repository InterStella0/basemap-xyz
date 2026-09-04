use std::collections::HashMap;
use std::sync::Arc;

use bytes::Bytes;
use moka::future::Cache;

use crate::cache::TileCache;
use crate::config::Config;
use crate::metatile::{MetaCoord, SliceError};
use crate::metrics::Metrics;
use crate::renderer::{RenderError, Renderer};
use crate::tiles::TileCoord;

#[derive(Debug, Clone, thiserror::Error)]
pub enum TileFailure {
    #[error(transparent)]
    Render(#[from] RenderError),
    /// This tile failed recently and the negative cache is still holding the door shut. Reported
    /// separately so a burst against a broken tile shows up as one render error, not thousands.
    #[error("tile failed recently; not retrying yet")]
    RecentlyFailed,
    /// The block rendered, but could not be cut into tiles. The renderer is fine; the image it sent
    /// back was not what we asked for, so this is kept separate from a render failure.
    #[error(transparent)]
    Slice(#[from] SliceError),
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
    /// Single-flight gate for whole blocks, and a short-lived holding pen for their tiles.
    ///
    /// A block is the unit QGIS renders, so this is where dedup has to happen: sixteen browser
    /// requests arriving together for one z7 block must cause one 46-second render, not sixteen.
    /// The entry carries the sliced tiles so the fifteen callers that did not do the rendering can
    /// take their tile straight out of it instead of going back to disk.
    metas: Cache<MetaCoord, Arc<HashMap<TileCoord, Bytes>>>,
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
        // Small and short-lived on purpose. Its only job is to be the single-flight gate and to
        // hold a block's tiles long enough for the callers waiting on that render to take theirs;
        // every tile is also seeded into `memory` and written to disk, so nothing is lost when an
        // entry goes. A 4x4 block of PNGs is around a megabyte and the api container has a 1 GB
        // limit, so this is capped in blocks rather than scaled off `memory_capacity`.
        let metas = Cache::builder()
            .max_capacity(64)
            .time_to_live(std::time::Duration::from_secs(60))
            .build();
        Self { config, cache, renderer, metrics, memory, negative, metas }
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

        let state = self.clone();
        // `TileCoord` now carries a `String` layer, so it is no longer `Copy`: clone one instance
        // for the single-flight key and move the other into the closure below.
        let key = coord.clone();

        self.memory
            .try_get_with(key, async move {
                if state.negative.get(&coord).await.is_some() {
                    Metrics::bump(&state.metrics.negative_hits);
                    return Err(TileFailure::RecentlyFailed);
                }

                // Re-check the disk inside the single-flight section. nginx normally serves hits
                // before we ever see them, but this keeps the endpoint correct when it is called
                // directly, and catches the tile another worker just wrote.
                if let Some(bytes) = state.cache.read_fresh(&coord).await {
                    Metrics::bump(&state.metrics.disk_hits);
                    return Ok(bytes);
                }

                let block = MetaCoord::containing(&coord, state.config.metatile.resolve(coord.z));
                // `containing` clamps to the pyramid, so a 4x4 band still renders z0 and z1 as
                // single tiles. Below a 2x2 block there is nothing to metatile.
                if block.n > 1 {
                    state.render_block_for(&coord, block).await
                } else {
                    state.render_single(&coord).await
                }
            })
            .await
    }

    /// One tile, one `GetTile`. The path that predates metatiling, and still the path whenever
    /// `METATILE_SIZE` leaves a zoom unbanded.
    async fn render_single(&self, coord: &TileCoord) -> Result<Bytes, TileFailure> {
        let bytes = match self.renderer.fetch(coord).await {
            Ok(bytes) => bytes,
            Err(err) => {
                Metrics::bump(&self.metrics.render_errors);
                tracing::warn!(%coord, error = %err, "render failed");
                // Only per-tile failures are negative-cached. A renderer-down failure must not hold
                // the door shut: while the service is unreachable every miss keeps answering 503,
                // and retries are cheap because the circuit breaker fails them fast. Caching a dead
                // renderer's errors would turn the 503 contract into a mix of 502s depending on
                // which request tripped the cache first.
                if !err.is_renderer_down() {
                    self.negative.insert(coord.clone(), ()).await;
                }
                return Err(TileFailure::Render(err));
            }
        };

        // A failed write is not a failed request: the client gets its tile, we just did not manage
        // to keep it. Losing the cache is a capacity problem, not a 500.
        if let Err(err) = self.cache.store(coord, &bytes).await {
            tracing::error!(%coord, error = %err, "rendered tile but could not cache it");
        }
        Ok(bytes)
    }

    /// Renders the block `coord` belongs to and returns `coord`'s share of it.
    ///
    /// The other `n^2 - 1` tiles are written to disk and seeded into memory on the way past, so the
    /// pan that follows this request is free. Blocks are single-flighted separately from tiles:
    /// sixteen simultaneous requests for one block cause one render.
    async fn render_block_for(&self, coord: &TileCoord, block: MetaCoord) -> Result<Bytes, TileFailure> {
        let state = self.clone();
        let rendering = block.clone();
        let tiles = self
            .metas
            .try_get_with(block.clone(), async move { state.render_block(&rendering).await })
            .await
            // The waiters share one `Arc<TileFailure>`; each needs an owned copy to return, which
            // is what `TileFailure: Clone` is for.
            .map_err(|err| (*err).clone())?;

        tiles.get(coord).cloned().ok_or_else(|| {
            // Unreachable unless `containing` and `tiles` disagree, which the metatile tests pin.
            TileFailure::Slice(SliceError::Decode(format!("block {block} did not contain {coord}")))
        })
    }

    /// Render one block, slice it, and persist every tile in it.
    async fn render_block(&self, block: &MetaCoord) -> Result<Arc<HashMap<TileCoord, Bytes>>, TileFailure> {
        let buffer = self.config.metatile_buffer_px;

        let png = match self.renderer.fetch_metatile(block, buffer).await {
            Ok(png) => png,
            Err(err) => {
                Metrics::bump(&self.metrics.render_errors);
                tracing::warn!(%block, error = %err, "metatile render failed");
                if !err.is_renderer_down() {
                    self.poison(block).await;
                }
                return Err(TileFailure::Render(err));
            }
        };

        let sliced = match block.slice(&png, buffer) {
            Ok(sliced) => sliced,
            Err(err) => {
                Metrics::bump(&self.metrics.metatile_slice_errors);
                tracing::error!(%block, error = %err, "rendered a metatile that could not be sliced");
                self.poison(block).await;
                return Err(TileFailure::Slice(err));
            }
        };

        let mut tiles = HashMap::with_capacity(sliced.len());
        for (tile, bytes) in sliced {
            if let Err(err) = self.cache.store(&tile, &bytes).await {
                tracing::error!(coord = %tile, error = %err, "rendered tile but could not cache it");
            }
            self.memory.insert(tile.clone(), bytes.clone()).await;
            tiles.insert(tile, bytes);
        }
        Metrics::add(&self.metrics.metatile_tiles, tiles.len() as u64);
        Ok(Arc::new(tiles))
    }

    /// Negative-caches every tile in a block after it failed.
    ///
    /// Without this the fifteen siblings of the tile that triggered a doomed render would each go
    /// on to pay for the same failing render again.
    async fn poison(&self, block: &MetaCoord) {
        for tile in block.tiles() {
            self.negative.insert(tile, ()).await;
        }
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
