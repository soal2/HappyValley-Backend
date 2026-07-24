"""Fake (demo) agent runtime with deterministic Chinese responses."""

import structlog

from app.agents.base import AgentRuntime, RankedMatch, SummaryResult

logger = structlog.get_logger()


def _candidate_age(candidate: dict[str, object], key: str, default: int) -> int:
    value = candidate.get(key, default)
    return value if isinstance(value, int) else default


class FakeAgentRuntime(AgentRuntime):
    """Deterministic demo runtime – no external API calls."""

    async def rank_candidates(
        self, user_mood: str, user_age: int, candidates: list[dict[str, object]]
    ) -> list[RankedMatch]:
        logger.info("fake_runtime.rank_candidates", mode="demo")
        # Rank by age proximity
        sorted_candidates = sorted(
            candidates,
            key=lambda c: abs(
                (
                    (
                        _candidate_age(c, "age_range_start", 20)
                        + _candidate_age(c, "age_range_end", 30)
                    )
                    / 2
                )
                - user_age
            ),
        )
        results = []
        for i, candidate in enumerate(sorted_candidates[:3]):
            results.append(
                RankedMatch(
                    character_id=str(candidate.get("id", f"char_{i}")),
                    rank=i + 1,
                    reason="年龄相近，能够理解你当前的心情（demo模式）",
                )
            )
        return results

    async def generate_ai_turns(
        self,
        context: str,
        characters: list[dict[str, object]],
        previous_contributions: list[str],
    ) -> list[str]:
        logger.info("fake_runtime.generate_ai_turns", mode="demo")
        return [
            f"{character['name']}增加了一束从云间落下的暖光（demo 模式）"
            for character in characters
        ]

    async def generate_follow_up(self, context: str, user_sharing: str) -> str:
        logger.info("fake_runtime.generate_follow_up", mode="demo")
        return "谢谢你愿意分享这些。能告诉我更多关于这种感受是什么时候开始的吗？"

    async def generate_summary(
        self, context: str, transcript: list[dict[str, object]]
    ) -> SummaryResult:
        logger.info("fake_runtime.generate_summary", mode="demo")
        return SummaryResult(
            summary_text="在这次交流中，你勇敢地分享了自己的感受，并得到了温暖的回应。每一次倾诉都是疗愈的开始。",
            encouragement_text="你今天做得很棒！记住，照顾好自己的情绪和照顾身体一样重要。明天又是新的一天。🌱",
        )

    async def generate_postcard_text(self, context: str, user_sharing: str) -> str:
        logger.info("fake_runtime.generate_postcard_text", mode="demo")
        return "愿这张明信片带给你温暖和力量。你值得被温柔对待。🌸"
