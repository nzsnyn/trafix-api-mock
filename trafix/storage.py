"""Snapshot storage.

In the Laravel app every image fetch is queued (``DownloadLprGateinJob`` and
friends). flow.md §2 warns that without a running ``queue:work`` the photos
silently never arrive — the failure looks like a code bug but is a missing
worker.

Here the download runs on a small thread pool inside the API process, so there
is no separate worker to forget. It is still off the request path: a slow or
dead camera must never hold the barrier shut.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger("storage")

# Naming conventions from the live site (flow.md §9).
LPR_GATEIN_DIR = "lpr/gatein"
LPR_GATEOUT_DIR = "lpr/gateout"
CCTV_DIR = "cctv"


class SnapshotStore:
    """Fetches and stores camera images without blocking the caller."""

    def __init__(self, root: Path, *, timeout: float = 5.0, workers: int = 4) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="snapshot")
        self._lock = threading.Lock()
        self.failures: list[str] = []

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)

    # -- naming ------------------------------------------------------------

    @staticmethod
    def lpr_filename(url: str, *, prefix: str = "CAMIN_LPR") -> str:
        """``CAMIN_LPR_<YmdHisu>.jpg`` — the site's convention."""
        extension = Path(urlparse(url).path).suffix.lstrip(".") or "jpg"
        # %f is microseconds; PHP's 'u' is the same six digits.
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{prefix}_{stamp}.{extension}"

    def relative_path(self, directory: str, filename: str) -> str:
        """The value written to ``cam_in`` / ``cam_out``: ``storage/<dir>/<file>``."""
        return f"storage/{directory}/{filename}"

    def save_upload(self, filename: str, content: bytes) -> str:
        """Store a file the LPR units upload directly.

        Mirrors ``$file->storeAs('public', $fileName)`` in the Laravel
        ``GateInLpr`` / ``GateinImageLpr``: the file lands at the storage root
        and the path recorded in the database is ``storage/<fileName>``, which
        the ``/storage`` mount serves back.
        """
        relative = f"storage/{filename}"
        target = self.root / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        log.info("stored uploaded snapshot %s (%d bytes)", filename, len(content))
        return relative

    # -- fetching ----------------------------------------------------------

    def download_async(self, url: str, directory: str, filename: str) -> str:
        """Queue a download and return the path it will land at.

        The path is recorded immediately so the ticket and the API response
        can reference it, exactly as the Laravel version does.
        """
        relative = self.relative_path(directory, filename)
        self._pool.submit(self._download, url, directory, filename)
        return relative

    def _download(self, url: str, directory: str, filename: str) -> None:
        target = self.root / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = httpx.get(url, timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # A dead exit camera is the norm on site (§7.2), so this is logged
            # loudly but never raised: the gate decision has already been made.
            log.warning("snapshot fetch failed for %s: %s", url, exc)
            with self._lock:
                self.failures.append(f"{url}: {exc}")
            return

        target.write_bytes(response.content)
        log.info("stored snapshot %s (%d bytes)", target.name, len(response.content))

    def capture_cctv_async(
        self,
        *,
        host: str,
        path: str,
        directory: str,
        filename: str,
        username: str | None = None,
        password: str | None = None,
    ) -> str:
        """Fetch a still from an IP camera.

        On site this always 404s: the app requests Dahua's ``snapshot.cgi``
        from Uniview cameras and sends no credentials (flow.md §7.5). Here the
        path is configurable and digest auth is supplied when configured.
        """
        relative = self.relative_path(directory, filename)
        self._pool.submit(
            self._capture_cctv, host, path, directory, filename, username, password
        )
        return relative

    def _capture_cctv(
        self,
        host: str,
        path: str,
        directory: str,
        filename: str,
        username: str | None,
        password: str | None,
    ) -> None:
        url = f"http://{host}{path}"
        auth = httpx.DigestAuth(username, password or "") if username else None
        target = self.root / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = httpx.get(url, timeout=self.timeout, auth=auth)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("cctv capture failed for %s: %s", url, exc)
            with self._lock:
                self.failures.append(f"{url}: {exc}")
            return

        target.write_bytes(response.content)
        log.info("stored cctv snapshot %s", target.name)
