use std::io::Cursor;

use bytes::Bytes;
use image::codecs::png::{CompressionType, FilterType, PngEncoder};
use image::{ImageEncoder, ImageReader};

use crate::tiles::TileCoord;

/// Edge of the Web-Mercator square in metres. `EPSG:3857` spans `-E..E` on both axes, and the XYZ
/// pyramid divides exactly that square into `2^z` columns and rows.
const MERCATOR_HALF_SPAN: f64 = 20_037_508.342_789_244;

/// One tile is 256 px in every tile matrix set this project serves.
pub const TILE_PX: u32 = 256;

/// A square block of `n x n` tiles, rendered by QGIS Server in a single WMS `GetMap` and then cut
/// into its tiles.
///
/// This exists because QGIS renders each request in isolation: labelling runs against the extent it
/// was given and nothing else. Ask for one 256 px tile at a time and a label that crosses the tile
/// edge is cut in half, while two labels in adjacent tiles cannot see each other and overprint.
/// Ask for the whole block at once and the label engine places every label in it against one
/// canvas, which is the only way a raster tile server gets whole labels.
///
/// Hashed and compared by value: like `TileCoord` this doubles as a single-flight key, so two
/// requests for tiles in the same block must produce the same key or one block would be rendered
/// `n^2` times.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct MetaCoord {
    pub layer: String,
    pub z: u8,
    /// Column of the block's top-left tile, always a multiple of `n`.
    pub mx: u32,
    /// Row of the block's top-left tile, always a multiple of `n`.
    pub my: u32,
    /// Block edge in tiles. Clamped to the pyramid, so at z0 this is 1 however large `METATILE_SIZE` is.
    pub n: u32,
}

#[derive(Debug, Clone, thiserror::Error)]
pub enum SliceError {
    #[error("could not decode the metatile PNG: {0}")]
    Decode(String),
    #[error("renderer returned a {got_w}x{got_h} image, expected {want}x{want}")]
    WrongSize { got_w: u32, got_h: u32, want: u32 },
    #[error("could not encode tile {coord}: {reason}")]
    Encode { coord: String, reason: String },
}

impl MetaCoord {
    /// The block containing `coord`, on the fixed `size x size` grid.
    ///
    /// Alignment is floored rather than centred on the request deliberately: every tile in a block
    /// must map to the *same* block, or the cache would fill with several differently-labelled
    /// renders of the same ground.
    pub fn containing(coord: &TileCoord, size: u32) -> Self {
        // A block can never be wider than the pyramid: at z1 there are two columns, so a size-4
        // block would ask for tiles that do not exist and QGIS would answer with a quarter of the
        // world stretched over the full image.
        let n = size.max(1).min(1u32 << coord.z.min(31));
        Self {
            layer: coord.layer.clone(),
            z: coord.z,
            mx: coord.x / n * n,
            my: coord.y / n * n,
            n,
        }
    }

    /// Side of the rendered image in pixels: the block plus `buffer_px` of overspill on each edge.
    pub fn size_px(&self, buffer_px: u32) -> u32 {
        self.n * TILE_PX + 2 * buffer_px
    }

    /// `(min_x, min_y, max_x, max_y)` in EPSG:3857, grown by `buffer_px` on all four sides.
    ///
    /// The buffer is what stitches labels across *block* boundaries. A label is centred on its
    /// anchor, so an anchor up to `buffer_px` outside the block still gets drawn here, at exactly
    /// the position the neighbouring block draws it — the two halves line up and the seam
    /// disappears. Anything wider than `2 * buffer_px` can still be cut, which is why the default
    /// is set from the widest label this cartography actually produces.
    pub fn bbox(&self, buffer_px: u32) -> (f64, f64, f64, f64) {
        let span = 2.0 * MERCATOR_HALF_SPAN / f64::from(1u32 << self.z);
        let pad = f64::from(buffer_px) / f64::from(TILE_PX) * span;
        let min_x = -MERCATOR_HALF_SPAN + f64::from(self.mx) * span - pad;
        let max_x = -MERCATOR_HALF_SPAN + f64::from(self.mx + self.n) * span + pad;
        // XYZ rows count from the top, so row `my` starts at the *north* edge and grows southward.
        let max_y = MERCATOR_HALF_SPAN - f64::from(self.my) * span + pad;
        let min_y = MERCATOR_HALF_SPAN - f64::from(self.my + self.n) * span - pad;
        (min_x, min_y, max_x, max_y)
    }

