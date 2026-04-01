"""에이전트 기반 클래스 - 멀티 AI provider 지원"""
import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from kitty.config import AIProvider, settings
from kitty.utils import logger

_TOKEN_DIR = Path("token_usage")


class BaseAgent(ABC):
    """모든 에이전트의 공통 기반"""

    def __init__(self, name: str, system_prompt: str) -> None:
        self.name = name
        self._base_prompt = system_prompt
        self.system_prompt = self._build_system_prompt()
        self._conversation: list[dict[str, Any]] = []
        self._provider = settings.ai_provider
        self._model = settings.resolved_model
        logger.info(f"[{self.name}] provider={self._provider}, model={self._model}")

    def _build_system_prompt(self) -> str:
        """base prompt + 성과 피드백 합산"""
        from kitty.feedback.store import get_feedback_prompt
        feedback = get_feedback_prompt(self.name)
        return self._base_prompt + feedback if feedback else self._base_prompt

    def reload_feedback(self) -> None:
        """피드백 파일이 업데이트됐을 때 system_prompt 갱신"""
        self.system_prompt = self._build_system_prompt()

    def _record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """토큰 사용량을 token_usage/YYYY-MM-DD.json 에 기록"""
        try:
            _TOKEN_DIR.mkdir(exist_ok=True)
            today = datetime.now().strftime("%Y-%m-%d")
            path = _TOKEN_DIR / f"{today}.json"
            entry = {
                "ts":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "agent":         self.name,
                "provider":      self._provider.value,
                "model":         self._model,
                "input_tokens":  input_tokens,
                "output_tokens": output_tokens,
            }
            entries = []
            if path.exists():
                try:
                    entries = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    entries = []
            entries.append(entry)
            path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
            logger.debug(f"[{self.name}] 토큰 기록: in={input_tokens} out={output_tokens}")
        except Exception as e:
            logger.warning(f"[{self.name}] 토큰 기록 실패: {e}")

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
        if response.usage:
            self._record_tokens(response.usage.input_tokens, response.usage.output_tokens)
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
        if response.usage:
            self._record_tokens(response.usage.prompt_tokens, response.usage.completion_tokens)
        return response.choices[0].message.content or ""

    async def _think_gemini(self) -> str:
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(
            model_name=self._model,
            system_instruction=self.system_prompt,
        )
        history = [
            {"role": "user" if m["role"] == "user" else "model", "parts": [m["content"]]}
            for m in self._conversation[:-1]
        ]
        chat = model.start_chat(history=history)
        response = await chat.send_message_async(self._conversation[-1]["content"])
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            self._record_tokens(
                response.usage_metadata.prompt_token_count,
                response.usage_metadata.candidates_token_count,
            )
        return response.text

    def reset_conversation(self) -> None:
        self._conversation = []

    @abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """에이전트의 핵심 로직. context를 받아 결과를 반환한다."""
        ...
