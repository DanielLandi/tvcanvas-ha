"""Photos the phone pushed over the LAN, named by the SHA-256 of their bytes.

Files live in `/config/tvcanvas/images/<sha256>.jpg` (docs/lan-api.md §2). The
in-memory index mirrors the folder, so the rotation and `have` never touch the
disk; every read, write and delete runs in the executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import PHOTOS_BYTES_CAP, PHOTOS_DIR, PHOTOS_FILES_CAP, PHOTOS_GC_AGE_SECONDS
from .models import SHA256_HEX, SyncSpec

_LOGGER = logging.getLogger(__name__)
_SUFFIX = ".jpg"
_TEMP_PREFIX = ".upload-"


class PhotosFull(Exception):
    """Storing this photo would go over the 1 GB / 400 files cap."""


@dataclass
class _Entry:
    size: int
    added: float  # wall clock (time.time()) of the last upload, for GC


def photos_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(*PHOTOS_DIR))


def _valid(sha: str) -> bool:
    return isinstance(sha, str) and SHA256_HEX.fullmatch(sha) is not None


class PhotoStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._dir = photos_dir(hass)
        self._index: dict[str, _Entry] = {}
        self._write_lock = asyncio.Lock()

    async def async_load(self) -> None:
        self._index = await self._hass.async_add_executor_job(self._scan)

    def _scan(self) -> dict[str, _Entry]:
        self._dir.mkdir(parents=True, exist_ok=True)
        index: dict[str, _Entry] = {}
        for path in self._dir.iterdir():
            if path.name.startswith(_TEMP_PREFIX):
                with contextlib.suppress(OSError):
                    path.unlink()  # an upload interrupted by a restart
                continue
            sha = path.name.removesuffix(_SUFFIX)
            if path.suffix != _SUFFIX or not _valid(sha) or not path.is_file():
                continue
            st = path.stat()
            index[sha] = _Entry(size=st.st_size, added=st.st_mtime)
        return index

    def _path(self, sha: str) -> Path:
        if not _valid(sha):  # never build a path from anything but 64 lowercase hex
            raise ValueError("not a sha256")
        return self._dir / f"{sha}{_SUFFIX}"

    # --- what is stored -------------------------------------------------------

    def __contains__(self, sha: object) -> bool:
        return isinstance(sha, str) and sha in self._index

    @property
    def hashes(self) -> list[str]:
        return sorted(self._index)

    @property
    def bytes_used(self) -> int:
        return sum(e.size for e in self._index.values())

    @property
    def files(self) -> int:
        return len(self._index)

    def missing(self, spec: SyncSpec) -> int:
        """How many of the spec's phone photos HA doesn't hold yet."""
        return sum(1 for sha in spec.source.local_hashes if sha not in self._index)

    def would_fit(self, size: int) -> bool:
        return self.files + 1 <= PHOTOS_FILES_CAP and self.bytes_used + size <= PHOTOS_BYTES_CAP

    # --- I/O --------------------------------------------------------------------

    async def async_read(self, sha: str) -> bytes | None:
        """The photo's bytes, or None when HA doesn't have it (anymore)."""
        if sha not in self._index:
            return None
        try:
            return await self._hass.async_add_executor_job(self._path(sha).read_bytes)
        except OSError:
            self._index.pop(sha, None)  # removed behind HA's back: wait for the phone again
            return None

    async def async_put(self, sha: str, data: bytes) -> bool:
        """Store *data* (already verified against *sha*). True when it is new.

        Raises PhotosFull over the cap. Writes a temp file and renames it, so a
        reader never sees half a photo."""
        path = self._path(sha)
        async with self._write_lock:  # cap check and write as one step
            now = time.time()
            entry = self._index.get(sha)
            if entry is not None:
                # Pushed again: restart its GC clock (a push just before its spec).
                entry.added = now
                with contextlib.suppress(OSError):
                    await self._hass.async_add_executor_job(os.utime, path, (now, now))
                return False
            if not self.would_fit(len(data)):
                raise PhotosFull
            await self._hass.async_add_executor_job(self._write, path, data)
            self._index[sha] = _Entry(size=len(data), added=now)
            return True

    def _write(self, path: Path, data: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=self._dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    async def async_gc(self, referenced: Iterable[str]) -> int:
        """Delete photos no spec references once they are 10 minutes old."""
        keep = set(referenced)
        cutoff = time.time() - PHOTOS_GC_AGE_SECONDS
        async with self._write_lock:
            doomed = [
                sha for sha, e in self._index.items() if sha not in keep and e.added <= cutoff
            ]
            if not doomed:
                return 0
            await self._hass.async_add_executor_job(self._delete, [self._path(s) for s in doomed])
            for sha in doomed:
                self._index.pop(sha, None)
        _LOGGER.debug("TVCanvas: deleted %d photos no pin uses", len(doomed))
        return len(doomed)

    @staticmethod
    def _delete(paths: list[Path]) -> None:
        for path in paths:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


async def async_remove_all(hass: HomeAssistant) -> None:
    """The config entry is gone: delete every photo the phones pushed."""
    folder = photos_dir(hass)

    def remove() -> None:
        shutil.rmtree(folder, ignore_errors=True)
        with contextlib.suppress(OSError):
            folder.parent.rmdir()  # /config/tvcanvas, only if nothing else lives there

    await hass.async_add_executor_job(remove)
