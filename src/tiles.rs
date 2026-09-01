use std::fmt;
use std::path::{Path, PathBuf};

/// An XYZ tile address in EPSG:3857, `y` counted from the top-left as every XYZ client expects.
///
/// Hashed and compared by value: this doubles as the single-flight key, so two requests for the
/// same tile (same layer *and* coordinate) must produce the same key or the whole dedup scheme
/// silently stops working.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TileCoord {
    /// The WMTS layer's short name, as requested in the URL. Not checked against a fixed list —
    /// QGIS Server itself rejects an unpublished name via a `ServiceException` — but it is
    /// sanitized in `validate()` before it ever reaches a filesystem path or an outbound request.
    pub layer: String,
    pub z: u8,
    pub x: u32,
    pub y: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum TileError {
    #[error("zoom {z} is outside the served range {min}..={max}")]
    ZoomOutOfRange { z: u8, min: u8, max: u8 },
    #[error("tile {x}/{y} does not exist at zoom {z} (max index {max})")]
    CoordOutOfRange { z: u8, x: u32, y: u32, max: u32 },
    /// The layer segment is now caller-controlled and lands directly in a filesystem path
    /// (`cache_path`) and a query string (`wmts_query`), so it is restricted to a conservative
    /// charset rather than merely checked for emptiness. This is a safety net, not an allow-list:
    /// any layer name in this shape is forwarded to QGIS Server as-is.
    #[error("invalid layer name {0:?}")]
    InvalidLayer(String),
}

/// The conservative charset for anything that can end up as a path segment or a WMTS `LAYER`
/// value. Applied to the caller-supplied layer in the URL *and* to the operator-supplied upstream
/// names in `TILE_LAYER_ROUTES`, so a route cannot smuggle in a shape the URL parser would reject.
/// Mirrored by the location regex in `nginx/nginx.conf`.
pub fn is_safe_layer_name(name: &str) -> bool {
    !name.is_empty()
        && name.bytes().all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.'))
        && name != "."
        && name != ".."
}

impl TileCoord {
    pub fn new(layer: impl Into<String>, z: u8, x: u32, y: u32) -> Self {
        Self { layer: layer.into(), z, x, y }
    }

    /// Rejects impossible tiles and unsafe layer names before they can reach QGIS or the
    /// filesystem. Without the zoom/coord checks a crawler walking `/tiles/*/30/.../...` would
    /// spend renderer processes producing ServiceExceptions; without the layer check, a layer
    /// segment containing `..` or `/` could escape the cache directory entirely.
    pub fn validate(&self, min_zoom: u8, max_zoom: u8) -> Result<(), TileError> {
        if !is_safe_layer_name(&self.layer) {
            return Err(TileError::InvalidLayer(self.layer.clone()));
        }

        if self.z < min_zoom || self.z > max_zoom {
            return Err(TileError::ZoomOutOfRange { z: self.z, min: min_zoom, max: max_zoom });
        }
        let span = 1u32 << self.z;
        if self.x >= span || self.y >= span {
            return Err(TileError::CoordOutOfRange { z: self.z, x: self.x, y: self.y, max: span - 1 });
        }
        Ok(())
    }

    /// `{root}/{layer}/{z}/{x}/{y}.png`.
    ///
    /// This layout is contractual: nginx serves cache hits straight off the same volume with
    /// `try_files /$layer/$tz/$tx/$ty.png` (see nginx/nginx.conf). Changing the shape here without
    /// changing it there turns every hit into a miss, and nothing will fail loudly when it happens.
    pub fn cache_path(&self, root: &Path) -> PathBuf {
        root.join(&self.layer)
            .join(self.z.to_string())
            .join(self.x.to_string())
            .join(format!("{}.png", self.y))
    }

    /// Scratch path in the *same* directory as the final file, so the rename that publishes it is
    /// atomic. The `.tmp` suffix keeps it outside nginx's `\.png$` route, so a partial write is
    /// never servable even in the instant before the rename.
    pub fn tmp_path(&self, root: &Path, nonce: u64) -> PathBuf {
        root.join(&self.layer)
            .join(self.z.to_string())
            .join(self.x.to_string())
            .join(format!("{}.png.{nonce}.tmp", self.y))
    }

    /// WMTS GetTile query for QGIS Server.
    ///
    /// Parameter shape copied from the tile location in zegraph-web's nginx config, which is known
    /// to work against this same image: TILEMATRIX is the bare zoom number within the `EPSG:3857`
    /// tile matrix set, and TILEROW takes the XYZ `y` directly because QGIS's 3857 matrix set is
    /// also top-left origin.
    ///
    /// `upstream_layer` is passed in rather than read from `self.layer` so that zoom-based routing
    /// (`TILE_LAYER_ROUTES`) can render a tile from a different QGIS layer than the one named in
    /// the URL. It must stay a parameter and never become a field: `TileCoord` is the disk path and
    /// the single-flight key, so a coordinate that renders from two upstreams at different zooms is
    /// still exactly one cache entry per z/x/y.
    pub fn wmts_query(&self, upstream_layer: &str) -> Vec<(&'static str, String)> {
        vec![
            ("SERVICE", "WMTS".to_string()),
            ("VERSION", "1.0.0".to_string()),
            ("REQUEST", "GetTile".to_string()),
            ("LAYER", upstream_layer.to_string()),
            ("STYLE", "default".to_string()),
            ("FORMAT", "image/png".to_string()),
            ("TILEMATRIXSET", "EPSG:3857".to_string()),
            ("TILEMATRIX", self.z.to_string()),
            ("TILECOL", self.x.to_string()),
            ("TILEROW", self.y.to_string()),
        ]
    }
}

