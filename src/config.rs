use std::env;
use std::path::PathBuf;
use std::str::FromStr;
use std::time::Duration;

use crate::tiles::is_safe_layer_name;

/// One zoom band of one public layer, and the QGIS layer that should actually render it.
#[derive(Debug, Clone, PartialEq, Eq)]
struct LayerRoute {
    public: String,
    lo: u8,
    hi: u8,
    upstream: String,
}

/// Maps `(public layer, zoom) -> upstream WMTS layer`.
///
/// This is what lets one public URL serve cheap generalized geometry zoomed out and expensive
/// detail zoomed in: `/tiles/countries/3/...` renders from `simple-countries` while
/// `/tiles/countries/14/...` renders from `countries`, and clients never see the difference.
///
/// Deliberately *not* applied to the cache path — see `TileCoord::wmts_query`.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LayerRoutes(Vec<LayerRoute>);

impl LayerRoutes {
    /// First matching rule wins; an unrouted layer or zoom renders from its own name, which is
    /// exactly the behaviour before routing existed.
    pub fn resolve<'a>(&'a self, public: &'a str, z: u8) -> &'a str {
        self.0
            .iter()
            .find(|r| r.public == public && z >= r.lo && z <= r.hi)
            .map(|r| r.upstream.as_str())
            .unwrap_or(public)
    }
}

/// `countries@0-5=simple-countries,countries@6-20=countries`
///
/// Parsed strictly: a malformed rule is an error, which `get_env_default` turns into a boot panic.
/// Silently ignoring a typo here would serve the wrong cartography for `TILE_TTL_DAYS`.
impl FromStr for LayerRoutes {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let mut routes = Vec::new();
        for rule in raw.split(',').map(str::trim).filter(|r| !r.is_empty()) {
            let (left, upstream) = rule
                .split_once('=')
                .ok_or_else(|| format!("rule {rule:?} is missing '=<upstream layer>'"))?;
            let (public, range) = left
                .split_once('@')
                .ok_or_else(|| format!("rule {rule:?} is missing '@<lo>-<hi>'"))?;
            let (lo, hi) = range
                .split_once('-')
                .ok_or_else(|| format!("zoom range {range:?} in {rule:?} is not '<lo>-<hi>'"))?;

            let public = public.trim();
            let upstream = upstream.trim();
            for (label, name) in [("layer", public), ("upstream layer", upstream)] {
                if !is_safe_layer_name(name) {
                    return Err(format!("{label} {name:?} in rule {rule:?} is not a valid layer name"));
                }
            }

            let parse_z = |v: &str, which: &str| -> Result<u8, String> {
                v.trim()
                    .parse::<u8>()
                    .map_err(|_| format!("{which} zoom {v:?} in rule {rule:?} is not a number 0-255"))
            };
            let lo = parse_z(lo, "low")?;
            let hi = parse_z(hi, "high")?;
            if lo > hi {
                return Err(format!("zoom range {lo}-{hi} in rule {rule:?} is inverted"));
            }

            routes.push(LayerRoute {
                public: public.to_string(),
                lo,
                hi,
                upstream: upstream.to_string(),
            });
        }
        Ok(Self(routes))
    }
}

/// Metatile edge, in tiles, per zoom band.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MetatileSizes(Vec<(u8, u8, u32)>);

impl Default for MetatileSizes {
    fn default() -> Self {
        Self(Vec::new())
    }
}

impl MetatileSizes {
    pub fn resolve(&self, z: u8) -> u32 {
        self.0
            .iter()
            .find(|(lo, hi, _)| z >= *lo && z <= *hi)
            .map(|(_, _, n)| *n)
            .unwrap_or(1)
    }

    pub fn is_enabled(&self) -> bool {
        self.0.iter().any(|(_, _, n)| *n > 1)
    }
}

