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
    # Keep dry powder for new edges (0 = allow full deploy).
    # Leave at 0: forcing a cash level makes the bot sell positions the buyer
    # then immediately re-buys, which just pays the spread in a loop.
    target_cash_frac: float = 0.0

    # Capital rotation: sell a tired position only to fund a clearly better one
    rotation_enabled: bool = True
    rotation_min_edge_gain: float = 0.10  # new edge must beat the old by this
    rotation_cooldown_hours: float = 12.0  # no re-trading a rotated market
    # Worst-case daily turnover is max_per_day * max_frac * 2 (a sell and a buy),
    # so these two numbers are the hard ceiling on what churn can ever cost.
    # 8 * 0.04 * 2 = 64% of equity/day at the absolute worst.
    rotation_max_per_day: int = 8
    rotation_max_frac_per_trade: float = 0.04
    rotation_earmark_minutes: float = 20.0  # freed cash reserved for the target
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
    # Peak-to-trough circuit breaker (Prevayo-style): cut new buy sizes on drawdown
    circuit_breaker_enabled: bool = True
    circuit_breaker_drawdown: float = 0.20  # trip at -20% from peak equity
    circuit_breaker_size_mult: float = 0.50  # halve buys while tripped
    # Measure the peak over a trailing window so one early spike doesn't
    # throttle buys for the rest of the cup. 0 = all-time peak.
    circuit_breaker_peak_window_days: float = 3.0

    # Resting-order hygiene: cancel unfilled limits and never stack duplicates
    # Ideas the edge bot considers per cycle. Too low and it re-buys the same
    # top-ranked names every cycle instead of spreading across the board.
    bot_max_edge_ideas: int = 40
    # A buy this small is noise once a cap is nearly full — skip it instead
    bot_min_order_qty: int = 25

    order_stale_seconds: int = 600
    max_cancels_per_cycle: int = 40
    # Don't dump an exit into a hole: skip if the bid is this far below target
    order_max_sell_slippage: float = 0.15

    # Always-on anomaly watch. These thresholds describe failures that looked
    # like healthy cycles at the time, so none of them raised an error.
    guard_enabled: bool = True
    guard_interval_seconds: int = 300
    guard_alert_cooldown_s: int = 3600  # per alert key, so a stuck state mails once
    guard_max_stall_seconds: int = 900  # no completed cycle in this long
    guard_max_orders_per_hour: int = 40  # the Oct 4-5 pile-up ran far past this
    guard_max_open_orders: int = 25  # 927 rested unnoticed for days
    guard_max_drawdown: float = 0.10  # warn well before the breaker trips at 20%
    guard_drawdown_window_hours: float = 72.0

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