    /// Every tile this block covers, row-major.
    pub fn tiles(&self) -> impl Iterator<Item = TileCoord> + '_ {
        (0..self.n).flat_map(move |dy| {
            (0..self.n).map(move |dx| TileCoord::new(self.layer.clone(), self.z, self.mx + dx, self.my + dy))
        })
    }

    /// Top-left pixel of `coord` inside the rendered image, or `None` if it is not in this block.
    pub fn crop_origin(&self, coord: &TileCoord, buffer_px: u32) -> Option<(u32, u32)> {
        if coord.layer != self.layer || coord.z != self.z {
            return None;
        }
        let dx = coord.x.checked_sub(self.mx)?;
        let dy = coord.y.checked_sub(self.my)?;
        (dx < self.n && dy < self.n).then(|| (buffer_px + dx * TILE_PX, buffer_px + dy * TILE_PX))
    }

    /// WMS `GetMap` query for QGIS Server.
    ///
    /// WMS rather than WMTS because only `GetMap` takes an arbitrary extent, which is the whole
    /// point. Three parameters are load-bearing, all three verified against the live renderer:
    ///
    /// * `TRANSPARENT=TRUE` — without it the default background is opaque white, where `GetTile`
    ///   hands back transparent. Getting this wrong paints a white sea.
    /// * `VERSION=1.1.1` with `SRS=` — 1.3.0 would introduce the axis-order question for no gain.
    /// * no `DPI` — QGIS derives the resolution from `BBOX`/`WIDTH`, and forcing the WMTS
    ///   90.7 DPI changes at most 6/255 on one channel. Not worth a parameter that can drift.
    ///
    /// `upstream_layer` is a parameter for the same reason it is one on `TileCoord::wmts_query`:
    /// `TILE_LAYER_ROUTES` may render this zoom from a different QGIS layer than the URL names.
    pub fn wms_query(&self, upstream_layer: &str, buffer_px: u32) -> Vec<(&'static str, String)> {
        let (min_x, min_y, max_x, max_y) = self.bbox(buffer_px);
        let px = self.size_px(buffer_px).to_string();
        vec![
            ("SERVICE", "WMS".to_string()),
            ("VERSION", "1.1.1".to_string()),
            ("REQUEST", "GetMap".to_string()),
            ("LAYERS", upstream_layer.to_string()),
            ("STYLES", String::new()),
            ("FORMAT", "image/png".to_string()),
            ("TRANSPARENT", "TRUE".to_string()),
            ("SRS", "EPSG:3857".to_string()),
            ("BBOX", format!("{min_x},{min_y},{max_x},{max_y}")),
            ("WIDTH", px.clone()),
            ("HEIGHT", px),
        ]
    }

    /// Cuts a rendered block into its tiles, dropping the buffer.
    ///
    /// Decodes once and re-encodes `n^2` times; about 50 ms for a 4x4 block against a render that
    /// costs tens of seconds.
    pub fn slice(&self, png: &[u8], buffer_px: u32) -> Result<Vec<(TileCoord, Bytes)>, SliceError> {
        let want = self.size_px(buffer_px);
        let image = ImageReader::new(Cursor::new(png))
            .with_guessed_format()
            .map_err(|err| SliceError::Decode(err.to_string()))?
            .decode()
            .map_err(|err| SliceError::Decode(err.to_string()))?
            .into_rgba8();

        if image.width() != want || image.height() != want {
            return Err(SliceError::WrongSize { got_w: image.width(), got_h: image.height(), want });
        }

        let mut out = Vec::with_capacity((self.n * self.n) as usize);
        for coord in self.tiles() {
            let (x, y) = self
                .crop_origin(&coord, buffer_px)
                .expect("tiles() only yields coordinates inside this block");
            let tile = image::imageops::crop_imm(&image, x, y, TILE_PX, TILE_PX).to_image();

            // Best/Adaptive rather than the encoder defaults: these tiles are cached for years and
            // served to the public, and the default settings produced files around 40% larger than
            // the ones QGIS's own PNG writer used to hand back. Tens of milliseconds per tile
            // against a render measured in tens of seconds.
            let mut encoded = Vec::new();
            PngEncoder::new_with_quality(&mut encoded, CompressionType::Best, FilterType::Adaptive)
                .write_image(tile.as_raw(), TILE_PX, TILE_PX, image::ExtendedColorType::Rgba8)
                .map_err(|err| SliceError::Encode { coord: coord.to_string(), reason: err.to_string() })?;
            out.push((coord, Bytes::from(encoded)));
        }
        Ok(out)
    }
}

