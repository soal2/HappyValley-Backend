"""Fake (demo) agent runtime with deterministic Chinese responses."""

import structlog

from app.agents.base import (
    AgentRuntime,
    GenerationMeta,
    RankedMatch,
    ReflectionAnswerResult,
    SummaryResult,
    TextGenerationResult,
)

logger = structlog.get_logger()


def _candidate_age(candidate: dict[str, object], key: str, default: int) -> int:
    value = candidate.get(key, default)
    return value if isinstance(value, int) else default


class FakeAgentRuntime(AgentRuntime):
    """Deterministic demo runtime – no external API calls."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    def _record(self, operation: str) -> None:
        self.calls[operation] = self.calls.get(operation, 0) + 1

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
        for i, candidate in enumerate(sorted_candidates):
            results.append(
                RankedMatch(
                    character_id=str(candidate.get("id", f"char_{i}")),
                    rank=i + 1,
                    reason="年龄相近，能够理解你当前的心情（demo模式）",
                )
            )
        return results

    def _text(self, text: str, request_id: str) -> TextGenerationResult:
        return TextGenerationResult(
            text=text,
            generation=GenerationMeta(
                source="test", model="fake-test-model", request_id=request_id
            ),
        )

    async def generate_opening(
        self,
        members: list[dict[str, object]],
        activity_rules: list[str],
        first_speaker: dict[str, object],
        max_duration_seconds: int = 20,
    ) -> TextGenerationResult:
        self._record("opening")
        return self._text(
            "欢迎大家带着各自的心情来到绘画疗愈空间。我们会用接龙共同完成一幅画，"
            f"结束后你将获得专属明信片。请{first_speaker['name']}先用文字描述希望画面出现的样子。",
            "fake-opening",
        )

    async def generate_peer_turn(
        self,
        character: dict[str, object],
        previous_contributions: list[str],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult:
        self._record("peer_turn")
        logger.info("fake_runtime.generate_peer_turn", mode="test")
        return self._text(
            f"我想在这幅画上加上一束从云间落下的暖光，照亮{character['name']}身旁的草地",
            f"fake-peer-{character['id']}",
        )

    async def generate_facilitator_response(
        self, context: str, user_sharing: str
    ) -> TextGenerationResult:
        self._record("facilitator_response")
        logger.info("fake_runtime.generate_facilitator_response", mode="test")
        return self._text(
            "我听见你认真看见了自己的感受。你愿意把它表达出来，这份真诚和觉察很珍贵。",
            "fake-facilitator-response",
        )

    async def generate_reflection_question(
        self,
        question_index: int,
        intent: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult:
        self._record("reflection_question")
        questions = [
            "看着我们共同完成的画，你此刻有什么感受？",
            "画下那一笔时，你心里想的是什么，为什么想这样画？",
            "此刻，你想对其他成员说些什么？",
            "听完大家的反馈，你现在的心情如何？",
        ]
        return self._text(questions[question_index], f"fake-question-{question_index}")

    async def generate_reflection_answer(
        self,
        character: dict[str, object],
        question: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> ReflectionAnswerResult:
        self._record("reflection_answer")
        referenced = [str(item["id"]) for item in transcript[-1:] if item.get("id")]
        return ReflectionAnswerResult(
            text="看到大家把不同的想法放在一起，我感到很温暖，也得到了一种新的视角。",
            positive_feedback_type="multiple_perspectives",
            referenced_message_ids=referenced,
            generation=GenerationMeta(
                source="test",
                model="fake-test-model",
                request_id=f"fake-reflection-{character['id']}-{len(transcript)}",
            ),
        )

    async def generate_summary(
        self,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 20,
    ) -> SummaryResult:
        self._record("summary")
        logger.info("fake_runtime.generate_summary", mode="demo")
        return SummaryResult(
            summary_text="在这次交流中，你勇敢地分享了自己的感受，并得到了温暖的回应。每一次倾诉都是疗愈的开始。",
            encouragement_text="你今天认真看见并表达了自己的感受。愿这份觉察继续陪伴你。",
            generation=GenerationMeta(
                source="test", model="fake-test-model", request_id="fake-summary"
            ),
        )

    async def generate_postcard_text(self, context: str, user_sharing: str) -> TextGenerationResult:
        self._record("postcard_text")
        logger.info("fake_runtime.generate_postcard_text", mode="test")
        return self._text(
            "愿这张明信片提醒你，你一直拥有发现微光的能力。",
            "fake-postcard",
        )
