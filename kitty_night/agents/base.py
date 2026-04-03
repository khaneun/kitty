"""Night mode 에이전트 기반 클래스 — kitty.agents.base와 완전 독립"""
import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from kitty_night.config import AIProvider, night_settings
from kitty_night.utils import logger

_KST = ZoneInfo("Asia/Seoul")
_TOKEN_DIR = Path("night-token_usage")


class NightBaseAgent(ABC):
    """Night mode 에이전트 공통 기반"""

    def __init__(self, name: str, system_prompt: str) -> None:
        self.name = name
        self._base_prompt = system_prompt
        self.system_prompt = self._build_system_prompt()
        self._conversation: list[dict[str, Any]] = []
        self._provider = night_settings.ai_provider
        self._model = night_settings.resolved_model
        logger.info(f"[Night:{self.name}] provider={self._provider}, model={self._model}")

    def _build_system_prompt(self) -> str:
        from kitty_night.feedback.store import get_feedback_prompt
        feedback = get_feedback_prompt(self.name)
        return self._base_prompt + feedback if feedback else self._base_prompt

    def reload_feedback(self) -> None:
        self.system_prompt = self._build_system_prompt()

    def _record_tokens(self, input_tokens: int, output_tokens: int) -> None:
        try:
            _TOKEN_DIR.mkdir(exist_ok=True)
            today = datetime.now(_KST).strftime("%Y-%m-%d")
            path = _TOKEN_DIR / f"{today}.json"
            entry = {
                "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
                "agent": self.name,
                "provider": self._provider.value,
                "model": self._model,
                "input_tokens": input_tokens,
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
            logger.debug(f"[Night:{self.name}] token: in={input_tokens} out={output_tokens}")
        except Exception as e:
            logger.warning(f"[Night:{self.name}] token record failed: {e}")

    async def think(self, user_message: str) -> str:
        self._conversation.append({"role": "user", "content": user_message})

        if self._provider == AIProvider.ANTHROPIC:
            reply = await self._think_anthropic()
        elif self._provider == AIProvider.OPENAI:
            reply = await self._think_openai()
        elif self._provider == AIProvider.GEMINI:
            reply = await self._think_gemini()
        else:
            raise ValueError(f"Unsupported provider: {self._provider}")

        self._conversation.append({"role": "assistant", "content": reply})
        logger.debug(f"[Night:{self.name}] {reply[:100]}...")
        return reply

    async def _think_anthropic(self) -> str:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=night_settings.anthropic_api_key)
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
        client = AsyncOpenAI(api_key=night_settings.openai_api_key)
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
        genai.configure(api_key=night_settings.gemini_api_key)
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

    async def chat(self, user_message: str, context: str = "") -> str:
        """One-shot Q&A — trading conversation 오염 없는 별도 응답"""
        system = self.system_prompt
        if context:
            system += f"\n\n[Current context]\n{context}"
        messages = [{"role": "user", "content": user_message}]

        if self._provider == AIProvider.ANTHROPIC:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=night_settings.anthropic_api_key)
            resp = await client.messages.create(
                model=self._model, max_tokens=1024,
                system=system, messages=messages,
            )
            return resp.content[0].text if resp.content else ""
        elif self._provider == AIProvider.OPENAI:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=night_settings.openai_api_key)
            resp = await client.chat.completions.create(
                model=self._model, max_tokens=1024,
                messages=[{"role": "system", "content": system}] + messages,  # type: ignore[arg-type]
            )
            return resp.choices[0].message.content or ""
        elif self._provider == AIProvider.GEMINI:
            import google.generativeai as genai
            genai.configure(api_key=night_settings.gemini_api_key)
            model = genai.GenerativeModel(model_name=self._model, system_instruction=system)
            resp = await model.generate_content_async(user_message)
            return resp.text
        return "Unsupported AI provider."

    def reset_conversation(self) -> None:
        self._conversation = []

    @abstractmethod
    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        """에이전트 핵심 로직"""
        ...
