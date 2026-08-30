use std::io;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use bytes::Bytes;
use tokio::io::AsyncWriteExt;

use crate::tiles::TileCoord;

/// Orphaned `.tmp` files older than this are swept. Generous relative to a render, so a sweep that
/// races an in-flight write cannot delete a temp file still being written.
const TMP_GRACE: Duration = Duration::from_secs(3_600);

#[derive(Debug, Default, Clone, Copy)]
pub struct SweepReport {
    pub files_live: u64,
    pub bytes_live: u64,
    pub files_removed: u64,
    pub bytes_removed: u64,
    pub dirs_pruned: u64,
    pub tmp_removed: u64,
}

pub struct TileCache {
    root: PathBuf,
    ttl: Duration,
    nonce: AtomicU64,
}

impl TileCache {
    pub fn new(root: PathBuf, ttl: Duration) -> Self {
        Self { root, ttl, nonce: AtomicU64::new(0) }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Returns the tile only if it exists *and* is inside the TTL. A stale file is left on disk for
    /// the janitor rather than unlinked here, so a request never blocks on cleanup.
    pub async fn read_fresh(&self, coord: &TileCoord) -> Option<Bytes> {
        let path = coord.cache_path(&self.root);
        let meta = tokio::fs::metadata(&path).await.ok()?;
        if !is_fresh(&meta, self.ttl) {
            return None;
        }
        match tokio::fs::read(&path).await {
            Ok(bytes) => Some(Bytes::from(bytes)),
            // A concurrent sweep may have unlinked it between the stat and the read. That is a
            // miss, not an error worth surfacing.
            Err(err) => {
                tracing::debug!(%coord, error = %err, "cached tile vanished between stat and read");
                None
            }
        }
    }

    /// Write-then-rename so nginx, which serves this directory directly, can never observe a
    /// half-written PNG. The temp file is a sibling of the target, which keeps the rename on one
    /// filesystem and therefore atomic.
    pub async fn store(&self, coord: &TileCoord, bytes: &Bytes) -> io::Result<()> {
        let final_path = coord.cache_path(&self.root);
        let dir = final_path
            .parent()
            .expect("cache_path always has a {layer}/{z}/{x} parent");
        tokio::fs::create_dir_all(dir).await?;

        let nonce = self.nonce.fetch_add(1, Ordering::Relaxed);
        let tmp_path = coord.tmp_path(&self.root, nonce);

        let write = async {
            let mut file = tokio::fs::File::create(&tmp_path).await?;
            file.write_all(bytes).await?;
            // Without this the rename can be durable while the contents are not, leaving a
            // zero-length tile that nginx will happily serve for the next 120 days.
            file.sync_all().await?;
            drop(file);
            tokio::fs::rename(&tmp_path, &final_path).await
        };

        if let Err(err) = write.await {
            let _ = tokio::fs::remove_file(&tmp_path).await;
            return Err(err);
        }
        Ok(())
    }

    /// Walks the whole cache: unlinks tiles past the TTL and orphaned temp files, prunes the
    /// directories that empties, and tallies what remains for `/stats`.
    pub async fn sweep(&self) -> io::Result<SweepReport> {
        let mut report = SweepReport::default();
        if !tokio::fs::try_exists(&self.root).await.unwrap_or(false) {
            return Ok(report);
        }

        // Explicit stack rather than recursion: an async fn cannot recurse without boxing. Depth is
        // unbounded in principle ({layer}/{z}/{x}/), but the walk below doesn't assume a fixed
        // depth anyway.
        let mut stack = vec![self.root.clone()];
        let mut visited = Vec::new();
        while let Some(dir) = stack.pop() {
            let mut entries = match tokio::fs::read_dir(&dir).await {
                Ok(entries) => entries,
                Err(err) => {
                    tracing::warn!(dir = %dir.display(), error = %err, "skipping unreadable cache dir");
                    continue;
                }
            };
            while let Some(entry) = entries.next_entry().await? {
                let path = entry.path();
                let Ok(meta) = entry.metadata().await else { continue };
                if meta.is_dir() {
                    stack.push(path);
                    continue;
                }

                let name = entry.file_name();
                let name = name.to_string_lossy();
                if name.ends_with(".tmp") {
                    if !is_fresh(&meta, TMP_GRACE) && tokio::fs::remove_file(&path).await.is_ok() {
                        report.tmp_removed += 1;
                    }
                    continue;
                }
                if !name.ends_with(".png") {
                    continue;
                }

                if is_fresh(&meta, self.ttl) {
                    report.files_live += 1;
                    report.bytes_live += meta.len();
                } else if tokio::fs::remove_file(&path).await.is_ok() {
                    report.files_removed += 1;
                    report.bytes_removed += meta.len();
                }
            }
            if dir != self.root {
                visited.push(dir);
            }
        }

        // Deepest first, so emptying {z}/{x} lets {z} be pruned in the same pass.
        visited.sort_by_key(|p| std::cmp::Reverse(p.components().count()));
        for dir in visited {
            // remove_dir only succeeds on an empty directory, which is exactly the condition we
            // want — no need to check first and race ourselves.
            if tokio::fs::remove_dir(&dir).await.is_ok() {
                report.dirs_pruned += 1;
            }
        }

        Ok(report)
    }
}

fn is_fresh(meta: &std::fs::Metadata, ttl: Duration) -> bool {
    let Ok(modified) = meta.modified() else {
        // A filesystem that cannot report mtime cannot support TTL. Treat as fresh rather than
        // throwing the whole cache away on every sweep.
        return true;
    };
    SystemTime::now()
        .duration_since(modified)
        .map(|age| age < ttl)
        .unwrap_or(true) // mtime in the future (clock skew): not expired
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::UNIX_EPOCH;

    fn temp_root(tag: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "dark-basemap-test-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn age_file(path: &Path, age: Duration) {
        let when = SystemTime::now() - age;
        let ft = filetime::FileTime::from_system_time(when);
        filetime::set_file_mtime(path, ft).unwrap();
    }

    #[tokio::test]
    async fn stores_then_reads_back_a_tile() {
        let root = temp_root("roundtrip");
        let cache = TileCache::new(root.clone(), Duration::from_secs(3600));
        let coord = TileCoord::new("countries", 9, 271, 171);
        let payload = Bytes::from_static(b"\x89PNG\r\n\x1a\nfake");

        cache.store(&coord, &payload).await.unwrap();
        assert!(root.join("countries/9/271/171.png").exists(), "must land on the path nginx serves");
        assert_eq!(cache.read_fresh(&coord).await, Some(payload));

        std::fs::remove_dir_all(&root).unwrap();
    }

    #[tokio::test]
    async fn store_leaves_no_temp_files_behind() {
        let root = temp_root("notmp");
        let cache = TileCache::new(root.clone(), Duration::from_secs(3600));
        cache.store(&TileCoord::new("countries", 1, 0, 0), &Bytes::from_static(b"x")).await.unwrap();

        let leftovers: Vec<_> = std::fs::read_dir(root.join("countries/1/0"))
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|n| n.ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "found orphaned temp files: {leftovers:?}");

        std::fs::remove_dir_all(&root).unwrap();
    }

