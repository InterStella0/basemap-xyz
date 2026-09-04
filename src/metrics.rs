use std::sync::atomic::{AtomicU64, Ordering};

/// Cheap process-lifetime counters behind `/stats`.
///
/// Note these only ever see requests that reached this process. In the deployed setup nginx serves
/// cache hits straight off the volume, so `disk_hits` counts the narrow case of a tile that landed
/// on disk between nginx's stat() and our lookup — not the real hit rate. Read nginx's logs for
/// that; read these to see how hard the renderer is actually working.
#[derive(Debug, Default)]
pub struct Metrics {
    pub requests: AtomicU64,
    pub rejected: AtomicU64,
    pub disk_hits: AtomicU64,
    pub memory_hits: AtomicU64,
    pub renders: AtomicU64,
    pub render_errors: AtomicU64,
    pub negative_hits: AtomicU64,
    pub queue_timeouts: AtomicU64,
    /// Times the circuit breaker flipped open (renderer unreachable, failing fast).
    pub circuit_opens: AtomicU64,
    /// Misses answered 503 because the renderer itself is down (breaker open, transport error or
    /// timeout) rather than because one tile failed.
    pub renderer_unavailable: AtomicU64,
    pub render_millis_total: AtomicU64,
    /// Renders that were a metatile rather than a single tile. Counted in `renders` too, so
    /// `renders - metatile_renders` is the number of one-off tile renders.
    pub metatile_renders: AtomicU64,
    /// Tiles produced by slicing those metatiles. The ratio against `metatile_renders` is the
    /// leverage metatiling is actually buying: 16 for a healthy 4x4 band.
    pub metatile_tiles: AtomicU64,
    /// Metatiles that rendered but could not be cut up — a wrong-sized or undecodable image.
    /// Distinct from `render_errors`, because the renderer answered and the fault is downstream.
    pub metatile_slice_errors: AtomicU64,
    /// Filled in by each janitor sweep; zero until the first one finishes.
    pub last_sweep_files: AtomicU64,
    pub last_sweep_bytes: AtomicU64,
    pub last_sweep_removed: AtomicU64,
    pub sweeps: AtomicU64,
}

impl Metrics {
    pub fn bump(counter: &AtomicU64) {
        counter.fetch_add(1, Ordering::Relaxed);
    }

    pub fn add(counter: &AtomicU64, n: u64) {
        counter.fetch_add(n, Ordering::Relaxed);
    }

    pub fn get(counter: &AtomicU64) -> u64 {
        counter.load(Ordering::Relaxed)
    }

    pub fn snapshot(&self) -> serde_json::Value {
        let renders = Self::get(&self.renders);
        let total_ms = Self::get(&self.render_millis_total);
        serde_json::json!({
            "requests": Self::get(&self.requests),
            "rejected": Self::get(&self.rejected),
            "disk_hits": Self::get(&self.disk_hits),
            "memory_hits": Self::get(&self.memory_hits),
            "renders": renders,
            "metatile_renders": Self::get(&self.metatile_renders),
            "metatile_tiles": Self::get(&self.metatile_tiles),
            "metatile_slice_errors": Self::get(&self.metatile_slice_errors),
            "render_errors": Self::get(&self.render_errors),
            "negative_hits": Self::get(&self.negative_hits),
            "queue_timeouts": Self::get(&self.queue_timeouts),
            "circuit_opens": Self::get(&self.circuit_opens),
            "renderer_unavailable": Self::get(&self.renderer_unavailable),
            "avg_render_ms": if renders == 0 { None } else { Some(total_ms / renders) },
            "cache": {
                "sweeps": Self::get(&self.sweeps),
                "files": Self::get(&self.last_sweep_files),
                "bytes": Self::get(&self.last_sweep_bytes),
                "removed_last_sweep": Self::get(&self.last_sweep_removed),
            }
        })
    }
}
