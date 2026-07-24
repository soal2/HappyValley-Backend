"""HTTP-based agent runtime using OpenAI-compatible API."""

import json
import uuid
from urllib.request import getproxies

import httpx
import structlog

from app.agents.base import (
    AgentRuntime,
    GenerationMeta,
    RankedMatch,
    SummaryResult,
    TextGenerationResult,
)
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
        self._network_mode = settings.AI_NETWORK_MODE
        self._proxy_url = settings.AI_PROXY_URL
        if self._network_mode == "proxy" and not self._proxy_url:
            raise RuntimeError("AI_NETWORK_MODE=proxy 时必须配置 AI_PROXY_URL")
        if self._network_mode == "auto" and not self._proxy_url:
            self._proxy_url = getproxies().get("https")
        self._client = client
        self._retry_delay_seconds = retry_delay_seconds

    def _proxy_for_attempt(self, attempt: int) -> str | None:
        if self._network_mode == "proxy":
            return self._proxy_url
        if self._network_mode == "auto" and attempt == 0:
            return self._proxy_url
        return None

    async def _call_chat(self, system_prompt: str, user_message: str) -> TextGenerationResult:
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
                    proxy = self._proxy_for_attempt(attempt)
                    async with httpx.AsyncClient(
                        timeout=self._timeout,
                        trust_env=False,
                        proxy=proxy,
                    ) as client:
                        response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
                text = str(data["choices"][0]["message"]["content"]).strip()
                if not text:
                    raise ValueError("模型返回空文本")
                generation = GenerationMeta(
                    source="remote",
                    model=self._model,
                    request_id=str(data.get("id") or uuid.uuid4()),
                )
                logger.info(
                    "http_runtime.generation_completed",
                    source=generation.source,
                    model=generation.model,
                    request_id=generation.request_id,
                    char_count=len(text),
                )
                return TextGenerationResult(
                    text=text,
                    generation=generation,
                )
            except Exception as e:
                logger.warning(
                    "http_runtime.call_failed",
                    attempt=attempt + 1,
                    network_route="proxy" if self._proxy_for_attempt(attempt) else "direct",
                    error_type=type(e).__name__,
                    error=str(e),
                )
                if attempt == 0:
                    import asyncio

                    await asyncio.sleep(self._retry_delay_seconds)
                else:
                    raise
        raise RuntimeError("LLM 请求失败")

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
        result = await self._call_chat(system_prompt, user_msg)
        parsed = json.loads(result.text)
        return [RankedMatch(**item) for item in parsed]

    async def generate_opening(
        self, members: list[dict[str, object]], activity_rules: list[str]
    ) -> TextGenerationResult:
        result = await self._call_chat(
            "你是心晴谷的 AI 团体带领者小晴。生成一段不超过80字的开场。必须明确说明："
            "这是游戏化心理互助而非医疗、诊断或治疗；用户先描述基础画面；"
            "后续成员只能增加或修饰，不能删除、否定或覆盖；用户可以随时退出。不要提出问题。",
            json.dumps({"members": members, "rules": activity_rules}, ensure_ascii=False),
        )
        self._validate_opening(result.text)
        return result

    async def generate_peer_turn(
        self,
        character: dict[str, object],
        previous_contributions: list[str],
    ) -> TextGenerationResult:
        system_prompt = (
            "你是绘画疗愈文字接龙中的AI团友。严格以给定角色说话，只写本轮新增或修饰的画面描述。"
            "不得删除、否定、替换或覆盖已有画面，不评价用户，不诊断，不提出问题。"
            "输出一段20至50字的中文纯文本，不要JSON、标题或角色名前缀。"
        )
        user_msg = json.dumps(
            {
                "character": character,
                "previous_contributions": previous_contributions,
            },
            ensure_ascii=False,
        )
        result = await self._call_chat(system_prompt, user_msg)
        if len(result.text) > 200 or "？" in result.text or "?" in result.text:
            raise ValueError("AI 团友输出不符合接龙格式")
        return result

    async def generate_facilitator_response(
        self, context: str, user_sharing: str
    ) -> TextGenerationResult:
        system_prompt = (
            "你是心晴谷的AI带领者小晴。针对用户刚分享的感受，写1至3句、不超过100个汉字的回应。"
            "先准确共情，再指出一项可观察的积极优势，如勇气、自我觉察、边界感、感受力或真诚。"
            "不得提问，不得诊断、说教、强行积极化或承诺治疗和治愈。只输出回应正文。"
        )
        result = await self._call_chat(
            system_prompt, f"共同画面：{context}\n用户分享：{user_sharing}"
        )
        self._validate_facilitator_response(result.text)
        return result

    async def generate_summary(
        self, context: str, transcript: list[dict[str, object]]
    ) -> SummaryResult:
        system_prompt = (
            "你是心理互助活动的总结者。根据对话记录生成简短总结和鼓励语。"
            '返回JSON: {"summary_text": "...", "encouragement_text": "..."}'
        )
        user_msg = json.dumps({"context": context, "transcript": transcript}, ensure_ascii=False)
        result = await self._call_chat(system_prompt, user_msg)
        parsed = json.loads(result.text)
        return SummaryResult(**parsed, generation=result.generation)

    async def generate_postcard_text(self, context: str, user_sharing: str) -> TextGenerationResult:
        system_prompt = (
            "你是一个温暖的文案创作者。根据用户的分享内容，写一段适合放在明信片上的鼓励文字。"
            "文字要简短、温暖、有力量感。"
        )
        result = await self._call_chat(
            system_prompt, f"活动总结：{context}\n用户分享：{user_sharing}"
        )
        if len(result.text) > 100 or "？" in result.text or "?" in result.text:
            raise ValueError("明信片鼓励语不符合格式")
        return result

    @staticmethod
    def _validate_opening(text: str) -> None:
        required_groups = [
            ("AI", "人工智能"),
            ("治疗", "医疗", "咨询"),
            ("增加", "修饰"),
            ("退出",),
        ]
        if len(text) > 260 or any(
            not any(word in text for word in group) for group in required_groups
        ):
            raise ValueError("小晴开场缺少必要披露")

    @staticmethod
    def _validate_facilitator_response(text: str) -> None:
        forbidden = ("？", "?", "治愈", "诊断", "你一定会好")
        if len(text) > 100 or any(word in text for word in forbidden):
            raise ValueError("小晴回应不符合安全规范")