/// `0-6:2,7-20:4`
///
/// Parsed strictly for the same reason `LayerRoutes` is: a typo here silently changes how every
/// tile in a band is rendered, and `get_env_default` turns the error into a boot panic.
impl FromStr for MetatileSizes {
    type Err = String;

    fn from_str(raw: &str) -> Result<Self, Self::Err> {
        let mut bands = Vec::new();
        for rule in raw.split(',').map(str::trim).filter(|r| !r.is_empty()) {
            let (range, size) = rule
                .split_once(':')
                .ok_or_else(|| format!("band {rule:?} is missing ':<tiles per side>'"))?;
            let (lo, hi) = range
                .split_once('-')
                .ok_or_else(|| format!("zoom range {range:?} in {rule:?} is not '<lo>-<hi>'"))?;

            let parse = |v: &str, which: &str| -> Result<u32, String> {
                v.trim()
                    .parse::<u32>()
                    .map_err(|_| format!("{which} {v:?} in band {rule:?} is not a number"))
            };
            let lo = u8::try_from(parse(lo, "low zoom")?)
                .map_err(|_| format!("low zoom in band {rule:?} is above 255"))?;
            let hi = u8::try_from(parse(hi, "high zoom")?)
                .map_err(|_| format!("high zoom in band {rule:?} is above 255"))?;
            let size = parse(size, "block size")?;

            if lo > hi {
                return Err(format!("zoom range {lo}-{hi} in band {rule:?} is inverted"));
            }
            // Powers of two only: any other edge would put block boundaries at different places on
            // adjacent zooms for no benefit, and 8x8 at 256px is already a 2048px render.
            if !matches!(size, 1 | 2 | 4 | 8) {
                return Err(format!("block size {size} in band {rule:?} must be 1, 2, 4 or 8"));
            }
            bands.push((lo, hi, size));
        }
        Ok(Self(bands))
    }
}

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
    /// Consecutive transport-level failures (connection refused, timeout) before the renderer is
    /// declared down and the circuit opens. Any response at all — even an HTTP 500 — resets the
    /// counter, because an answer proves the process is alive.
    pub render_failure_threshold: u32,
    /// How long the circuit stays open once tripped. While open, uncached tiles fail fast with a
    /// 503 instead of each burning `render_timeout` against a dead renderer.
    pub render_circuit_open: Duration,
    pub memory_capacity: u64,
    pub memory_ttl: Duration,
    pub negative_ttl: Duration,
    /// Cache version
    pub project_version: Option<String>,
    /// Zoom-dependent mapping from the layer named in the URL to the QGIS layer that renders it.
    /// Empty (the default) means every layer renders from its own name.
    pub layer_routes: LayerRoutes,
    /// How many tiles per side QGIS renders in one request, per zoom band. See `MetatileSizes`.
    pub metatile: MetatileSizes,
    pub metatile_buffer_px: u32,
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
            render_failure_threshold: get_env_default("RENDER_FAILURE_THRESHOLD", 3),
            render_circuit_open: Duration::from_secs(get_env_default("RENDER_CIRCUIT_OPEN_SECS", 30)),
            memory_capacity: get_env_default("TILE_MEMORY_CAPACITY", 4096),
            memory_ttl: Duration::from_secs(get_env_default("TILE_MEMORY_TTL_SECS", 600)),
            negative_ttl: Duration::from_secs(get_env_default("TILE_NEGATIVE_TTL_SECS", 60)),
            // Any non-empty string is valid here, so `get_env_default` (which panics on an
            // unparseable value) doesn't fit — this is parsed by hand instead.
            project_version: env::var("PROJECT_VERSION")
                .ok()
                .map(|v| v.trim().to_string())
                .filter(|v| !v.is_empty()),
            layer_routes: get_env_default("TILE_LAYER_ROUTES", LayerRoutes::default()),
            metatile: get_env_default("METATILE_SIZE", MetatileSizes::default()),
            metatile_buffer_px: get_env_default("METATILE_BUFFER_PX", 128),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn routes(raw: &str) -> LayerRoutes {
        raw.parse().expect("should parse")
    }

    #[test]
    fn an_unset_route_table_leaves_every_layer_rendering_from_its_own_name() {
        let r = LayerRoutes::default();
        for z in [0, 5, 6, 20] {
            assert_eq!(r.resolve("countries", z), "countries");
        }
    }

    #[test]
    fn the_zoom_band_boundary_is_inclusive_on_both_ends() {
        let r = routes("countries@0-5=simple-countries");
        assert_eq!(r.resolve("countries", 0), "simple-countries");
        assert_eq!(r.resolve("countries", 5), "simple-countries");
        // Past the band there is no rule, so it falls through to the requested name.
        assert_eq!(r.resolve("countries", 6), "countries");
        assert_eq!(r.resolve("countries", 20), "countries");
    }

    #[test]
    fn an_explicit_two_band_table_routes_both_sides() {
        let r = routes("countries@0-5=simple-countries,countries@6-20=countries-detail");
        assert_eq!(r.resolve("countries", 5), "simple-countries");
        assert_eq!(r.resolve("countries", 6), "countries-detail");
    }

    #[test]
    fn layers_without_a_rule_are_untouched() {
        let r = routes("countries@0-5=simple-countries");
        assert_eq!(r.resolve("roads", 3), "roads");
    }

    #[test]
    fn whitespace_and_trailing_separators_are_tolerated() {
        let r = routes("  countries@0-5 = simple-countries , ");
        assert_eq!(r.resolve("countries", 2), "simple-countries");
    }

    fn sizes(raw: &str) -> MetatileSizes {
        raw.parse().expect("should parse")
    }

    #[test]
    fn an_unset_metatile_table_renders_every_zoom_one_tile_at_a_time() {
        let m = MetatileSizes::default();
        assert!(!m.is_enabled());
        for z in [0, 5, 7, 20] {
            assert_eq!(m.resolve(z), 1);
        }
    }

    #[test]
    fn metatile_bands_are_inclusive_and_fall_through_to_one() {
        let m = sizes("0-6:2,7-20:4");
        assert!(m.is_enabled());
        assert_eq!(m.resolve(0), 2);
        assert_eq!(m.resolve(6), 2);
        assert_eq!(m.resolve(7), 4);
        assert_eq!(m.resolve(20), 4);
        assert_eq!(m.resolve(21), 1);
    }

    #[test]
    fn malformed_metatile_bands_are_rejected_rather_than_ignored() {
        for bad in [
            "0-6",        // no size
            "0-6:",       // empty size
            "6:2",        // range is not lo-hi
            "6-0:2",      // inverted
            "0-999:2",    // zoom out of u8 range
            "0-6:3",      // not a power of two
            "0-6:0",      // zero would divide by zero downstream
            "0-6:16",     // a 4096px render is not a tile server
            "0-6:two",    // not a number
        ] {
            assert!(bad.parse::<MetatileSizes>().is_err(), "expected {bad:?} to be rejected");
        }
    }

    /// Every one of these would otherwise serve the wrong cartography, silently, until the TTL
    /// expired. `get_env_default` turns each into a boot panic.
    #[test]
    fn malformed_rules_are_rejected_rather_than_ignored() {
        for bad in [
            "countries@0-5",              // no upstream
            "countries=simple-countries", // no zoom band
            "countries@0=simple",         // range is not lo-hi
            "countries@5-0=simple",       // inverted
            "countries@0-999=simple",     // zoom out of u8 range
            "countries@0-5=../etc",       // unsafe upstream name
            "a/b@0-5=simple",             // unsafe public name
            "countries@0-5=",             // empty upstream
        ] {
            assert!(bad.parse::<LayerRoutes>().is_err(), "expected {bad:?} to be rejected");
        }
    }
}
