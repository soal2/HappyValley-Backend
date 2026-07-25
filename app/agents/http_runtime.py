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
    ReflectionAnswerResult,
    SummaryResult,
    TextGenerationResult,
)
from app.config import get_settings

logger = structlog.get_logger()


def _parse_json_object(raw: str) -> dict[str, object]:
    """Parse one model JSON object, tolerating a surrounding markdown fence."""
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型未返回要求的结构化 JSON")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError("模型返回的结构化 JSON 无法解析") from error
    if not isinstance(parsed, dict):
        raise ValueError("模型未返回要求的 JSON 对象")
    return parsed


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
        self,
        members: list[dict[str, object]],
        activity_rules: list[str],
        first_speaker: dict[str, object],
        max_duration_seconds: int = 20,
    ) -> TextGenerationResult:
        first_speaker_name = str(first_speaker.get("name") or "").strip()
        result = await self._call_chat(
            "你是心晴谷的 AI 团体带领者小晴。生成一段不超过80字的温暖开场。"
            "欢迎大家带着各自心情来到绘画疗愈空间，说明将用接龙共同完成画作，"
            "结束后可获得根据本次活动定制的明信片，并自然邀请指定的第一位团员"
            "用文字描述希望画面出现的样子。活动介绍已经完成 AI 身份与非医疗披露，"
            f"此处不要重复免责声明，不要更换首位团员。朗读须在{max_duration_seconds}秒内完成，"
            f"正文必须原样包含“共同完成一幅画”“明信片”和“现在请{first_speaker_name}先来”。"
            "只输出开场正文，不要标题、列表或引号。",
            json.dumps(
                {
                    "members": members,
                    "rules": activity_rules,
                    "first_speaker": first_speaker,
                },
                ensure_ascii=False,
            ),
        )
        self._validate_opening(result.text, first_speaker_name)
        return result

    async def generate_peer_turn(
        self,
        character: dict[str, object],
        previous_contributions: list[str],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult:
        # 注入角色人设，让输出更有拟人感
        persona = str(character.get("system_prompt_template") or "")
        style = str(character.get("speak_style") or "")
        name = str(character.get("name", "AI团友"))

        persona_block = ""
        if persona:
            persona_block += f"\n【你的身份】\n{persona.strip()}\n"
        if style:
            persona_block += f"\n【你的说话风格】\n{style}\n"

        system_prompt = (
            f"你是【{name}】，正在参与绘画疗愈文字接龙活动。"
            f"{persona_block}\n"
            "【任务要求】\n"
            "以“我希望这幅画上有”或“我想在这幅画上加上”开头，"
            "用你独有的语气和视角，为画面新增或修饰一段描述。"
            "要求：只写本轮新增/修饰的画面内容，20至32字中文纯文本，保持中性或积极。"
            "不得删除、否定或覆盖已有画面，不评价用户，不诊断，不提问。"
            f"朗读须在{max_duration_seconds}秒内完成。不要输出JSON、标题或角色名前缀。"
        )
        user_msg = json.dumps(
            {
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

    async def generate_reflection_question(
        self,
        question_index: int,
        intent: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult:
        required_terms = [
            "感受",
            "画下那一笔、心里想",
            "其他成员、说",
            "现在、心情",
        ][question_index]
        result = await self._call_chat(
            "你是心晴谷的AI带领者小晴。根据已经发生的团体内容，用一至两句自然承接并提出"
            f"当前指定问题。正文不超过32个汉字，只提出这一个问题，不追加追问，不诊断、不承诺治愈，"
            f"正文必须包含这些轮次锚点词：{required_terms}。"
            f"朗读须在{max_duration_seconds}秒内完成。",
            json.dumps(
                {
                    "question_index": question_index,
                    "required_intent": intent,
                    "artwork": context,
                    "transcript": transcript,
                },
                ensure_ascii=False,
            ),
        )
        if len(result.text) > 100 or not any(mark in result.text for mark in ("？", "?")):
            raise ValueError("小晴回顾问题不符合格式")
        return result

    async def generate_reflection_answer(
        self,
        character: dict[str, object],
        question: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> ReflectionAnswerResult:
        result = await self._call_chat(
            "你是绘画疗愈活动中的AI团友。严格使用角色资料和已发生的发言回答当前问题，"
            "结合自身设定表达，并给出来自画作、成员鼓励或多元视角的真实积极反馈。"
            "正文中的 text 不超过34个汉字。不得编造成员说过的话，不得诊断或承诺治愈，"
            f"朗读须在{max_duration_seconds}秒内完成。"
            "如果真人团友本轮选择不分享（跳过），不要引用该团友的发言。"
            '返回JSON：{"text":"...","positive_feedback_type":'
            '"artwork|encouragement|multiple_perspectives",'
            '"referenced_message_ids":["仅可填写上下文中存在的消息ID"]}',
            json.dumps(
                {
                    "character": character,
                    "question": question,
                    "artwork": context,
                    "transcript": transcript,
                },
                ensure_ascii=False,
            ),
        )
        parsed = _parse_json_object(result.text)
        answer = ReflectionAnswerResult(**parsed, generation=result.generation)
        if len(answer.text) > 120 or answer.positive_feedback_type not in {
            "artwork",
            "encouragement",
            "multiple_perspectives",
        }:
            raise ValueError("AI 团友回顾回答不符合格式")
        return answer

    async def generate_summary(
        self,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 20,
    ) -> SummaryResult:
        system_prompt = (
            "你是心晴谷的AI团体带领者小晴。根据真实对话生成简短总结和鼓励语。"
            "优先准确回应真人玩家实际说过的话，再总结共同主题、可见的美好品质、"
            "解决问题的多样视角与温暖安全的团体氛围。若真人四轮都跳过，只总结"
            "共同画作和已经发生的互动，不推测真人感受，不诊断、不承诺治愈。"
            f"summary_text 必须在 70 个汉字以内（约 {max_duration_seconds} 秒朗读）、不超过 3 个句子；"
            "encouragement_text 必须在 30 个汉字以内。不要输出 JSON 以外的内容。"
            '返回JSON: {"summary_text": "...", "encouragement_text": "..."}'
        )
        user_msg = json.dumps({"context": context, "transcript": transcript}, ensure_ascii=False)
        result = await self._call_chat(system_prompt, user_msg)
        parsed = _parse_json_object(result.text)
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
    def _validate_opening(text: str, first_speaker_name: str) -> None:
        normalized = "".join(text.split())
        required_phrases = (
            "共同完成一幅画",
            "明信片",
            f"现在请{first_speaker_name}先来",
        )
        if (
            not first_speaker_name
            or len(text) > 160
            or any(phrase not in normalized for phrase in required_phrases)
        ):
            raise ValueError("小晴开场缺少活动流程信息")

    @staticmethod
    def _validate_facilitator_response(text: str) -> None:
        forbidden = ("？", "?", "治愈", "诊断", "你一定会好")
        if len(text) > 100 or any(word in text for word in forbidden):
            raise ValueError("小晴回应不符合安全规范")
