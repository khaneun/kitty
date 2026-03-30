"""MCP Tool 기반 클래스"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    success: bool
    data: str          # AI 프롬프트에 주입할 포맷된 텍스트
    source: str        # 도구 이름 (로깅용)
    error: str = field(default="")


class BaseTool(ABC):
    """모든 외부 데이터 도구의 공통 기반"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def fetch(self, query: str) -> ToolResult: ...

    async def close(self) -> None:
        """리소스 정리 (필요시 오버라이드)"""
