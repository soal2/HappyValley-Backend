"""Dynamic AI teammate generation: Stage A (skeleton), Stage B (persona), Stage C (relay enrichment).

Two-phase latency design:
- Stage A (synchronous): select 2 real whitelist characters + 2 conceptual skeletons,
  persist to ``room_characters`` with real rows ``status=ready`` and conceptual rows
  ``status=skeleton``. Returns member slots for ``stage_state.members``.
- Stage B (background): generate full 5-dimension personas for the 2 conceptual
  characters in parallel, persist ``status=ready``. On failure/timeout, apply a
  reserve fallback persona. Notifies the client via ``team_updated`` WebSocket event.
- Stage C (relay): ``_run_relay`` awaits the Stage B future (``wait_ready``), then
  loads personas from ``room_characters`` and enriches each peer before calling
  ``generate_peer_turn``.

Concurrency invariants:
- The persona authority is the ``room_characters`` table — never written into
  ``stage_state.members`` (which is exposed via snapshots).
- The background task only writes ``room_characters``; it never acquires the room
  lock or touches ``stage_state``/``revision``.
- ``_run_relay`` holds the room lock for at most ``CHARACTER_GENERATION_WAIT_SECONDS``
  while awaiting the future; on timeout or missing future it applies synchronous
  fallback (no LLM, <50ms).
"""

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import (
    AgentRuntime,
    ConceptualSkeleton,
    TeamSkeletonResult,
)
from app.config import get_settings
from app.container import Container
from app.database import async_session_factory
from app.models.character import Character
from app.models.room import ActivityRoom
from app.models.room_character import RoomCharacter
from app.models.user import User, UserProfile
from app.realtime import connection_manager
from app.services import matching_service

logger = structlog.get_logger()

# All 16 MBTI types, used as a pool when reassigning conflicting types.
_MBTI_POOL = [
    "INTJ", "INTP", "INFJ", "INFP", "ISTJ", "ISFJ", "ISTP", "ISFP",
    "ENTJ", "ENTP", "ENFJ", "ENFP", "ESTJ", "ESFJ", "ESTP", "ESFP",
]

# Therapeutic role ratio: 2 been-there + 1 different-perspective + 1 quiet-resonator.
_ROLE_QUOTA = ["been-there", "been-there", "different-perspective", "quiet-resonator"]

_VOICE_IDS = ["zhu", "xiaoli", "zhang", "li"]


# ---------------------------------------------------------------------------
# MBTI → appearance mapping
# ---------------------------------------------------------------------------

_appearance_cache: dict[str, str] | None = None