impl fmt::Display for TileCoord {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}/{}/{}/{}", self.layer, self.z, self.x, self.y)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_tiles_inside_the_pyramid() {
        assert!(TileCoord::new("countries", 0, 0, 0).validate(0, 20).is_ok());
        // Last valid tile at z=1 and at the configured max zoom.
        assert!(TileCoord::new("countries", 1, 1, 1).validate(0, 20).is_ok());
        assert!(TileCoord::new("countries", 20, (1 << 20) - 1, (1 << 20) - 1).validate(0, 20).is_ok());
    }

    #[test]
    fn rejects_zoom_outside_the_served_range() {
        assert_eq!(
            TileCoord::new("countries", 21, 0, 0).validate(0, 20),
            Err(TileError::ZoomOutOfRange { z: 21, min: 0, max: 20 })
        );
        assert_eq!(
            TileCoord::new("countries", 1, 0, 0).validate(2, 20),
            Err(TileError::ZoomOutOfRange { z: 1, min: 2, max: 20 })
        );
    }

    #[test]
    fn rejects_coords_that_do_not_exist_at_that_zoom() {
        // z=0 has exactly one tile, so 1/0 is already off the edge.
        assert_eq!(
            TileCoord::new("countries", 0, 1, 0).validate(0, 20),
            Err(TileError::CoordOutOfRange { z: 0, x: 1, y: 0, max: 0 })
        );
        assert_eq!(
            TileCoord::new("countries", 1, 0, 2).validate(0, 20),
            Err(TileError::CoordOutOfRange { z: 1, x: 0, y: 2, max: 1 })
        );
        // The boundary is exclusive: 2^20 is one past the last index at z=20.
        assert!(TileCoord::new("countries", 20, 1 << 20, 0).validate(0, 20).is_err());
    }

    #[test]
    fn rejects_unsafe_or_empty_layer_names() {
        for bad in ["", "..", ".", "../../etc", "a/b", "a b", "layer\0"] {
            assert_eq!(
                TileCoord::new(bad, 0, 0, 0).validate(0, 20),
                Err(TileError::InvalidLayer(bad.to_string())),
                "expected {bad:?} to be rejected"
            );
        }
    }

    #[test]
    fn accepts_layer_names_with_dots_dashes_and_underscores() {
        assert!(TileCoord::new("dark-basemap_v2.1", 0, 0, 0).validate(0, 20).is_ok());
    }

    #[test]
    fn cache_path_matches_the_layout_nginx_serves() {
        let p = TileCoord::new("countries", 9, 271, 171).cache_path(Path::new("/var/cache/tiles"));
        assert_eq!(p, Path::new("/var/cache/tiles/countries/9/271/171.png"));
    }

    #[test]
    fn different_layers_at_the_same_coordinate_land_in_different_paths() {
        let root = Path::new("/var/cache/tiles");
        let a = TileCoord::new("countries", 9, 271, 171).cache_path(root);
        let b = TileCoord::new("roads", 9, 271, 171).cache_path(root);
        assert_ne!(a, b);
    }

    #[test]
    fn tmp_path_is_a_sibling_and_is_not_a_png() {
        let root = Path::new("/var/cache/tiles");
        let coord = TileCoord::new("countries", 9, 271, 171);
        let tmp = coord.tmp_path(root, 42);
        assert_eq!(tmp.parent(), coord.cache_path(root).parent(), "rename must stay within one dir");
        assert!(!tmp.to_string_lossy().ends_with(".png"), "nginx must not be able to serve a partial write");
    }

    #[test]
    fn wmts_query_matches_the_known_good_parameter_shape() {
        let q = TileCoord::new("darkbasemap", 9, 271, 171).wmts_query("darkbasemap");
        let encoded: Vec<String> = q.iter().map(|(k, v)| format!("{k}={v}")).collect();
        assert_eq!(
            encoded.join("&"),
            "SERVICE=WMTS&VERSION=1.0.0&REQUEST=GetTile&LAYER=darkbasemap&STYLE=default\
             &FORMAT=image/png&TILEMATRIXSET=EPSG:3857&TILEMATRIX=9&TILECOL=271&TILEROW=171"
        );
    }

    /// The routing case: the URL says `countries`, the render comes from `simple-countries`.
    #[test]
    fn wmts_query_asks_for_the_upstream_layer_not_the_requested_one() {
        let coord = TileCoord::new("countries", 3, 4, 3);
        let q = coord.wmts_query("simple-countries");
        let layer = q.iter().find(|(k, _)| *k == "LAYER").map(|(_, v)| v.as_str());
        assert_eq!(layer, Some("simple-countries"));
        // The cache path is unaffected: it follows the requested name, which is what nginx serves.
        assert_eq!(
            coord.cache_path(Path::new("/var/cache/tiles")),
            Path::new("/var/cache/tiles/countries/3/4/3.png")
        );
    }

    #[test]
    fn safe_layer_names_agree_with_the_coordinate_validator() {
        for good in ["countries", "simple-countries", "dark-basemap_v2.1"] {
            assert!(is_safe_layer_name(good), "{good:?} should be accepted");
        }
        for bad in ["", "..", ".", "../../etc", "a/b", "a b", "layer\0"] {
            assert!(!is_safe_layer_name(bad), "{bad:?} should be rejected");
        }
    }
}