impl std::fmt::Display for MetaCoord {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}/{}/{}+{}", self.layer, self.z, self.mx, self.my, self.n)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png(width: u32, height: u32, colour: impl Fn(u32, u32) -> [u8; 4]) -> Vec<u8> {
        let image = image::RgbaImage::from_fn(width, height, |x, y| image::Rgba(colour(x, y)));
        let mut out = Vec::new();
        image::codecs::png::PngEncoder::new(&mut out)
            .write_image(image.as_raw(), width, height, image::ExtendedColorType::Rgba8)
            .unwrap();
        out
    }

    #[test]
    fn every_tile_in_a_block_resolves_to_the_same_block() {
        let first = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 4);
        for x in 68..72 {
            for y in 44..48 {
                assert_eq!(MetaCoord::containing(&TileCoord::new("countries", 7, x, y), 4), first);
            }
        }
        assert_eq!((first.mx, first.my, first.n), (68, 44, 4));
        // One past the block's edge is a different block.
        assert_ne!(MetaCoord::containing(&TileCoord::new("countries", 7, 72, 44), 4), first);
    }

    #[test]
    fn a_block_is_clamped_to_the_pyramid() {
        // z0 has one tile, z1 has four: a size-4 block cannot exist at either.
        assert_eq!(MetaCoord::containing(&TileCoord::new("countries", 0, 0, 0), 4).n, 1);
        assert_eq!(MetaCoord::containing(&TileCoord::new("countries", 1, 1, 1), 4).n, 2);
        assert_eq!(MetaCoord::containing(&TileCoord::new("countries", 2, 3, 3), 4).n, 4);
        // A size of zero would divide by zero; it is treated as "no metatiling".
        assert_eq!(MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 0).n, 1);
    }

    #[test]
    fn different_layers_never_share_a_block() {
        let a = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 4);
        let b = MetaCoord::containing(&TileCoord::new("roads", 7, 68, 44), 4);
        assert_ne!(a, b);
    }

    /// The whole scheme rests on the unbuffered single-tile block covering exactly the ground that
    /// the equivalent `GetTile` covers. z0 is the one case we can assert in closed form.
    #[test]
    fn an_unbuffered_single_tile_block_is_the_whole_world_at_z0() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 0, 0, 0), 1);
        let (min_x, min_y, max_x, max_y) = meta.bbox(0);
        assert!((min_x + MERCATOR_HALF_SPAN).abs() < 1e-6);
        assert!((min_y + MERCATOR_HALF_SPAN).abs() < 1e-6);
        assert!((max_x - MERCATOR_HALF_SPAN).abs() < 1e-6);
        assert!((max_y - MERCATOR_HALF_SPAN).abs() < 1e-6);
    }

    /// Row 0 is the *north* edge. Getting this upside down is the classic XYZ-vs-TMS bug and it
    /// would produce a plausible-looking map of the wrong hemisphere.
    #[test]
    fn row_zero_is_the_northern_edge() {
        let north = MetaCoord::containing(&TileCoord::new("countries", 2, 0, 0), 1).bbox(0);
        let south = MetaCoord::containing(&TileCoord::new("countries", 2, 0, 3), 1).bbox(0);
        assert!((north.3 - MERCATOR_HALF_SPAN).abs() < 1e-6, "top row must touch the north edge");
        assert!((south.1 + MERCATOR_HALF_SPAN).abs() < 1e-6, "bottom row must touch the south edge");
        assert!(north.1 > south.3, "row 0 must sit entirely north of the last row");
    }

    #[test]
    fn the_buffer_grows_the_extent_symmetrically_by_whole_tiles() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 3, 4, 4), 2);
        let plain = meta.bbox(0);
        let padded = meta.bbox(TILE_PX); // exactly one tile of buffer
        let span = 2.0 * MERCATOR_HALF_SPAN / 8.0;
        for (a, b, sign) in [
            (padded.0, plain.0, -1.0),
            (padded.1, plain.1, -1.0),
            (padded.2, plain.2, 1.0),
            (padded.3, plain.3, 1.0),
        ] {
            assert!((a - (b + sign * span)).abs() < 1e-6, "expected {b} shifted by {sign} tile, got {a}");
        }
        assert_eq!(meta.size_px(TILE_PX), 2 * 256 + 512);
    }

    #[test]
    fn crop_origins_walk_the_block_and_reject_outsiders() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 4);
        assert_eq!(meta.crop_origin(&TileCoord::new("countries", 7, 68, 44), 128), Some((128, 128)));
        assert_eq!(meta.crop_origin(&TileCoord::new("countries", 7, 71, 47), 128), Some((128 + 768, 128 + 768)));
        assert_eq!(meta.crop_origin(&TileCoord::new("countries", 7, 72, 44), 128), None);
        assert_eq!(meta.crop_origin(&TileCoord::new("countries", 7, 67, 44), 128), None);
        assert_eq!(meta.crop_origin(&TileCoord::new("countries", 8, 68, 44), 128), None);
        assert_eq!(meta.crop_origin(&TileCoord::new("roads", 7, 68, 44), 128), None);
    }

    #[test]
    fn tiles_yields_the_whole_block_once_each() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 4);
        let tiles: Vec<_> = meta.tiles().collect();
        assert_eq!(tiles.len(), 16);
        let unique: std::collections::HashSet<_> = tiles.iter().collect();
        assert_eq!(unique.len(), 16);
        assert!(tiles.iter().all(|t| meta.crop_origin(t, 0).is_some()));
    }

    #[test]
    fn wms_query_asks_for_the_upstream_layer_with_a_transparent_background() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 3, 4, 3), 2);
        let query = meta.wms_query("simple-countries", 128);
        let get = |key: &str| query.iter().find(|(k, _)| *k == key).map(|(_, v)| v.as_str()).unwrap();
        assert_eq!(get("LAYERS"), "simple-countries");
        assert_eq!(get("TRANSPARENT"), "TRUE");
        assert_eq!(get("SRS"), "EPSG:3857");
        assert_eq!(get("VERSION"), "1.1.1");
        assert_eq!(get("WIDTH"), "768");
        assert_eq!(get("HEIGHT"), "768");
        assert!(!query.iter().any(|(k, _)| *k == "DPI"), "DPI must not be sent; QGIS derives it");
    }

    /// Each sliced tile must carry the pixels from its own quadrant, buffer discarded. The source
    /// image encodes the block coordinate in the pixel value so a transposed or off-by-a-buffer
    /// crop cannot pass.
    #[test]
    fn slice_cuts_the_buffer_off_and_keeps_each_tile_in_its_place() {
        let buffer = 8;
        let meta = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 2);
        let side = meta.size_px(buffer);
        let source = png(side, side, |x, y| {
            if x < buffer || y < buffer || x >= side - buffer || y >= side - buffer {
                [255, 0, 0, 255] // buffer: must never appear in any tile
            } else {
                [((x - buffer) / TILE_PX) as u8, ((y - buffer) / TILE_PX) as u8, 7, 255]
            }
        });

        let tiles = meta.slice(&source, buffer).unwrap();
        assert_eq!(tiles.len(), 4);
        for (coord, bytes) in tiles {
            let decoded = image::load_from_memory(&bytes).unwrap().into_rgba8();
            assert_eq!(decoded.dimensions(), (TILE_PX, TILE_PX));
            let expected = image::Rgba([(coord.x - meta.mx) as u8, (coord.y - meta.my) as u8, 7, 255]);
            for corner in [(0, 0), (255, 0), (0, 255), (255, 255)] {
                assert_eq!(
                    *decoded.get_pixel(corner.0, corner.1),
                    expected,
                    "{coord} corner {corner:?} came from the wrong part of the block"
                );
            }
        }
    }

    #[test]
    fn slice_rejects_an_image_that_is_not_the_size_we_asked_for() {
        let meta = MetaCoord::containing(&TileCoord::new("countries", 7, 68, 44), 2);
        // QGIS silently clamps oversized requests against QGIS_SERVER_MAX_WIDTH; cropping such an
        // answer would produce a plausible tile of the wrong ground.
        let source = png(256, 256, |_, _| [0, 0, 0, 255]);
        assert!(matches!(meta.slice(&source, 8), Err(SliceError::WrongSize { .. })));
        assert!(matches!(meta.slice(b"not a png", 8), Err(SliceError::Decode(_))));
    }
}
