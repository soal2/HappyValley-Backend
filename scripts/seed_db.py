"""Seed database with characters from config/characters.yaml.

Usage: python -m scripts.seed_db
"""

import asyncio
from pathlib import Path

from app.database import async_session_factory
from app.services.matching_service import sync_characters

project_root = Path(__file__).resolve().parent.parent


async def seed_characters() -> None:
    """Load characters from YAML, insert into DB if not existing (idempotent)."""
    async with async_session_factory() as session:
        count = await sync_characters(session, str(project_root / "config" / "characters.yaml"))
        await session.commit()
    print(f"人物配置已幂等同步，共 {count} 条。")


def main() -> None:
    """Entry point."""
    print("=== 心晴谷 角色种子数据 ===\n")
    asyncio.run(seed_characters())


if __name__ == "__main__":
    main()