def _load_mbti_appearance() -> dict[str, str]:
    global _appearance_cache
    if _appearance_cache is not None:
        return _appearance_cache
    path = Path(get_settings().MBTI_APPEARANCE_CONFIG_PATH)
    with path.open(encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    _appearance_cache = {str(k).upper(): str(v) for k, v in (raw.get("mapping") or {}).items()}
    return _appearance_cache


def _appearance_for(mbti: str) -> str:
    mapping = _load_mbti_appearance()
    key = mbti.upper()
    if key in mapping:
        return mapping[key]
    return f"mbti_{mbti.lower()}"


# ---------------------------------------------------------------------------
# Task registry — in-process bookkeeping for background Stage B tasks
# ---------------------------------------------------------------------------

_tasks: dict[str, asyncio.Task[None]] = {}
_futures: dict[str, asyncio.Future[None]] = {}
_registry_lock = asyncio.Lock()


# Separate registry for the AI_INTRO + PAINTING_TURN pre-roll background task.
# Not shared with Stage B's _tasks because the two have different lifecycles
# (Stage B may finish before pre-roll, and either may be retried independently).
_preroll_tasks: dict[str, asyncio.Task[None]] = {}
_preroll_lock = asyncio.Lock()


async def start_for_room(room_id: str) -> None:
    """Idempotently launch the Stage B background task for a room.

    Creates a future (resolved when generation completes or fails) and an
    asyncio task guarded against garbage collection by storing the reference.
    """
    async with _registry_lock:
        existing = _tasks.get(room_id)
        if existing is not None and not existing.done():
            return  # already running
        if room_id not in _futures or _futures[room_id].done():
            _futures[room_id] = asyncio.get_event_loop().create_future()
        task = asyncio.create_task(_generate_conceptual_personas(room_id))
        _tasks[room_id] = task


async def start_ai_intro_and_preroll(room_id: str) -> None:
    """Idempotently launch the AI_INTRO opening + PAINTING_TURN pre-roll background task.

    Lets the HTTP request that creates the room return immediately after Stage A;
    the opening message and the first few AI peer turns are then streamed to the
    room's WebSocket via ``connection_manager.send_to_room`` as the task progresses.
    """
    async with _preroll_lock:
        existing = _preroll_tasks.get(room_id)
        if existing is not None and not existing.done():
            return  # already running
        task = asyncio.create_task(_run_ai_intro_and_preroll(room_id))
        _preroll_tasks[room_id] = task


async def _run_ai_intro_and_preroll(room_id: str) -> None:
    """Background wrapper: load agent_runtime, invoke activity_service pre-roll, cleanup."""
    # Lazy import to avoid a circular dependency: activity_service imports
    # character_generation_service at module load.
    from app.services import activity_service

    try:
        agent_runtime = Container.get().agent_runtime
        logger.info("ai_intro_preroll_started", room_id=room_id)
        await activity_service.run_ai_intro_and_preroll(room_id, agent_runtime)
        logger.info("ai_intro_preroll_completed", room_id=room_id)
    except Exception:
        logger.exception("ai_intro_preroll_failed", room_id=room_id)
        try:
            await connection_manager.send_to_room(
                room_id,
                {
                    "type": "error",
                    "error": "pre_roll_failed",
                    "recoverable": True,
                },
            )
        except Exception:
            logger.exception("ai_intro_preroll_error_emit_failed", room_id=room_id)
    finally:
        async with _preroll_lock:
            _preroll_tasks.pop(room_id, None)


async def wait_ready(room_id: str, timeout: float) -> bool:
    """Await the Stage B future for a room.

    Returns True if the future resolved within ``timeout``. Returns False on
    timeout, missing future (e.g. after process restart), or exception — the
    caller must then apply synchronous fallback for any non-ready rows.
    """
    async with _registry_lock:
        future = _futures.get(room_id)
    if future is None:
        return False
    try:
        await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        return True
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return False
    except Exception:
        return False


async def cancel_all() -> None:
    """Cancel all in-flight generation tasks (application shutdown)."""
    async with _registry_lock:
        tasks = list(_tasks.values())
        futures = list(_futures.values())
    async with _preroll_lock:
        preroll_tasks = list(_preroll_tasks.values())
    for future in futures:
        if not future.done():
            future.cancel()
    for task in tasks:
        task.cancel()
    for task in preroll_tasks:
        task.cancel()
    for task in tasks + preroll_tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    async with _registry_lock:
        _tasks.clear()
        _futures.clear()
    async with _preroll_lock:
        _preroll_tasks.clear()


# ---------------------------------------------------------------------------
# Stage A — synchronous skeleton selection
# ---------------------------------------------------------------------------


async def _try_select_skeletons(
    agent_runtime: AgentRuntime,
    mood: str,
    user_age: int,
    user_profile: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> TeamSkeletonResult:
    """Call the LLM ``select_team_skeletons`` with one retry."""
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return await agent_runtime.select_team_skeletons(
                mood, user_age, user_profile, candidates
            )
        except Exception as error:
            last_error = error
            logger.warning(
                "character_gen.stage_a_attempt_failed",
                error_type=type(error).__name__,
                error=str(error),
            )
    raise last_error or RuntimeError("select_team_skeletons failed")  # type: ignore[misc]


def _rule_based_skeleton(whitelist: list[Character]) -> TeamSkeletonResult:
    """Deterministic skeleton when the LLM is unavailable or mood is crisis-empty."""
    been_there = [c for c in whitelist if c.personality_type == "been-there"]
    reals = been_there[:2] if len(been_there) >= 2 else whitelist[:2]
    real_ids = [c.id for c in reals]
    used_mbti = {c.mbti for c in reals if c.mbti}

    conceptuals: list[ConceptualSkeleton] = []
    for role, name in [("different-perspective", "星河旅人"), ("quiet-resonator", "晨雾小鹿")]:
        mbti = next(m for m in _MBTI_POOL if m not in used_mbti)
        used_mbti.add(mbti)
        conceptuals.append(
            ConceptualSkeleton(
                name=name,
                era="当代",
                mbti=mbti,
                role=role,
                one_line_concept=f"一位{role}视角的虚构伙伴",
            )
        )
    return TeamSkeletonResult(real_character_ids=real_ids, conceptuals=conceptuals)


def _validate_and_fix_skeleton(
    result: TeamSkeletonResult, whitelist: list[Character]
) -> TeamSkeletonResult:
    """Validate LLM output and deterministically fix role ratio and MBTI conflicts.

    Real character roles are fixed (from the whitelist); conceptual roles are
    recomputed to complete the 2/1/1 therapeutic ratio. MBTI conflicts are
    resolved by reassigning from the pool.
    """
    char_by_id = {c.id: c for c in whitelist}

    valid_reals = [rid for rid in result.real_character_ids if rid in char_by_id]
    if len(valid_reals) < 2:
        return _rule_based_skeleton(whitelist)

    real_chars = [char_by_id[rid] for rid in valid_reals[:2]]
    used_mbti: set[str] = {c.mbti for c in real_chars if c.mbti}

    # Compute the two conceptual roles needed to complete 2 been-there + 1 + 1.
    needed_roles = list(_ROLE_QUOTA)
    for char in real_chars:
        if char.personality_type in needed_roles:
            needed_roles.remove(char.personality_type)
    conceptual_roles = needed_roles  # exactly 2 remaining

    # Ensure exactly 2 conceptuals.
    raw_conceptuals = list(result.conceptuals[:2])
    while len(raw_conceptuals) < 2:
        raw_conceptuals.append(
            ConceptualSkeleton(
                name=f"概念伙伴{len(raw_conceptuals) + 1}",
                era="当代",
                mbti="INTJ",
                role="been-there",
                one_line_concept="一位虚构伙伴",
            )
        )

    appearance = _load_mbti_appearance()
    fixed: list[ConceptualSkeleton] = []
    for idx, conceptual in enumerate(raw_conceptuals):
        role = conceptual_roles[idx] if idx < len(conceptual_roles) else "been-there"
        mbti = (conceptual.mbti or "").upper()
        if mbti not in appearance or mbti in used_mbti:
            mbti = next((m for m in _MBTI_POOL if m not in used_mbti), "INTJ")
        used_mbti.add(mbti)
        name = (conceptual.name or f"概念伙伴{idx + 1}")[:20]
        era = (conceptual.era or "当代")[:50]
        concept = (conceptual.one_line_concept or f"一位{role}视角的虚构伙伴")[:60]
        fixed.append(
            ConceptualSkeleton(name=name, era=era, mbti=mbti, role=role, one_line_concept=concept)
        )

    return TeamSkeletonResult(
        real_character_ids=[c.id for c in real_chars], conceptuals=fixed
    )


def _member_slot(rc: RoomCharacter, voice_id: str) -> dict[str, Any]:
    """Build a stable member slot for ``stage_state.members`` (no persona fields)."""
    return {
        "id": rc.id,
        "type": "ai_peer",
        "name": rc.name,
        "era": rc.era,
        "mbti": rc.mbti,
        "role": rc.role,
        "one_line_concept": rc.one_line_concept,
        "appearance_id": rc.appearance_id,
        "voice_id": voice_id,
        "source": rc.source,
        "fictional": rc.source != "real",
        "persona_status": rc.status,
    }


async def select_and_persist_skeletons(
    room: ActivityRoom,
    user: User,
    mood: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
) -> list[dict[str, Any]]:
    """Stage A: select 2 real + 2 conceptual skeletons and persist to ``room_characters``.

    Returns the 4 member slot dicts (without persona) for ``stage_state.members``.
    ``mood`` is the match mood (empty string for crisis — skips LLM, uses rule skeleton).
    """
    await matching_service.sync_characters(db)

    char_result = await db.execute(select(Character).where(Character.fictional.is_(False)))
    whitelist = list(char_result.scalars())
    if len(whitelist) < 2:
        raise RuntimeError("白名单真实人物不足 2 位，无法组队")

    profile_result = await db.execute(
        select(UserProfile).where(UserProfile.user_id == user.id)
    )
    profile = profile_result.scalar_one_or_none()
    user_age = datetime.now(UTC).year - int(profile.birth_year or 2000) if profile else 30
    user_profile: dict[str, Any] = {
        "mbti": profile.mbti if profile else None,
        "birth_year": profile.birth_year if profile else None,
    }

    candidates = [
        {
            "id": c.id,
            "name": c.name,
            "era": c.era,
            "role": c.personality_type,
            "mbti": c.mbti,
            "age_range_start": c.age_range_start,
            "age_range_end": c.age_range_end,
            "topics": c.topics,
        }
        for c in whitelist
    ]

    skeleton: TeamSkeletonResult | None = None
    if mood:  # crisis mood (empty) skips the LLM entirely
        try:
            skeleton = await _try_select_skeletons(
                agent_runtime, mood, user_age, user_profile, candidates
            )
        except Exception as error:
            logger.warning(
                "character_gen.stage_a_llm_failed",
                room_id=room.id,
                error_type=type(error).__name__,
            )
            skeleton = None

    if skeleton is None:
        skeleton = _rule_based_skeleton(whitelist)

    skeleton = _validate_and_fix_skeleton(skeleton, whitelist)

    char_by_id = {c.id: c for c in whitelist}
    members: list[dict[str, Any]] = []
    slot = 0

    for real_id in skeleton.real_character_ids:
        char = char_by_id.get(real_id, whitelist[0])
        rc = RoomCharacter(
            id=str(uuid.uuid4()),
            room_id=room.id,
            slot=slot,
            role=char.personality_type,
            source="real",
            character_id=char.id,
            name=char.name,
            era=char.era,
            mbti=char.mbti or "INTJ",
            appearance_id=char.appearance_id,
            system_prompt_template=char.system_prompt_template,
            speak_style=char.speak_style,
            facts_boundaries=char.facts_boundaries,
            safety_fallback=char.safety_fallback,
            topics=char.topics,
            status="ready",
        )
        db.add(rc)
        members.append(_member_slot(rc, _VOICE_IDS[slot]))
        slot += 1

    for conceptual in skeleton.conceptuals:
        rc = RoomCharacter(
            id=str(uuid.uuid4()),
            room_id=room.id,
            slot=slot,
            role=conceptual.role,
            source="conceptual",
            character_id=None,
            name=conceptual.name,
            era=conceptual.era,
            mbti=conceptual.mbti,
            appearance_id=_appearance_for(conceptual.mbti),
            one_line_concept=conceptual.one_line_concept,
            status="skeleton",
        )
        db.add(rc)
        members.append(_member_slot(rc, _VOICE_IDS[slot]))
        slot += 1

    await db.flush()
    logger.info(
        "character_gen.stage_a_complete",
        room_id=room.id,
        real_count=len(skeleton.real_character_ids),
        conceptual_count=len(skeleton.conceptuals),
    )
    return members


# ---------------------------------------------------------------------------
# Stage B — background persona generation
# ---------------------------------------------------------------------------


async def _load_fallback_character(role: str, db: AsyncSession) -> Character | None:
    """Load a reserve (fictional) character matching the given role."""
    result = await db.execute(
        select(Character)
        .where(Character.fictional.is_(True), Character.personality_type == role)
        .limit(1)
    )
    character = result.scalar_one_or_none()
    if character is not None:
        return character
    # Last resort: any fictional character.
    result = await db.execute(select(Character).where(Character.fictional.is_(True)).limit(1))
    return result.scalar_one_or_none()


async def apply_fallback(rc: RoomCharacter, db: AsyncSession) -> None:
    """Copy a reserve character's persona into the room_character row.

    The ``id`` stays stable (used as ``sender_id``); only display/persona fields
    are overwritten. Sets ``source=fallback, status=ready``.
    """
    fallback_char = await _load_fallback_character(rc.role, db)
    if fallback_char is None:
        # No reserve available — write a minimal safety persona.
        rc.system_prompt_template = rc.system_prompt_template or f"你是{rc.name}，一位温暖的虚构伙伴。"
        rc.speak_style = rc.speak_style or "温和简短。"
        rc.facts_boundaries = rc.facts_boundaries or f"虚构角色{rc.name}。"
        rc.safety_fallback = rc.safety_fallback or "如果此刻很难受，请让专业的人来帮你。"
        rc.topics = rc.topics or ["陪伴", "倾听"]
        rc.source = "fallback"
        rc.status = "ready"
        return

    rc.character_id = fallback_char.id
    rc.name = fallback_char.name
    rc.era = fallback_char.era
    rc.mbti = fallback_char.mbti or rc.mbti
    rc.appearance_id = fallback_char.appearance_id
    rc.system_prompt_template = fallback_char.system_prompt_template
    rc.speak_style = fallback_char.speak_style
    rc.facts_boundaries = fallback_char.facts_boundaries
    rc.safety_fallback = fallback_char.safety_fallback
    rc.topics = fallback_char.topics
    rc.source = "fallback"
    rc.status = "ready"


async def _generate_conceptual_personas(room_id: str) -> None:
    """Stage B: generate full personas for conceptual characters in the background."""
    settings = get_settings()
    timeout = settings.CHARACTER_GENERATION_TIMEOUT_SECONDS
    future = _futures.get(room_id)

    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(RoomCharacter)
                .where(
                    RoomCharacter.room_id == room_id,
                    RoomCharacter.source == "conceptual",
                    RoomCharacter.status == "skeleton",
                )
                .order_by(RoomCharacter.slot)
            )
            conceptuals = list(result.scalars())

            if not conceptuals:
                return  # nothing to generate (already ready or fell back)

            for rc in conceptuals:
                rc.status = "generating"
            await db.commit()

            room = await db.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
            if room is None:
                return

            state = dict(room.stage_state or {})
            mood = str(state.get("mood") or "")

            profile_result = await db.execute(
                select(UserProfile).where(UserProfile.user_id == room.user_id)
            )
            profile = profile_result.scalar_one_or_none()
            user_profile: dict[str, Any] = {
                "mbti": profile.mbti if profile else None,
                "birth_year": profile.birth_year if profile else None,
            }

            agent_runtime = Container.get().agent_runtime

            async def _generate_one(rc: RoomCharacter) -> None:
                skeleton = {
                    "name": rc.name,
                    "era": rc.era,
                    "mbti": rc.mbti,
                    "role": rc.role,
                    "one_line_concept": rc.one_line_concept or "",
                }
                try:
                    persona = await asyncio.wait_for(
                        agent_runtime.generate_conceptual_persona(skeleton, mood, user_profile),
                        timeout=timeout,
                    )
                    rc.system_prompt_template = persona.system_prompt_template
                    rc.speak_style = persona.speak_style
                    rc.facts_boundaries = persona.facts_boundaries
                    rc.safety_fallback = persona.safety_fallback
                    rc.topics = persona.topics
                    rc.five_dim = persona.five_dim.model_dump()
                    rc.generation_meta = persona.generation.model_dump()
                    rc.status = "ready"
                except Exception as error:
                    logger.warning(
                        "character_gen.stage_b_persona_failed",
                        room_id=room_id,
                        character_id=rc.id,
                        error_type=type(error).__name__,
                        error=str(error),
                    )
                    await apply_fallback(rc, db)

            await asyncio.gather(*[_generate_one(rc) for rc in conceptuals])
            await db.commit()

        await connection_manager.send_to_room(room_id, {"type": "team_updated"})
        logger.info("character_gen.stage_b_complete", room_id=room_id)
    except Exception:
        logger.exception("character_gen.stage_b_failed", room_id=room_id)
        try:
            async with async_session_factory() as db:
                stale_result = await db.execute(
                    select(RoomCharacter).where(
                        RoomCharacter.room_id == room_id,
                        RoomCharacter.source == "conceptual",
                        RoomCharacter.status != "ready",
                    )
                )
                for rc in stale_result.scalars():
                    await apply_fallback(rc, db)
                await db.commit()
        except Exception:
            logger.exception("character_gen.stage_b_fallback_failed", room_id=room_id)
    finally:
        if future is not None and not future.done():
            future.set_result(None)


# ---------------------------------------------------------------------------
# Stage C — relay enrichment helpers
# ---------------------------------------------------------------------------


async def load_room_characters(room_id: str, db: AsyncSession) -> list[RoomCharacter]:
    """Load all room_characters for a room, ordered by slot."""
    result = await db.execute(
        select(RoomCharacter)
        .where(RoomCharacter.room_id == room_id)
        .order_by(RoomCharacter.slot)
    )
    return list(result.scalars())


def persona_snapshot(rc: RoomCharacter) -> dict[str, Any]:
    """Extract a plain-dict persona snapshot safe to use after the session closes."""
    return {
        "id": rc.id,
        "name": rc.name,
        "era": rc.era,
        "mbti": rc.mbti,
        "role": rc.role,
        "source": rc.source,
        "appearance_id": rc.appearance_id,
        "system_prompt_template": rc.system_prompt_template,
        "speak_style": rc.speak_style,
        "facts_boundaries": rc.facts_boundaries,
        "safety_fallback": rc.safety_fallback,
        "topics": rc.topics or [],
        "status": rc.status,
    }


def enrich_peer_with_persona(peer: dict[str, Any], persona: dict[str, Any]) -> dict[str, Any]:
    """Inject persona fields into a peer dict for ``generate_peer_turn``.

    Returns a new dict; the original ``peer`` is not mutated. This closes the
    long-standing gap where ``generate_peer_turn`` could not access the persona.
    """
    enriched = dict(peer)
    enriched["name"] = persona["name"]
    enriched["era"] = persona["era"]
    enriched["mbti"] = persona["mbti"]
    enriched["role"] = persona["role"]
    enriched["system_prompt_template"] = persona["system_prompt_template"]
    enriched["speak_style"] = persona["speak_style"]
    enriched["facts_boundaries"] = persona["facts_boundaries"]
    enriched["safety_fallback"] = persona["safety_fallback"]
    enriched["topics"] = persona["topics"]
    return enriched


async def ensure_personas_ready(room_id: str) -> dict[str, dict[str, Any]]:
    """Stage C entry: wait for Stage B, apply synchronous fallback, return persona snapshots.

    Returns a dict mapping ``room_character.id`` → ``persona_snapshot`` dict.
    Uses a fresh session to see the background task's committed writes (avoids
    SQLite transaction-snapshot staleness when the caller's session began before
    the background commit).
    """
    settings = get_settings()
    await wait_ready(room_id, settings.CHARACTER_GENERATION_WAIT_SECONDS)

    async with async_session_factory() as db:
        characters = await load_room_characters(room_id, db)
        for rc in characters:
            if rc.status != "ready":
                await apply_fallback(rc, db)
        await db.commit()
        return {rc.id: persona_snapshot(rc) for rc in characters}


async def cleanup_stale_generations(db: AsyncSession) -> int:
    """Maintenance: fall back any room_characters stuck in skeleton/generating past the deadline.

    Only touches rows whose ``updated_at`` is older than
    ``CHARACTER_GENERATION_STALE_SECONDS`` AND whose room has no in-flight task.
    Returns the count of rows recovered.
    """
    settings = get_settings()
    cutoff = datetime.now(UTC) - _stale_timedelta(settings.CHARACTER_GENERATION_STALE_SECONDS)
    result = await db.execute(
        select(RoomCharacter)
        .where(
            RoomCharacter.status.in_(["skeleton", "generating"]),
            RoomCharacter.updated_at < cutoff,
        )
    )
    recovered = 0
    for rc in result.scalars():
        async with _registry_lock:
            has_task = rc.room_id in _tasks and not _tasks[rc.room_id].done()
        if has_task:
            continue  # still running — let the task resolve naturally
        await apply_fallback(rc, db)
        recovered += 1
    if recovered:
        logger.info("character_gen.stale_recovered", count=recovered)
    return recovered


def _stale_timedelta(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)
