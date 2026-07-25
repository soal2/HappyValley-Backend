"""Fake (demo) agent runtime with deterministic Chinese responses."""

import structlog

from app.agents.base import (
    AgentRuntime,
    BigFive,
    CognitiveStyle,
    ConceptualPersonaResult,
    ConceptualSkeleton,
    ExpressionStyle,
    FiveDimPersona,
    GenerationMeta,
    HistoricalContext,
    Motivation,
    RankedMatch,
    ReflectionAnswerResult,
    SummaryResult,
    TeamSkeletonResult,
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

    async def select_team_skeletons(
        self,
        user_mood: str,
        user_age: int,
        user_profile: dict[str, object],
        whitelist_candidates: list[dict[str, object]],
    ) -> TeamSkeletonResult:
        """Deterministic Stage A: first 2 whitelist candidates + 2 fixed conceptuals."""
        self._record("select_team_skeletons")
        logger.info("fake_runtime.select_team_skeletons", mode="test")
        real = whitelist_candidates[:2]
        real_ids = [str(candidate["id"]) for candidate in real]
        real_roles = [str(candidate.get("role", "been-there")) for candidate in real]
        used_mbti = {str(candidate.get("mbti", "")) for candidate in real}

        # Complete the 2 been-there + 1 different-perspective + 1 quiet-resonator ratio.
        needed_roles = ["been-there", "been-there", "different-perspective", "quiet-resonator"]
        for role in real_roles:
            if role in needed_roles:
                needed_roles.remove(role)
        conceptual_roles = needed_roles  # 2 remaining roles

        names = [("星河旅人", "当代"), ("晨雾小鹿", "当代")]
        mbti_pool = ["INTJ", "ENTJ", "ISTP", "ESFP", "ENTP", "ISFJ", "ESTP", "ENFP"]
        conceptuals: list[ConceptualSkeleton] = []
        for index, (name, era) in enumerate(names):
            mbti = next(mbti for mbti in mbti_pool if mbti not in used_mbti)
            used_mbti.add(mbti)
            conceptuals.append(
                ConceptualSkeleton(
                    name=name,
                    era=era,
                    mbti=mbti,
                    role=conceptual_roles[index],
                    one_line_concept=f"一位{conceptual_roles[index]}视角的虚构伙伴（demo）",
                )
            )
        return TeamSkeletonResult(real_character_ids=real_ids, conceptuals=conceptuals)

    async def generate_conceptual_persona(
        self,
        skeleton: dict[str, object],
        user_mood: str,
        user_profile: dict[str, object],
    ) -> ConceptualPersonaResult:
        """Deterministic Stage B: fixed 5-dimension persona for a conceptual character."""
        self._record("generate_conceptual_persona")
        logger.info("fake_runtime.generate_conceptual_persona", mode="test")
        name = str(skeleton.get("name", "概念伙伴"))
        role = str(skeleton.get("role", "been-there"))
        return ConceptualPersonaResult(
            five_dim=FiveDimPersona(
                big_five=BigFive(
                    openness=0.75,
                    conscientiousness=0.6,
                    extraversion=0.5,
                    agreeableness=0.8,
                    neuroticism=0.4,
                ),
                cognitive_style=CognitiveStyle(
                    decision_making="直觉与感受并重",
                    risk_taking="谨慎但愿意尝试新事物",
                ),
                motivation=Motivation(
                    intrinsic=["对美的追求", "理解他人的渴望"],
                    extrinsic=["被认可的温暖"],
                ),
                expression_style=ExpressionStyle(
                    sentence_length="简短",
                    humor="温和",
                    metaphor_frequency="偶尔",
                ),
                historical_context=HistoricalContext(
                    era=str(skeleton.get("era", "当代")),
                    profession="艺术爱好者",
                    cultural_background="现代都市",
                    key_events=["开始接触绘画疗愈"],
                ),
            ),
            system_prompt_template=(
                f'你是{name}，一位{role}视角的虚构伙伴。以简短真诚的方式回应，不超过3句话。用"你"称呼对方。'
            ),
            speak_style="温和简短，真诚自然。",
            facts_boundaries=f"虚构角色{name}，不涉及真实历史事件或人物。",
            safety_fallback="我听到你了。如果此刻很难受，请让专业的人来帮你，这不是软弱。",
            topics=["创作与感受", "安静的力量", "被理解的温暖"],
            generation=GenerationMeta(
                source="test", model="fake-test-model", request_id=f"fake-persona-{name}"
            ),
        )
