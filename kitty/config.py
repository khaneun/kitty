from enum import Enum

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    PAPER = "paper"   # 모의투자
    LIVE = "live"     # 실전투자


class AIProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"


# provider별 기본 모델
DEFAULT_MODELS: dict[str, str] = {
    AIProvider.ANTHROPIC: "claude-opus-4-6",
    AIProvider.OPENAI: "gpt-4o",
    AIProvider.GEMINI: "gemini-1.5-pro",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI Provider 설정
    ai_provider: AIProvider = Field(default=AIProvider.ANTHROPIC, description="사용할 AI 제공자")
    ai_model: str = Field(default="", description="모델명 (비워두면 provider 기본값 사용)")

    # API Keys (사용하는 provider 키만 입력하면 됨)
    anthropic_api_key: str = Field(default="", description="Anthropic API 키")
    openai_api_key: str = Field(default="", description="OpenAI API 키")
    gemini_api_key: str = Field(default="", description="Google Gemini API 키")

    @model_validator(mode="after")
    def check_provider_key(self) -> "Settings":
        key_map = {
            AIProvider.ANTHROPIC: self.anthropic_api_key,
            AIProvider.OPENAI: self.openai_api_key,
            AIProvider.GEMINI: self.gemini_api_key,
        }
        if not key_map[self.ai_provider]:
            raise ValueError(f"AI_PROVIDER='{self.ai_provider}' 이지만 해당 API 키가 설정되지 않았습니다.")

        if self.trading_mode == TradingMode.PAPER:
            if not self.kis_paper_app_key or not self.kis_paper_account_number:
                raise ValueError("TRADING_MODE=paper 이지만 KIS_PAPER_APP_KEY / KIS_PAPER_ACCOUNT_NUMBER 가 설정되지 않았습니다.")
        else:
            if not self.kis_app_key or not self.kis_account_number:
                raise ValueError("TRADING_MODE=live 이지만 KIS_APP_KEY / KIS_ACCOUNT_NUMBER 가 설정되지 않았습니다.")

        return self

    @property
    def resolved_model(self) -> str:
        """ai_model이 비어있으면 provider 기본값 반환"""
        return self.ai_model or DEFAULT_MODELS[self.ai_provider]

    # 한국투자증권 실전투자 (KIS Developers)
    kis_app_key: str = Field(default="", description="실전투자 App Key")
    kis_app_secret: str = Field(default="", description="실전투자 App Secret")
    kis_account_number: str = Field(default="", description="실전투자 계좌번호 10자리")

    # 한국투자증권 모의투자 (KIS Developers Virtual)
    kis_paper_app_key: str = Field(default="", description="모의투자 App Key")
    kis_paper_app_secret: str = Field(default="", description="모의투자 App Secret")
    kis_paper_account_number: str = Field(default="", description="모의투자 계좌번호 10자리")

    # Telegram
    telegram_bot_token: str = Field(..., description="텔레그램 봇 토큰")
    telegram_chat_id: str = Field(..., description="텔레그램 채팅 ID")

    # 모니터 대시보드
    monitor_host: str = Field(default="", description="모니터 호스트 (비워두면 EC2 퍼블릭 IP 자동 조회)")
    monitor_port: int = Field(default=8080, description="모니터 포트")

    # 매매 설정
    trading_mode: TradingMode = Field(default=TradingMode.PAPER)
    max_buy_amount: int = Field(default=1_000_000, description="1회 최대 매수금액 (원)")
    max_position_size: int = Field(default=5_000_000, description="종목당 최대 보유금액 (원)")

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


settings = Settings()  # type: ignore[call-arg]
