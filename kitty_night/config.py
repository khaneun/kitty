"""Kitty Night Mode 설정 — 해외주식 자동매매 환경변수"""
from enum import Enum

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class AIProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


DEFAULT_MODELS: dict[str, str] = {
    AIProvider.ANTHROPIC: "claude-sonnet-4-6",
    AIProvider.OPENAI: "gpt-4o",
    AIProvider.GEMINI: "gemini-2.0-flash",
}


class Exchange(str, Enum):
    """지원 해외 거래소"""
    NAS = "NAS"   # NASDAQ
    NYS = "NYS"   # NYSE
    AMS = "AMS"   # AMEX


EXCHANGE_LABELS: dict[str, str] = {
    "NAS": "NASDAQ",
    "NYS": "NYSE",
    "AMS": "AMEX",
    "TSE": "Tokyo",    # 확장 예비
    "HKS": "Hong Kong",
    "SHS": "Shanghai",
    "SZS": "Shenzhen",
    "HSX": "Ho Chi Minh",
    "LIS": "London",
}


class NightSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env를 기본으로 읽고, .env.night가 있으면 덮어씀 (night 전용 오버라이드용)
        env_file=(".env", ".env.night"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # AI Provider — NIGHT_AI_PROVIDER 우선, 없으면 AI_PROVIDER 사용
    ai_provider: AIProvider = Field(
        default=AIProvider.OPENAI,
        validation_alias=AliasChoices("NIGHT_AI_PROVIDER", "AI_PROVIDER"),
    )
    ai_model: str = Field(default="", alias="NIGHT_AI_MODEL")

    # API Keys — .env와 동일한 키 공유
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # KIS 실전 — NIGHT_KIS_* 우선, 없으면 KIS_* 사용
    kis_app_key: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_APP_KEY", "KIS_APP_KEY"),
    )
    kis_app_secret: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_APP_SECRET", "KIS_APP_SECRET"),
    )
    kis_account_number: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_ACCOUNT_NUMBER", "KIS_ACCOUNT_NUMBER"),
    )

    # KIS 모의 — NIGHT_KIS_PAPER_* 우선, 없으면 KIS_PAPER_* 사용
    kis_paper_app_key: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_PAPER_APP_KEY", "KIS_PAPER_APP_KEY"),
    )
    kis_paper_app_secret: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_PAPER_APP_SECRET", "KIS_PAPER_APP_SECRET"),
    )
    kis_paper_account_number: str = Field(
        default="",
        validation_alias=AliasChoices("NIGHT_KIS_PAPER_ACCOUNT_NUMBER", "KIS_PAPER_ACCOUNT_NUMBER"),
    )

    # Telegram — .env와 동일한 키 공유
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # 매매 설정
    trading_mode: TradingMode = Field(default=TradingMode.PAPER, alias="NIGHT_TRADING_MODE")
    max_buy_amount_usd: float = Field(default=700.0, alias="NIGHT_MAX_BUY_AMOUNT_USD")
    max_position_size_usd: float = Field(default=3500.0, alias="NIGHT_MAX_POSITION_SIZE_USD")

    # 대상 시장
    target_exchanges: str = Field(default="NAS,NYS", alias="NIGHT_TARGET_EXCHANGES")

    # 사이클 간격 (초)
    cycle_seconds: int = Field(default=900, alias="NIGHT_CYCLE_SECONDS")

    @model_validator(mode="after")
    def check_provider_key(self) -> "NightSettings":
        key_map = {
            AIProvider.ANTHROPIC: self.anthropic_api_key,
            AIProvider.OPENAI: self.openai_api_key,
            AIProvider.GEMINI: self.gemini_api_key,
        }
        if not key_map[self.ai_provider]:
            raise ValueError(
                f"NIGHT_AI_PROVIDER='{self.ai_provider}' 이지만 해당 API 키가 설정되지 않았습니다."
            )

        if self.trading_mode == TradingMode.PAPER:
            if not self.kis_paper_app_key or not self.kis_paper_account_number:
                raise ValueError(
                    "NIGHT_TRADING_MODE=paper 이지만 NIGHT_KIS_PAPER_APP_KEY / "
                    "NIGHT_KIS_PAPER_ACCOUNT_NUMBER 가 설정되지 않았습니다."
                )
        else:
            if not self.kis_app_key or not self.kis_account_number:
                raise ValueError(
                    "NIGHT_TRADING_MODE=live 이지만 NIGHT_KIS_APP_KEY / "
                    "NIGHT_KIS_ACCOUNT_NUMBER 가 설정되지 않았습니다."
                )
        return self

    @property
    def resolved_model(self) -> str:
        return self.ai_model or DEFAULT_MODELS[self.ai_provider]

    @property
    def is_live(self) -> bool:
        return self.trading_mode == TradingMode.LIVE

    @property
    def active_kis_app_key(self) -> str:
        return self.kis_app_key if self.is_live else self.kis_paper_app_key

    @property
    def active_kis_app_secret(self) -> str:
        return self.kis_app_secret if self.is_live else self.kis_paper_app_secret

    @property
    def active_kis_account_number(self) -> str:
        return self.kis_account_number if self.is_live else self.kis_paper_account_number

    @property
    def active_kis_base_url(self) -> str:
        return (
            "https://openapi.koreainvestment.com:9443"
            if self.is_live
            else "https://openapivts.koreainvestment.com:29443"
        )

    @property
    def exchange_list(self) -> list[str]:
        return [e.strip().upper() for e in self.target_exchanges.split(",") if e.strip()]


night_settings = NightSettings()  # type: ignore[call-arg]
