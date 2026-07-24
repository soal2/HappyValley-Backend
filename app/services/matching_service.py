"""Versioned character synchronization and rule-constrained matching."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentRuntime
from app.config import get_settings
from app.domain.matching import MatchingEngine
from app.models.character import Character
from app.models.user import UserProfile


def _load_config(path: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    config_path = Path(path or get_settings().CHARACTERS_CONFIG_PATH)
    with config_path.open(encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    return str(raw["version"]), list(raw["characters"])


async def sync_characters(db: AsyncSession, path: str | None = None) -> int:
    """Idempotently insert or update the reviewed character configuration."""
    version, entries = _load_config(path)
    result = await db.execute(select(Character))
    existing = {(item.name, item.era): item for item in result.scalars()}
    for index, entry in enumerate(entries):
        key = (entry["name"], entry["era"])
        character = existing.get(key)
        if character is None:
            character = Character(name=entry["name"], era=entry["era"])
            db.add(character)
        character.age_range_start = entry["age_range"]["start"]
        character.age_range_end = entry["age_range"]["end"]
        character.personality_type = entry["personality_type"]
        character.topics = entry.get("topics", [])
        character.speak_style = entry.get("speak_style")
        character.facts_boundaries = entry.get("facts_boundaries")
        character.safety_fallback = entry.get("safety_fallback")
        character.system_prompt_template = entry.get("system_prompt_template")
        character.config_version = version
        character.fictional = "虚构" in entry["era"]
        character.appearance_id = entry.get("appearance_id", f"ai_character_{index + 1:02d}")
    await db.flush()
    return len(entries)


def _public_character(character: Character, reason: str) -> dict[str, Any]:
    return {
        "id": character.id,
        "name": character.name,
        "era": character.era,
        "role": character.personality_type,
        "fictional": character.fictional,
        "appearance_id": character.appearance_id,
        "config_version": character.config_version,
        "match_reason": reason,
    }


async def match_team_for_user(
    user_id: str,
    user_mood: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
) -> list[dict[str, Any]]:
    """Select a legal 2+1+1 team; the LLM may rank only within that legal set."""
    await sync_characters(db)
    profile_result = await db.execute(select(UserProfile).where(UserProfile.user_id == user_id))
    profile = profile_result.scalar_one()
    user_age = datetime.now(UTC).year - int(profile.birth_year or 2000)
    character_result = await db.execute(select(Character))
    legal_team = MatchingEngine(list(character_result.scalars())).match_team(user_age)
    roles = [character.personality_type for character in legal_team]
    if len(legal_team) != 4 or roles.count("been-there") != 2:
        raise RuntimeError("人物配置无法组成 2 位过来人 + 1 位不同视角 + 1 位安静共鸣者")

    candidates: list[dict[str, object]] = [
        {
            "id": character.id,
            "name": character.name,
            "role": character.personality_type,
            "age_range_start": character.age_range_start,
            "age_range_end": character.age_range_end,
            "topics": character.topics,
        }
        for character in legal_team
    ]
    reasons = {character.id: "规则匹配：角色功能合法且年龄阶段较接近" for character in legal_team}
    try:
        ranked = await agent_runtime.rank_candidates(user_mood, user_age, candidates)
        valid_ids = {character.id for character in legal_team}
        ordered_ids = [item.character_id for item in sorted(ranked, key=lambda item: item.rank)]
        if set(ordered_ids) == valid_ids and len(ordered_ids) == 4:
            by_id = {character.id: character for character in legal_team}
            legal_team = [by_id[character_id] for character_id in ordered_ids]
            reasons.update({item.character_id: item.reason for item in ranked})
    except Exception:
        pass
    return [_public_character(character, reasons[character.id]) for character in legal_team]


class MatchingService:
    """Compatibility facade used by the seed script."""

    @staticmethod
    def load_characters_from_yaml(path: str) -> list[Character]:
        version, entries = _load_config(path)
        return [
            Character(
                name=entry["name"],
                era=entry["era"],
                age_range_start=entry["age_range"]["start"],
                age_range_end=entry["age_range"]["end"],
                personality_type=entry["personality_type"],
                topics=entry.get("topics", []),
                speak_style=entry.get("speak_style"),
                facts_boundaries=entry.get("facts_boundaries"),
                safety_fallback=entry.get("safety_fallback"),
                system_prompt_template=entry.get("system_prompt_template"),
                config_version=version,
                fictional="虚构" in entry["era"],
                appearance_id=entry.get("appearance_id", f"ai_character_{index + 1:02d}"),
            )
            for index, entry in enumerate(entries)
        ]