    #[tokio::test]
    async fn a_tile_past_the_ttl_reads_as_a_miss() {
        let root = temp_root("stale");
        let ttl = Duration::from_secs(120 * 86_400);
        let cache = TileCache::new(root.clone(), ttl);
        let coord = TileCoord::new("countries", 3, 4, 5);
        cache.store(&coord, &Bytes::from_static(b"stale")).await.unwrap();

        // Just inside the TTL is still a hit; just outside is a miss.
        age_file(&coord.cache_path(&root), ttl - Duration::from_secs(3600));
        assert!(cache.read_fresh(&coord).await.is_some(), "119d-old tile should still serve");

        age_file(&coord.cache_path(&root), ttl + Duration::from_secs(3600));
        assert!(cache.read_fresh(&coord).await.is_none(), "121d-old tile must not serve");

        std::fs::remove_dir_all(&root).unwrap();
    }

    #[tokio::test]
    async fn sweep_removes_expired_tiles_and_prunes_their_directories() {
        let root = temp_root("sweep");
        let ttl = Duration::from_secs(120 * 86_400);
        let cache = TileCache::new(root.clone(), ttl);

        let fresh = TileCoord::new("countries", 2, 1, 1);
        let stale = TileCoord::new("countries", 7, 60, 40);
        cache.store(&fresh, &Bytes::from_static(b"fresh")).await.unwrap();
        cache.store(&stale, &Bytes::from_static(b"stale")).await.unwrap();
        age_file(&stale.cache_path(&root), ttl + Duration::from_secs(86_400));

        let report = cache.sweep().await.unwrap();
        assert_eq!(report.files_removed, 1);
        assert_eq!(report.files_live, 1);
        assert!(!stale.cache_path(&root).exists());
        assert!(fresh.cache_path(&root).exists(), "sweep must not touch fresh tiles");
        assert!(!root.join("countries/7").exists(), "emptied {{z}}/{{x}} dirs should be pruned");
        assert!(root.join("countries/2/1").exists(), "dirs still holding tiles must survive");

        std::fs::remove_dir_all(&root).unwrap();
    }

    #[tokio::test]
    async fn sweep_clears_orphaned_temp_files_but_spares_recent_ones() {
        let root = temp_root("tmp");
        let cache = TileCache::new(root.clone(), Duration::from_secs(120 * 86_400));
        std::fs::create_dir_all(root.join("5/1")).unwrap();

        let orphan = root.join("5/1/2.png.7.tmp");
        let in_flight = root.join("5/1/3.png.8.tmp");
        std::fs::write(&orphan, b"partial").unwrap();
        std::fs::write(&in_flight, b"partial").unwrap();
        age_file(&orphan, TMP_GRACE + Duration::from_secs(60));

        let report = cache.sweep().await.unwrap();
        assert_eq!(report.tmp_removed, 1);
        assert!(!orphan.exists());
        assert!(in_flight.exists(), "a temp file may still be mid-write; do not delete it");

        std::fs::remove_dir_all(&root).unwrap();
    }

    #[tokio::test]
    async fn sweep_on_a_missing_cache_dir_is_a_no_op() {
        let root = std::env::temp_dir().join(format!("dark-basemap-absent-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let cache = TileCache::new(root, Duration::from_secs(60));
        let report = cache.sweep().await.unwrap();
        assert_eq!(report.files_live, 0);
        assert_eq!(report.files_removed, 0);
    }

    #[test]
    fn a_future_mtime_is_not_treated_as_expired() {
        let root = temp_root("skew");
        let path = root.join("skew.png");
        std::fs::write(&path, b"x").unwrap();
        let future = SystemTime::now() + Duration::from_secs(86_400);
        filetime::set_file_mtime(&path, filetime::FileTime::from_system_time(future)).unwrap();

        let meta = std::fs::metadata(&path).unwrap();
        assert!(is_fresh(&meta, Duration::from_secs(60)), "clock skew must not nuke the cache");
        assert!(meta.modified().unwrap() > UNIX_EPOCH);

        std::fs::remove_dir_all(&root).unwrap();
    }
}
