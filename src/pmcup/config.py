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
    # Refuse live orders outside Oct 1–Nov 4 2026 ET even if latches are on
    enforce_trading_window: bool = True
    tournament_kelly_mult: float = 1.75
    min_edge: float = 0.04
    max_position_frac: float = 0.25
    # Cap total notional per underlying race (across Dem/Rep mirrors)
    max_race_exposure_frac: float = 0.25
    # Correlated basket caps (House seats move together, etc.)
    max_house_basket_frac: float = 0.55
    max_senate_basket_frac: float = 0.40
    max_gov_basket_frac: float = 0.30
    max_other_basket_frac: float = 0.25
    # Shrink model fair probs toward market mid as confidence falls
    blend_toward_market: bool = True
    # 1.0 = full (1-confidence) weight on market; 0.5 = gentler shrink
    market_blend_strength: float = 1.0
    request_timeout_s: float = 30.0
    http_max_retries: int = 3
    http_retry_backoff_s: float = 0.75

    # Unattended multi-bot runner
    bot_interval_seconds: int = 180
    bot_max_orders_per_cycle: int = 5
    bot_refresh_forecasts_every_n: int = 10
    bot_enable_edge: bool = True
    bot_enable_constraint: bool = True
    bot_enable_risk: bool = True
    bot_exit_net_edge: float = 0.01  # flatten when net edge collapses below this
    bot_take_profit_frac: float = 0.35  # sell if uPnL% exceeds this (paper/live)
    # Underwater trim when no scan idea exists for the position
    bot_underwater_exit_frac: float = -0.20
    # Keep running after failures (systemd/watchdog restart). Notify after N fails.
    bot_fail_notify_streak: int = 3
    bot_fail_backoff_s: int = 60
    # FLB heuristic: paper OK by default; blocked for live unless explicitly enabled
    allow_flb_paper: bool = True
    allow_flb_live: bool = False
    # Never overwrite fair_probs rows marked manual/user/locked
    protect_manual_fair_probs: bool = True
    # Seconds before bot status is considered stale on the dashboard
    bot_status_stale_seconds: int = 600

    # Email alerts
    notify_on_stop: bool = True
    notify_on_errors: bool = True
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
    def latches_allow_live(self) -> bool:
        return self.live_trading and not self.dry_run

    @property
    def can_trade_live(self) -> bool:
        if not self.latches_allow_live:
            return False
        if self.enforce_trading_window:
            from .trading_window import in_live_window

            return in_live_window()
        return True


settings = Settings()
