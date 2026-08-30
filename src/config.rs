use std::env;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

/// Reads an optional variable, falling back to the compiled-in default. A present-but-unparseable
/// value is a panic, not a silent fallback: a typo in TILE_TTL_DAYS should stop the container
/// rather than quietly serve a different TTL than the operator asked for.
pub fn get_env_default<T: FromStr>(key: &str, default: T) -> T {
    match env::var(key) {
        Ok(raw) if !raw.trim().is_empty() => raw
            .trim()
            .parse()
            .unwrap_or_else(|_| panic!("environment variable {key} has an unparseable value: {raw:?}")),
        _ => default,
    }
}

#[derive(Debug, Clone)]
pub struct Config {
    pub port: u16,
    /// Base URL of the QGIS Server container, e.g. `http://renderer`. The official
    /// `qgis/qgis-server` image runs its own nginx on :80 and exposes `/ows/` for direct access to
    /// the project named by `QGIS_PROJECT_FILE`, so we speak plain HTTP to it.
    pub renderer_url: String,
    pub cache_dir: PathBuf,
    pub tile_ttl: Duration,
    pub sweep_interval: Duration,
    /// `max-age` handed to browsers and CDNs. Independent of `tile_ttl`, which governs our disk.
    pub client_max_age: u64,
    pub min_zoom: u8,
    pub max_zoom: u8,
    /// Upstream renders allowed at once. Match to the renderer's FCGID_MAX_PROCESSES; going higher
    /// just queues inside QGIS where we cannot time it out.
    pub render_concurrency: usize,
    pub render_queue_timeout: Duration,
    pub render_timeout: Duration,
    pub memory_capacity: u64,
    pub memory_ttl: Duration,
    pub negative_ttl: Duration,
    /// Opaque operator-supplied tag (by convention a hash of project.qgz, written by
    /// `scripts/sync-project-version.sh`) that the cache compares against a marker on disk at boot.
    /// A mismatch means the cartography changed, so the whole cache is wiped rather than waiting
    /// out `tile_ttl`. `None` (unset or empty) means "not tracking this" — pure TTL behaviour,
    /// unchanged from today.
    pub project_version: Option<String>,
}

impl Config {
    pub fn from_env() -> Self {
        let max_zoom: u8 = get_env_default("MAX_ZOOM", 20);
        let min_zoom: u8 = get_env_default("MIN_ZOOM", 0);
        assert!(min_zoom <= max_zoom, "MIN_ZOOM ({min_zoom}) must not exceed MAX_ZOOM ({max_zoom})");
        // 2^z must fit in the u32 that TileCoord uses for x/y.
        assert!(max_zoom < 32, "MAX_ZOOM ({max_zoom}) must be below 32");

        Self {
            port: get_env_default("PORT", 3000),
            renderer_url: get_env_default("RENDERER_URL", "http://renderer".to_string())
                .trim_end_matches('/')
                .to_string(),
            cache_dir: PathBuf::from(get_env_default(
                "TILE_CACHE_DIR",
                "/var/cache/tiles".to_string(),
            )),
            tile_ttl: Duration::from_secs(get_env_default::<u64>("TILE_TTL_DAYS", 120) * 86_400),
            sweep_interval: Duration::from_secs(
                get_env_default::<u64>("TILE_SWEEP_INTERVAL_HOURS", 6) * 3_600,
            ),
            client_max_age: get_env_default("TILE_CLIENT_MAX_AGE", 604_800),
            min_zoom,
            max_zoom,
            render_concurrency: get_env_default("RENDER_CONCURRENCY", 4),
            render_queue_timeout: Duration::from_secs(get_env_default(
                "RENDER_QUEUE_TIMEOUT_SECS",
                20,
            )),
            render_timeout: Duration::from_secs(get_env_default("RENDER_TIMEOUT_SECS", 60)),
            memory_capacity: get_env_default("TILE_MEMORY_CAPACITY", 4096),
            memory_ttl: Duration::from_secs(get_env_default("TILE_MEMORY_TTL_SECS", 600)),
            negative_ttl: Duration::from_secs(get_env_default("TILE_NEGATIVE_TTL_SECS", 60)),
            // Any non-empty string is valid here, so `get_env_default` (which panics on an
            // unparseable value) doesn't fit — this is parsed by hand instead.
            project_version: env::var("PROJECT_VERSION")
                .ok()
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty()),
        }
    }
}
