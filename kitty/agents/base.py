"""에이전트 기반 클래스 - 멀티 AI provider 지원"""
from abc import ABC, abstractmethod
from typing import Any

from kitty.config import AIProvider, settings
from kitty.utils import logger


class BaseAgent(ABC):
    """모든 에이전트의 공통 기반"""

    def __init__(self, name: str, system_prompt: str) -> None:
        self.name = name
        self.system_prompt = system_prompt
        self._conversation: list[dict[str, Any]] = []
        self._provider = settings.ai_provider
        self._model = settings.resolved_model
        logger.info(f"[{self.name}] provider={self._provider}, model={self._model}")

    async def think(self, user_message: str) -> str:
        """설정된 AI provider에게 메시지를 보내고 응답을 받는다."""
        self._conversation.append({"role": "user", "content": user_message})

        if self._provider == AIProvider.ANTHROPIC:
            reply = await self._think_anthropic()
        elif self._provider == AIProvider.OPENAI:
            reply = await self._think_openai()
        elif self._provider == AIProvider.GEMINI:
            reply = await self._think_gemini()
        else:
            raise ValueError(f"지원하지 않는 provider: {self._provider}")

        self._conversation.append({"role": "assistant", "content": reply})
        logger.debug(f"[{self.name}] {reply[:100]}...")
        return reply

    async def _think_anthropic(self) -> str:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=self.system_prompt,
            messages=self._conversation,
        )
        return response.content[0].text if response.content else ""

    async def _think_openai(self) -> str:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        messages = [{"role": "system", "content": self.system_prompt}] + self._conversation
        response = await client.chat.completions.create(
            model=self._model,
            max_tokens=4096,
            messages=messages,  # type: ignore[arg-type]
        )
        return response.choices[0].message.content or ""

    async def _think_gemini(self) -> str:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self._model,
            system_instruction=self.system_prompt,
        )
        # Gemini는 대화 이력을 별도 형식으로 변환
        history = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]}
            for m in self._conversation[:-1]  # 마지막 메시지 제외 (send_message로 전달)
        ]
        chat = model.start_chat(history=history)
        response = await chat.send_message_async(self._conversation[-1]["content"])
        return response.text

    def reset_conversation(self) -> None:
        self._conversation = []

    @abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """에이전트의 핵심 로직. context를 받아 결과를 반환한다."""
        ...
