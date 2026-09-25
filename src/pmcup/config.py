from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    supermarket_api_key: str = ""
    supermarket_api_base: str = "https://www.thesuper.market/api/v1"
    tournament_slug: str = ""
    dry_run: bool = True
    # Second safety latch — must be true AND dry_run false for live orders
    live_trading: bool = False
    tournament_kelly_mult: float = 1.75
    min_edge: float = 0.04
    max_position_frac: float = 0.25
    request_timeout_s: float = 30.0

    # Unattended multi-bot runner
    bot_interval_seconds: int = 180
    bot_max_orders_per_cycle: int = 5
    bot_refresh_forecasts_every_n: int = 10
    bot_enable_edge: bool = True
    bot_enable_constraint: bool = True
    bot_enable_risk: bool = True
    bot_exit_net_edge: float = 0.01  # flatten when net edge collapses below this
    bot_take_profit_frac: float = 0.35  # sell if uPnL% exceeds this (paper/live)

    # Email alerts when bots stop/crash
    notify_on_stop: bool = True
    notify_email_to: str = ""
    notify_email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    def require_api_key(self) -> str:
        if not self.supermarket_api_key or self.supermarket_api_key.startswith("your_"):
            raise SystemExit(
                "Missing SUPERMARKET_API_KEY. Copy .env.example → .env and paste "
                "a key from https://sig.thesuper.market → Settings → API Keys "
                "(scopes: read + trade)."
            )
        return self.supermarket_api_key

    @property
    def can_trade_live(self) -> bool:
        return self.live_trading and not self.dry_run


settings = Settings()
