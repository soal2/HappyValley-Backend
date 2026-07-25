"""Local filesystem media storage implementation."""

import os
import uuid
from pathlib import Path

import structlog

from app.config import get_settings
from app.media.storage import MediaStorage

logger = structlog.get_logger()


class LocalMediaStorage(MediaStorage):
    """Store media files on local filesystem with path traversal prevention."""

    def __init__(self) -> None:
        settings = get_settings()
        self._base_path = Path(settings.MEDIA_STORAGE_PATH).resolve()
        self._base_path.mkdir(parents=True, exist_ok=True)

    def _validate_path(self, path: str) -> Path:
        """Validate path to prevent traversal attacks."""
        if ".." in path:
            raise PermissionError("Path traversal detected")
        resolved = (self._base_path / path).resolve()
        if not resolved.is_relative_to(self._base_path):
            raise PermissionError("Path traversal detected")
        return resolved

    async def save(self, data: bytes, owner_id: str, room_id: str, category: str) -> str:
        """Save file with UUID-based name: {category}/{owner_id}_{uuid4()}.png"""
        category_dir = self._base_path / category
        category_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{owner_id}_{uuid.uuid4().hex}.png"
        relative_path = f"{category}/{filename}"
        full_path = category_dir / filename

        with open(full_path, "wb") as f:
            f.write(data)

        logger.info("local_storage.saved", path=relative_path, size=len(data))
        return relative_path

    async def read(self, path: str, requester_id: str) -> bytes:
        """Read file with permission check (owner_id must match)."""
        resolved = self._validate_path(path)

        # Extract owner_id from filename pattern: {owner_id}_{uuid}.png
        filename = resolved.name
        parts = filename.rsplit("_", 1)
        if len(parts) >= 2:
            embedded_owner = parts[0]
            if embedded_owner != requester_id:
                raise PermissionError(
                    f"Access denied: requester {requester_id} does not own file"
                )

        if not resolved.exists():
            raise FileNotFoundError(f"File not found: {path}")

        with open(resolved, "rb") as f:
            return f.read()

    async def delete(self, path: str) -> None:
        """Delete file if it exists."""
        resolved = self._validate_path(path)
        if resolved.exists():
            os.remove(resolved)
            logger.info("local_storage.deleted", path=path)
        else:
            logger.warning("local_storage.delete_not_found", path=path)
