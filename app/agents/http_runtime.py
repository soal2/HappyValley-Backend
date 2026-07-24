"""HTTP-based agent runtime using OpenAI-compatible API."""

import json

import httpx
import structlog

from app.agents.base import AgentRuntime, RankedMatch, SummaryResult
from app.agents.fake_runtime import FakeAgentRuntime
from app.config import get_settings

logger = structlog.get_logger()


class HttpAgentRuntime(AgentRuntime):
    """Production runtime that calls OpenAI-compatible chat API."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, retry_delay_seconds: float = 1.0
    ) -> None:
        settings = get_settings()
        if not settings.AI_API_KEY or not settings.AI_MODEL or not settings.AI_BASE_URL:
            raise RuntimeError("真实 AI 模式需要 AI_API_KEY、AI_MODEL 和 AI_BASE_URL")
        self._api_key = settings.AI_API_KEY
        self._model = settings.AI_MODEL
        self._base_url = settings.AI_BASE_URL.rstrip("/")
        self._timeout = settings.AI_TIMEOUT_SECONDS
        self._fallback = FakeAgentRuntime()
        self._client = client
        self._retry_delay_seconds = retry_delay_seconds

    async def _call_chat(self, system_prompt: str, user_message: str) -> str:
        """Make a single chat completion call with 1 retry."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.7,
        }
        url = f"{self._base_url}/chat/completions"

        for attempt in range(2):
            try:
                if self._client:
                    response = await self._client.post(url, json=payload, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                return str(data["choices"][0]["message"]["content"])
            except Exception as e:
                logger.warning(
                    "http_runtime.call_failed",
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == 0:
                    import asyncio

                    await asyncio.sleep(self._retry_delay_seconds)
                else:
                    raise
        return ""  # unreachable

    async def rank_candidates(
        self, user_mood: str, user_age: int, candidates: list[dict[str, object]]
    ) -> list[RankedMatch]:
        system_prompt = (
            "你是一个匹配系统。根据用户心情和年龄，对候选AI角色排序。"
            '返回JSON数组，格式为: [{"character_id": "...", "rank": 1, "reason": "..."}]'
        )
        user_msg = json.dumps(
            {"user_mood": user_mood, "user_age": user_age, "candidates": candidates},
            ensure_ascii=False,
        )
        try:
            result = await self._call_chat(system_prompt, user_msg)
            parsed = json.loads(result)
            return [RankedMatch(**item) for item in parsed]
        except Exception as e:
            logger.warning("http_runtime.rank_candidates.fallback", error=str(e))
            return await self._fallback.rank_candidates(user_mood, user_age, candidates)

    async def generate_ai_turns(
        self,
        context: str,
        characters: list[dict[str, object]],
        previous_contributions: list[str],
    ) -> list[str]:
        system_prompt = (
            "你是团体心理互助活动中的AI角色。根据上下文和角色设定，生成每个角色的发言。"
            "返回JSON数组，每个元素是一个字符串（角色的发言）。用温暖、共情的语气。"
        )
        user_msg = json.dumps(
            {
                "context": context,
                "characters": characters,
                "previous_contributions": previous_contributions,
            },
            ensure_ascii=False,
        )
        try:
            result = await self._call_chat(system_prompt, user_msg)
            parsed = json.loads(result)
            if not isinstance(parsed, list) or len(parsed) != len(characters):
                raise ValueError("AI 接龙输出数量与角色数量不一致")
            turns = [str(item).strip() for item in parsed]
            if any(not turn for turn in turns):
                raise ValueError("AI 接龙输出含空文本")
            return turns
        except Exception as e:
            logger.warning("http_runtime.generate_ai_turns.fallback", error=str(e))
            return await self._fallback.generate_ai_turns(
                context, characters, previous_contributions
            )

    async def generate_follow_up(self, context: str, user_sharing: str) -> str:
        system_prompt = (
            "你是一个温暖的心理互助引导者。根据用户分享的内容，生成一个温和的追问或回应。"
            "不要诊断或给建议，只需共情和引导。"
        )
        try:
            return await self._call_chat(
                system_prompt, f"上下文：{context}\n用户分享：{user_sharing}"
            )
        except Exception as e:
            logger.warning("http_runtime.generate_follow_up.fallback", error=str(e))
            return await self._fallback.generate_follow_up(context, user_sharing)

    async def generate_summary(
        self, context: str, transcript: list[dict[str, object]]
    ) -> SummaryResult:
        system_prompt = (
            "你是心理互助活动的总结者。根据对话记录生成简短总结和鼓励语。"
            '返回JSON: {"summary_text": "...", "encouragement_text": "..."}'
        )
        user_msg = json.dumps({"context": context, "transcript": transcript}, ensure_ascii=False)
        try:
            result = await self._call_chat(system_prompt, user_msg)
            parsed = json.loads(result)
            return SummaryResult(**parsed)
        except Exception as e:
            logger.warning("http_runtime.generate_summary.fallback", error=str(e))
            return await self._fallback.generate_summary(context, transcript)

    async def generate_postcard_text(self, context: str, user_sharing: str) -> str:
        system_prompt = (
            "你是一个温暖的文案创作者。根据用户的分享内容，写一段适合放在明信片上的鼓励文字。"
            "文字要简短、温暖、有力量感。"
        )
        try:
            return await self._call_chat(
                system_prompt, f"上下文：{context}\n用户分享：{user_sharing}"
            )
        except Exception as e:
            logger.warning("http_runtime.generate_postcard_text.fallback", error=str(e))
            return await self._fallback.generate_postcard_text(context, user_sharing)
