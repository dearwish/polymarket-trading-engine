from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "polymarket-trading-engine"
    trading_mode: str = "paper"
    market_family: str = "btc_1h"
    loop_seconds: int = 15

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openai/gpt-4.1-mini"

    polymarket_host: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_url: str = "https://data-api.polymarket.com"
    polymarket_ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    polymarket_chain_id: int = 137
    btc_ws_url: str = "wss://stream.binance.com:9443/stream"
    btc_symbol: str = "btcusdt"
    btc_rest_fallback_url: str = "https://api.binance.com/api/v3/ticker/price"
    ws_reconnect_backoff_seconds: float = 2.0
    ws_reconnect_backoff_max_seconds: float = 30.0
    ws_ssl_verify: bool = True
    daemon_discovery_interval_seconds: int = 60
    daemon_decision_min_interval_seconds: float = 1.0
    # How often the daemon polls settings_changes.id for operator overrides.
    # 2s matches human-perceived "instant" and mirrors the other interval
    # loops; see DaemonRunner._settings_reload_loop.
    daemon_settings_reload_interval_seconds: float = 2.0
    # When True the daemon auto-executes APPROVED decisions as paper trades.
    # Positions are recorded in the portfolio so the dashboard / report paths
    # fill in without any separate CLI invocation. Safe by default (opt-in).
    daemon_auto_paper_execute: bool = False
    # Exit thresholds for open paper positions, evaluated on every tick.
    # Expressed as a fraction of the committed size_usd:
    #   paper_take_profit_pct = 0.20 → close when unrealised PnL ≥ +20%
    #   paper_stop_loss_pct   = 0.25 → close when unrealised PnL ≤ −25%
    # Set to 0.0 to disable (default): positions then hold until the TTE exit
    # buffer kicks in right before market resolution.
    paper_take_profit_pct: float = 0.0
    paper_stop_loss_pct: float = 0.0
    # Trailing stop: track peak token price since entry; close when current
    # drops by `paper_trailing_stop_pct` of the peak. 0.0 disables.
    paper_trailing_stop_pct: float = 0.0
    # Trail-arm threshold: the trailing stop only becomes active once the
    # peak clears entry_price × (1 + paper_trail_arm_pct). Prevents the
    # trail from firing at a small loss when the peak barely moved above
    # entry. 0.0 arms immediately (previous behaviour).
    paper_trail_arm_pct: float = 0.0
    # Trail confirmation: require N consecutive ticks where the exit-walk
    # VWAP sits at-or-below ``trail_floor`` before firing the trail. Filters
    # single-tick wicks / spread widenings that would otherwise stop a
    # winning position out on a one-tick noise spike. 0 or 1 = fire on the
    # first tick below floor (legacy behaviour). Counter resets to zero on
    # any tick where current price recovers above the floor.
    paper_trail_confirmation_ticks: int = 0
    # Stop-loss limit-out path: when SL triggers, post a passive limit at
    # ``threshold − slippage_ticks × tick`` and wait up to
    # ``paper_sl_limit_ttl_ticks`` daemon ticks for it to fully fill against
    # the live bid book. If filled, exit at the limit-or-better VWAP (clean
    # ~−SL_pct close). If the TTL expires unfilled, fall back to the legacy
    # full-book walk (accepting the gap). 0 = disabled (legacy walk-immediate).
    paper_sl_limit_ttl_ticks: int = 0
    paper_sl_limit_slippage_ticks: int = 1
    # Pre-trade exit-depth gate. Block new entries (maker AND taker paths)
    # when the bid book on the exit side has less than
    # ``size_usd × min_exit_depth_multiplier`` of dollar-depth across the
    # top 5 levels. Catches the doomed setups where SL would inevitably
    # expire to a deep walk fallback because there's no liquidity to
    # absorb our position near the threshold. 0.0 = disabled.
    min_exit_depth_multiplier: float = 0.0
    # Entry cooldown: after any close on a market, block new entries on the
    # same market for this many seconds. Prevents immediate re-entry whipsaw
    # where the scorer flips sides and each flip gets stopped out. 0 disables.
    paper_entry_cooldown_seconds: int = 0
    # Minimum TTE for a new entry. Trades opened inside the last minute of a
    # candle have no time for the thesis to develop before the exit buffer
    # forces a close, and price noise in the final seconds dominates the GBM
    # drift signal. 0 disables. Typical value: 60–90.
    min_entry_tte_seconds: int = 0
    # Force-close any open position at this TTE regardless of PnL (supersedes
    # the exit_buffer sweep when > exit_buffer_seconds). Lets us widen the
    # fixed stop without holding through the final-seconds noise that eats
    # trailing stops. 0 disables — the normal exit buffer still applies.
    position_force_exit_tte_seconds: int = 0
    # Consecutive-loss circuit breaker. If the most recent N CLOSED positions
    # all have realized_pnl ≤ 0 the daemon halts with safety_stop_reason=
    # "consecutive_loss_limit" until an operator reviews. 0 disables.
    max_consecutive_losses: int = 0
    # Minimum seconds elapsed since the candle opened before a new entry is
    # allowed. Prevents entering at candle open when btc_log_return_since_candle_open
    # is ~0 and GBM uncertainty is at its maximum. Only applied to candle-style
    # families (btc_15m, btc_5m, btc_1h); threshold markets are unaffected.
    # 0 disables (default). A value of 60–90 is a reasonable starting point.
    min_candle_elapsed_seconds: int = 0
    # Upper bound: don't enter after this many seconds into the candle. Avoids
    # entering in the final third where there is no time for the TP/trail to
    # develop but the full stop-loss can still fire. 0 disables.
    max_candle_elapsed_seconds: int = 0
    # Scale-out / tiered take-profit ladder. Comma-separated list of
    #   "<pnl_pct>:<fraction_to_close>" pairs, e.g. "0.15:0.5,0.30:0.25"
    # meaning: at +15% PnL close 50% of the position, at +30% close another
    # 25%. Tranches fire in order; each only once per open position. Empty
    # string disables the ladder entirely (fixed TP / trailing / TTE still
    # apply to the remainder).
    paper_tp_ladder: str = ""
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 0
    live_trading_enabled: bool = False
    live_order_type: str = "FOK"
    live_post_only: bool = False

    max_position_usd: float = 10.0
    min_confidence: float = 0.75
    min_edge: float = 0.03
    max_spread: float = 0.04
    min_depth_usd: float = 200.0
    exit_buffer_seconds: int = 5
    exit_buffer_pct_of_tte: float = 0.0
    # Per-strategy daily-loss budget. RiskEngine rejects new entries with
    # ``daily_loss_limit`` once a strategy's own ``daily_realized_pnl``
    # falls below ``-max_daily_loss_usd``. Halts entries on that strategy
    # only — other strategies keep trading and existing positions still
    # get managed (SL/TP/trail) regardless. Was a daemon-wide kill-switch
    # before 2026-04-29.
    max_daily_loss_usd: float = 25.0
    stale_data_seconds: int = 30
    max_rejected_orders: int = 3
    max_concurrent_positions: int = 1
    max_net_btc_exposure_usd: float = 50.0
    paper_starting_balance_usd: float = 100.0
    # Per-strategy paper bankroll overrides. Comma-separated
    # ``strategy_id:amount`` pairs, e.g.
    # ``"market_maker:10000,fade:200,penny:5"``. Strategies absent from
    # the map fall back to ``paper_starting_balance_usd``. Hot-reloadable
    # — unlike the default which is frozen at PortfolioEngine init, this
    # field is parsed fresh on every ``get_account_state`` call so a
    # mid-soak edit (via dashboard / CLI / API) takes effect immediately.
    # Each strategy's paper accounting is fully isolated: MM's
    # ``available_usd`` derives from its own bankroll + own realised PnL
    # − own reserved, with no contention against fade/penny/adaptive_v2.
    paper_starting_balance_per_strategy: str = ""
    paper_position_ttl_seconds: int = 60
    paper_entry_slippage_bps: float = 10.0
    paper_exit_slippage_bps: float = 10.0
    # Phase 3 adaptive-regime: maker-follow configuration for the adaptive
    # scorer. When it sees a trending regime it places a resting limit a
    # ``paper_follow_limit_discount_bps`` below mid on its side; if the book
    # crosses that level within ``paper_follow_maker_ttl_seconds`` we treat
    # it as filled. 50 bps / 300 s are initial defaults tuned for 15-minute
    # BTC Up/Down markets — revisit after the first trending-regime soak.
    paper_follow_limit_discount_bps: float = 50.0
    paper_follow_maker_ttl_seconds: int = 300
    # Maker-yield selection (Tier 2 from gamma-trade-lab reference, 2026-04):
    # gate cancel/replace on material drift so the follow-maker path can
    # track a moving mid without churning on every tick. Both thresholds
    # default to 0.0 = never re-price a resting order (preserves the
    # existing "rest and wait for TTL" behaviour); raise the price
    # threshold to e.g. 0.005 (half-cent) and the size threshold to 10%
    # to opt in to price tracking.
    paper_follow_cancel_price_threshold: float = 0.0
    paper_follow_cancel_size_threshold_pct: float = 0.0
    # Depth filter for anchoring the maker mid. With min>0 we skip ghost
    # 1-lot levels when computing best-bid/best-ask — prevents the paper
    # maker from posting behind a phantom order. 0.0 disables (raw mid).
    paper_follow_min_level_size_shares: float = 0.0
    # Route the fade scorer's BUY assessments through the paper-maker
    # lifecycle (resting limit at mid − ``paper_follow_limit_discount_bps``,
    # TTL ``paper_follow_maker_ttl_seconds``) instead of the immediate
    # taker fill. Lets us simulate a post-only-GTC live-trading mode on
    # paper before flipping the live flag. Off by default; live-reloadable.
    fade_post_only: bool = False
    # Penny-buy strategy (extreme-tail dip). Runs as a third strategy
    # alongside fade + adaptive. See ``engine/penny_scoring.py`` for the
    # thesis and ``scripts/backtest_penny.py`` for the parameter sweep
    # that chose these defaults. All four settings hot-reload.
    # Phase 1 scorer clone — delegates to fade in every regime (trending
    # path was retired 2026-04-23 after follow-maker hit 17% win rate on
    # 250 trades). With it enabled the daemon doubles market-impact for
    # zero alpha; default off. Flip true only to bootstrap a genuinely
    # different adaptive variant in the strategy slot.
    adaptive_enabled: bool = False
    penny_enabled: bool = True
    penny_entry_thresh: float = 0.03
    penny_min_entry_tte_seconds: int = 300
    penny_force_exit_tte_seconds: int = 120
    penny_tp_multiple: float = 2.0
    penny_size_usd: float = 1.0
    # Stop-loss as a multiple of entry price. 0.5 means "exit when bid
    # drops to 50% of entry" (a 3¢ entry stops out at 1.5¢). Caps each
    # losing trade at −(1 − multiple) × size instead of riding to the
    # TTE-based force-exit floor (typically around 1¢ = −67%). Setting
    # to 0 disables the gate. See scripts/backtest_penny.py for the
    # break-even hit-rate math.
    penny_stop_loss_multiple: float = 0.5
    # Reversal-confirmation gate: require YES mid to have moved IN OUR
    # FAVOUR by at least this many bps over the last 30s before entering.
    # Replaces the earlier "max adverse move" gate which only required a
    # pause — the 2026-04-24 soak showed pauses are often temporary and
    # the side keeps crashing. Requiring actual reversal gives a much
    # stronger "the knife has bounced" signal. 0 disables.
    penny_min_favorable_move_bps: float = 25.0
    # Overreaction-fade strategy (adaptive_v2). Runs as a third scorer
    # alongside fade + penny; orthogonal signal: detects Polymarket mid
    # moves that outpace BTC's justification and bets the reversion. See
    # engine/overreaction_scoring.py for the full thesis.
    adaptive_v2_enabled: bool = True
    adaptive_v2_overreaction_threshold: float = 0.02
    adaptive_v2_sensitivity: float = 10.0
    adaptive_v2_cost_floor: float = 0.005
    adaptive_v2_min_seconds_to_expiry: int = 60
    # Hard ceiling on |edge| for the overreaction-fade scorer. Mirrors
    # ``quant_max_abs_edge`` for adaptive_v2: empirically the highest-edge
    # ticks have been the worst-PnL bucket because they correspond to
    # *real* (not noise) Polymarket moves the BTC reference hasn't caught
    # up to yet. 0.0 disables.
    adaptive_v2_max_abs_edge: float = 0.30
    # Route adaptive_v2's APPROVED assessments through the paper-maker
    # lifecycle (resting limit at mid − ``paper_follow_limit_discount_bps``,
    # TTL ``paper_follow_maker_ttl_seconds``) instead of an immediate taker
    # fill — the adaptive_v2 analogue of ``fade_post_only``. SL / TP ladder
    # / trail / SL-limit-out logic is already shared with fade via the
    # standard exit pipeline so no separate knobs are needed for those.
    adaptive_v2_post_only: bool = False
    # Per-strategy stop-loss override for adaptive_v2. When > 0, replaces
    # ``paper_stop_loss_pct`` for adaptive_v2 positions only — fade keeps
    # the global value. Adaptive_v2 fights fast moves, so its SL needs to
    # fire earlier (while bids are still populated near the threshold) to
    # let the limit-out path actually fill instead of expiring into a deep
    # walk fallback. 0 = use global ``paper_stop_loss_pct``.
    adaptive_v2_stop_loss_pct: float = 0.0
    # Direction-flip for the OverreactionScorer. Mirrors
    # ``quant_invert_drift`` on the fade scorer: when True the scorer bets
    # CONTINUATION instead of reversion (mid overshot up → bet YES). The
    # same short-horizon momentum thesis that flipped fade from −16% ROI
    # to break-even should apply here too.
    adaptive_v2_invert: bool = False
    # Top-5 book-imbalance gate. When enabled, abstain on entries where the
    # book pressure opposes the chosen side with magnitude
    # ``≥ adaptive_v2_imbalance_gate_min_abs``. Soak attribution showed the
    # against-pressure bucket bled -$0.17/trade vs +$0.66 with-pressure.
    # Mirrors the OFI gate already shared with the quant scorer.
    adaptive_v2_imbalance_gate_enabled: bool = False
    adaptive_v2_imbalance_gate_min_abs: float = 0.10
    # Skip adaptive_v2 entries during the first N seconds of the candle.
    # Soak attribution: candle_phase=0-60s lost -$0.82/trade — the largest
    # single kill bucket for this strategy. 0 disables.
    adaptive_v2_min_candle_elapsed_seconds: int = 0

    # Market-maker strategy. Posts a two-sided quote (YES-buy + NO-buy)
    # around the book mid, captures the spread when both legs fill, and
    # earns Polymarket's daily maker-reward subsidy on markets that pay
    # them. Inventory-aware skew shifts both legs toward neutralising any
    # one-sided fill. See engine/market_maker for the full strategy.
    # Disabled by default; flip on per soak. Runs alongside fade / penny
    # / adaptive_v2 with its own portfolio slice (strategy_id="market_maker").
    mm_enabled: bool = False
    # Notional per leg, USD. Total maximum exposure on a market is
    # mm_max_inventory_usd; per-fill quote size sets the granularity of
    # how the strategy fills toward that cap. NB: Polymarket reward-
    # paying markets carry a ``rewardsMinSize`` floor (frequently 200 or
    # 1000 USDC); quotes below that floor earn the spread but no daily
    # subsidy. See ``mm_universe_require_size_eligible``.
    mm_size_usd: float = 5.0
    # Half-width of our quoted spread around mid, in price units (cents).
    # 0.02 → quote ``mid − 0.02`` and ``(1 − mid) − 0.02``. If both legs
    # fill we book ``2 × half_spread`` of gross spread before costs.
    mm_target_half_spread: float = 0.02
    # Don't quote when the market spread is tighter than this — a tighter
    # market means we'd have to rest INSIDE the existing spread to lead
    # the book, which sacrifices the spread we're trying to capture.
    # Lowered 2026-05-01 from 0.01 → 0.005 after live observation: a 1¢
    # spread is the BEST possible MM target (wide enough to capture,
    # tight enough to signal liquidity) and should not be excluded.
    mm_min_market_spread: float = 0.005
    # Don't quote when the market spread is wider than this — a wide
    # spread on a binary market is the toxic-flow signature (informed
    # taker dumping into a thin book). 0 disables the upper gate.
    mm_max_market_spread: float = 0.10
    # Don't quote inside the final ``mm_min_tte_seconds`` of TTE — the
    # book gets thin and one-sided as resolution approaches and the
    # half-spread can rarely be captured cleanly inside that window.
    mm_min_tte_seconds: int = 120
    # Inventory-aware skew strength. 0 disables skew (symmetric quotes
    # always); 1.0 means a fully-loaded one-sided cap shifts BOTH legs by
    # exactly ``mm_target_half_spread`` (one leg moves to mid, the other
    # to mid ± full_spread). Typical 0.3–0.7 range.
    mm_inventory_skew_strength: float = 0.5
    # Per-side inventory cap, USD. When YES exposure reaches this we halt
    # the YES-buy leg (NO-buy keeps quoting to flatten); mirror for NO.
    # Bounds the strategy's worst-case loss-on-resolution to roughly the
    # cap times the larger of (1 − yes_quote, 1 − no_quote).
    mm_max_inventory_usd: float = 5.0
    # Only quote markets that pay maker-reward subsidies. The MM thesis
    # assumes the daily yield + captured spread compensates for adverse
    # selection; without the yield component the math gets thin on
    # short-horizon markets. Off by default since BTC short-horizon
    # markets typically pay no rewards — flip on when targeting longer-
    # horizon families.
    mm_require_rewards: bool = False
    # TTL on each resting MM quote, seconds. On expiry the quote is
    # cancelled and re-placed on the next tick at the then-current
    # skewed price. Re-placement cadence is also driven by the cancel
    # thresholds below; the TTL is a safety net for quiet windows.
    mm_quote_ttl_seconds: int = 60
    # Cancel/replace hysteresis (mirror of paper_follow_cancel_*). Only
    # re-quote when the desired price has drifted by more than this many
    # ticks from the resting quote, OR when the desired size differs
    # from the resting size by more than this %. Prevents cancel-thrash
    # that kills queue position when the mid jitters sub-tick.
    # Lowered 2026-05-01 from 1.0 → 0.5 after live observation: with a
    # 2.5¢ reward band and a 1-tick (1¢) hysteresis, quotes were sitting
    # 4-5¢ off the moving mid — outside the reward band 98% of the time.
    # 0.5 ticks (0.5¢) keeps quotes tracking the mid more aggressively.
    mm_replace_min_ticks: float = 0.5
    mm_replace_min_size_pct: float = 0.10
    # TTE buffer for force-closing accumulated MM legs at resolution.
    # MM positions don't have a TP/SL/trail — they either flatten via
    # the opposite-side fill or carry to expiry. This setting closes any
    # remaining open legs at the current bid when the market reaches
    # this many seconds before resolution, capping the carry risk.
    mm_force_exit_tte_seconds: int = 30
    # Cadence (seconds) for the MM freshness sweep. The MM lifecycle
    # handler only runs when a Polymarket WS event triggers a tick for
    # one of the strategy's markets — quiet markets (long-tail political,
    # off-hours sports) can go minutes without an event, leaving stale
    # TTL-expired quotes in memory and missing reward-band re-quotes
    # entirely. This loop invokes ``_handle_market_maker_strategy`` for
    # every market in ``_mm_market_ids`` on a fixed cadence so quiet
    # markets still get TTL cleanup, drift re-quotes, and accrual ticks.
    # 0 disables. Default 5s is a balance between responsiveness and
    # tick volume — the WS-driven path still handles fast markets
    # within ``decision_min_interval_seconds``.
    mm_freshness_interval_seconds: float = 5.0
    # Pre-fill adverse-selection guard. Refuse to honour a fill on a
    # resting MM quote if the current YES mid has moved away from the
    # quote's price by more than ``mm_max_fill_drift_pct / 100``. The
    # 2026-05-02 soak surfaced two −$985 single-leg fills caused by
    # zombie quotes catching falling-knife resolutions: our YES bid at
    # 0.695 stayed in memory while the market crashed to ~0.01, then a
    # WS event reactivated the handler and our stale quote ate the dump.
    # Default 5% (5¢ around 0.50 mid) — anything wider than that is
    # "the market repriced under us" and a fill there is pure adverse
    # selection. 0 disables the guard.
    mm_max_fill_drift_pct: float = 5.0
    # Hard TTE floor on FILLS. The existing ``mm_force_exit_tte_seconds``
    # floor handles closing already-open positions before resolution; this
    # mirrors it on the OPEN side — refuse to fill any resting quote when
    # ``tte_seconds <= mm_no_fill_tte_seconds``. Prevents the
    # "fill-then-immediately-resolve" failure where we open a position
    # 30 seconds before the market resolves against us. 0 disables.
    mm_no_fill_tte_seconds: int = 60
    # Defense-in-depth: cancel any pending MM quote older than this
    # regardless of which loop / market state path manages it. Belt-and-
    # braces against the "market dropped from MM universe but quote is
    # still in _pending_mm_orders memory" zombie path. 0 disables;
    # default 600s is well above the 60s TTL so legit re-quote cycles
    # are never affected.
    mm_max_quote_age_seconds: int = 600
    # Market-maker universe selector. The MM strategy needs a different
    # universe from the BTC short-horizon scorers (politics / sports /
    # long-duration events that pay daily reward subsidies). When
    # ``mm_universe_enabled`` is True the daemon scans Polymarket-wide
    # via ``PolymarketConnector.discover_mm_markets``, ranks by yield-
    # per-$1k-liquidity, and routes the top-N markets exclusively to the
    # MM strategy — fade / penny / adaptive_v2 see only the existing BTC
    # universe and are not invoked on MM markets. Set False to keep MM
    # piggybacking on the BTC discovery (degenerate but useful for
    # testing the lifecycle handler in isolation).
    mm_universe_enabled: bool = True
    # Filter: minimum daily reward rate (USD/day) for a market to be
    # MM-eligible. Below this the daily yield can't carry the strategy.
    mm_universe_min_rewards_daily_usd: float = 1.0
    # Filter: minimum aggregate book liquidity (USD) — a thin market
    # invites toxic flow and the spread can't be earned consistently.
    mm_universe_min_liquidity_usd: float = 5000.0
    # Filter: minimum time-to-expiry (seconds) — MM accumulates fills
    # over time, so a market resolving in the next hour can't capture
    # the spread reliably. Default 1h is conservative.
    mm_universe_min_tte_seconds: int = 3600
    # How many top-ranked MM markets to actually subscribe to. Each adds
    # 2 WS asset subscriptions (YES + NO tokens) so keep this small for
    # paper soaks.
    mm_universe_max_markets: int = 5
    # Cadence in seconds for re-scanning the MM universe. Independent
    # of the general discovery interval because the MM universe shifts
    # on a slower timescale (rewards announcements, new markets) than
    # BTC short-horizon slug rolls.
    mm_universe_refresh_seconds: int = 300
    # When True, the scanner drops markets whose ``rewardsMinSize``
    # exceeds ``mm_size_usd`` — quotes smaller than the reward-pool
    # eligibility floor earn the spread but no maker subsidy, defeating
    # the MM thesis (which assumes daily yield is the bulk of the edge).
    # As of 2026-05-01 every reward-paying market in the live Gamma
    # snapshot had ``rewardsMinSize >= 200``, so with the V1 default
    # ``mm_size_usd = 5.0`` this filter eliminates the entire universe.
    # The right way to soak with this on is to bump ``mm_size_usd`` up
    # to the floor of the markets you want to target (200 for the long-
    # tail political markets, 1000 for the daily sports markets) and
    # ensure ``paper_starting_balance_usd`` can cover the worst case.
    # Set False to keep quoting reward-paying markets even when the
    # configured size is below their floor — useful for paper-mode
    # lifecycle validation but the resulting fills earn no rewards.
    mm_universe_require_size_eligible: bool = True

    fee_bps: float = 0.0
    execution_maker_min_edge: float = 0.04
    execution_maker_min_tte_seconds: int = 120
    execution_price_tick: float = 0.01
    # Hysteresis on maker cancel/replace: only re-quote when the fresh maker
    # price has moved by more than ``execution_replace_min_ticks`` × tick OR
    # when the resting size differs from a target size by more than
    # ``execution_replace_min_size_pct``. Default of 2 ticks (warproxxx /
    # gamma-trade-lab pattern) prevents the cancel-thrash that kills queue
    # position when our scorer re-fires on every WS event with sub-tick noise.
    execution_replace_min_ticks: float = 2.0
    execution_replace_min_size_pct: float = 0.10
    execution_exit_buffer_floor_seconds: int = 10
    execution_exit_buffer_pct_of_tte: float = 0.1
    quant_drift_damping: float = 0.5
    # When True the scorer flips fair_yes to (1 - fair_yes). Used to test the
    # hypothesis that BTC short-horizon moves mean-revert rather than follow
    # GBM continuation: if the unflipped hit rate is significantly < 50% with
    # a strong Brier score, flipping should land the signal in the 60-90% band.
    quant_invert_drift: bool = False
    # Hard abstain ceiling on the chosen-side edge. Empirical soak data showed
    # the highest-conviction picks (|edge| ≥ 0.30) had the worst hit rates,
    # likely because extreme edges come from model/market disagreements the
    # GBM prior can't actually resolve. Set > 0 to force ABSTAIN whenever the
    # chosen edge exceeds the ceiling; 0.0 disables the guard.
    quant_max_abs_edge: float = 0.0
    quant_imbalance_tilt: float = 0.03
    quant_slippage_baseline_bps: float = 15.0
    quant_slippage_spread_coef: float = 0.25
    quant_default_vol_per_second: float = 0.00015
    quant_drift_horizon_seconds: float = 900.0
    quant_tte_floor_seconds: float = 5.0
    quant_confidence_per_edge: float = 10.0
    quant_high_expiry_risk_seconds: int = 15
    quant_medium_expiry_risk_seconds: int = 60
    # Shadow scorer (Phase 4 A/B). Empty string disables; set to "htf_tilt" to
    # run a parallel scorer on every tick without affecting live trade logic.
    # When enabled, daemon_tick gains shadow_fair_probability / shadow_suggested_side
    # / shadow_edge_yes / shadow_edge_no fields for offline comparison.
    # Regime-aware gate (replaces the old binary trend veto).
    # When enabled, counter-trend trades must clear a higher minimum edge
    # proportional to trend strength. With-trend and ranging trades are
    # unaffected. 4h takes priority over 1h (highest-timeframe-wins).
    quant_trend_filter_enabled: bool = False
    quant_trend_filter_min_abs_return: float = 0.003
    # Required edge when the chosen side opposes the 4h / 1h trend.
    quant_trend_opposed_strong_min_edge: float = 0.15  # vs 4h trend
    quant_trend_opposed_weak_min_edge: float = 0.06    # vs 1h trend only
    # Distressed market floor: block counter-trend entry when the ask on our side
    # is below this price. A low ask means the market has already heavily priced
    # in the opposing outcome — the GBM edge is structural lag, not real alpha.
    # E.g. 0.30 blocks buying YES when ask_yes < 0.30 in a downtrend. 0 = off.
    quant_trend_distressed_max_ask: float = 0.0
    # Unconditional minimum entry price: skip any trade where our side's ask is
    # below this floor regardless of trend direction. Very low ask prices (< 0.20)
    # have bid-ask spreads that exceed the stop-loss width, causing immediate
    # gap-out on entry. 0 = off.
    quant_min_entry_price: float = 0.0
    # Unconditional maximum entry price: skip any trade where our side's ask is
    # above this ceiling. Mid-band entries (≥ 0.50) empirically bleed 2–3× faster
    # than low-band trades because the fade signal is weakest near 0.50 — the
    # "least informed" zone where price is closest to a true coin-flip. 0 = off.
    quant_max_entry_price: float = 0.0

    # OFI gate: veto trades where strong signed order flow opposes the direction.
    # Informed flow (signed_flow_5s) is the single best short-term price-impact
    # predictor per Cont et al. 2014. Only fires when |flow| >= min_abs_flow.
    quant_ofi_gate_enabled: bool = False
    quant_ofi_gate_min_abs_flow: float = 30.0

    # Skip entries during the first N seconds of the 15-minute candle. The
    # drift-since-candle-open signal is unstable when the candle has barely
    # opened — soak attribution showed candle_phase=0-60s lost -$0.31/trade
    # vs +$0.35 in the 60-300s bucket. Only applies when the packet carries
    # a non-zero ``time_elapsed_in_candle_s``; threshold markets are unaffected.
    # 0 disables.
    quant_min_candle_elapsed_seconds: int = 0

    # Volatility regime gate: raise edge bar or abstain in high-vol conditions
    # where GBM fair-value estimates are less reliable (wider confidence intervals).
    quant_vol_regime_enabled: bool = False
    quant_vol_regime_high_threshold: float = 0.005    # 30m realized vol
    quant_vol_regime_extreme_threshold: float = 0.010
    quant_vol_regime_high_min_edge: float = 0.08

    quant_shadow_variant: str = ""
    # Magnitude of the HTF-trend tilt applied by the shadow scorer.
    # sign(btc_log_return_1h) * this value is added to the base fair_yes.
    # Calibrated from soak: 1h return predicted 57% of outcomes (vs 50% random)
    # while the base scorer agreed with the trend only 41% of the time.
    quant_shadow_htf_tilt_strength: float = 0.10
    # Per-session additive bias for the shadow scorer. Derived from observed
    # fair_yes vs actual YES-rate gaps in the first HTF soak (EU −14pp, US −27pp).
    # These are regime-conditional and should be re-calibrated after each soak.
    quant_shadow_session_bias_eu: float = 0.0
    quant_shadow_session_bias_us: float = 0.0

    events_jsonl_max_bytes: int = 200_000_000
    events_jsonl_keep_tail_bytes: int = 50_000_000
    daemon_maintenance_interval_seconds: int = 3600
    daemon_heartbeat_interval_seconds: float = 5.0
    # How often the daemon sweeps resting paper-maker orders to re-quote
    # any whose desired price has drifted past the cancel threshold,
    # independent of WS event triggers. Closes the quiet-window gap
    # where a rest sits stale for minutes between WS ticks. 0 disables.
    daemon_maker_freshness_interval_seconds: float = 1.0
    daemon_prune_history_days: int = 14
    daemon_heartbeat_stale_seconds: float = 30.0
    daemon_ws_stale_seconds: float = 60.0

    data_dir: Path = Field(default=Path("data"))
    log_dir: Path = Field(default=Path("logs"))
    db_path: Path = Field(default=Path("data/agent.db"))
    events_path: Path = Field(default=Path("logs/events.jsonl"))
    heartbeat_path: Path = Field(default=Path("data/daemon_heartbeat.json"))
    backups_dir: Path = Field(default=Path("data/backups"))
    runtime_settings_path: Path = Field(default=Path("data/runtime_settings.json"))


EDITABLE_SETTINGS_METADATA: dict[str, dict[str, Any]] = {
    # Every field here must also appear in INITIAL_SETTINGS_BASELINE — enforced
    # by tests/test_initial_settings.py. Fields marked requires_restart=True
    # are still editable via the API/CLI but the daemon's reload loop surfaces
    # the flag to the operator instead of hot-swapping them.
    "trading_mode": {"label": "Mode", "type": "select", "options": ["paper", "live"], "group": "runtime", "requires_restart": True},
    "market_family": {
        "label": "Market Family",
        "type": "select",
        "options": ["btc_1h", "btc_15m", "btc_5m", "btc_daily_threshold"],
        "group": "runtime",
        "requires_restart": True,
    },
    "loop_seconds": {"label": "Loop Seconds", "type": "number", "min": 1, "max": 300, "step": 1, "group": "runtime"},
    "openrouter_model": {"label": "OpenRouter Model", "type": "text", "group": "runtime"},
    "daemon_auto_paper_execute": {
        "label": "Daemon Auto Paper Execute",
        "type": "boolean",
        "group": "runtime",
        "requires_restart": True,
    },
    "live_trading_enabled": {"label": "Live Trading Enabled", "type": "boolean", "group": "live"},
    "live_order_type": {"label": "Live Order Type", "type": "select", "options": ["FOK", "GTC"], "group": "live"},
    "live_post_only": {"label": "Live Post Only", "type": "boolean", "group": "live"},
    "max_position_usd": {"label": "Max Position USD", "type": "number", "min": 1, "max": 100000, "step": 0.5, "group": "thresholds"},
    "min_confidence": {"label": "Min Confidence", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "min_edge": {"label": "Min Edge", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "max_spread": {"label": "Max Spread", "type": "number", "min": 0, "max": 1, "step": 0.001, "group": "thresholds"},
    "min_depth_usd": {"label": "Min Depth USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_seconds": {"label": "Exit Buffer Seconds", "type": "number", "min": 0, "max": 3600, "step": 1, "group": "thresholds"},
    "max_daily_loss_usd": {"label": "Max Daily Loss USD", "type": "number", "min": 0, "max": 100000, "step": 0.5, "group": "thresholds"},
    "stale_data_seconds": {"label": "Stale Data Seconds", "type": "number", "min": 1, "max": 3600, "step": 1, "group": "thresholds"},
    "max_rejected_orders": {"label": "Max Rejected Orders", "type": "number", "min": 1, "max": 100, "step": 1, "group": "thresholds"},
    "max_concurrent_positions": {"label": "Max Concurrent Positions", "type": "number", "min": 1, "max": 20, "step": 1, "group": "thresholds"},
    "max_net_btc_exposure_usd": {"label": "Max Net BTC Exposure USD", "type": "number", "min": 0, "max": 1000000, "step": 1, "group": "thresholds"},
    "exit_buffer_pct_of_tte": {"label": "Exit Buffer % of TTE", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "paper_starting_balance_usd": {
        "label": "Paper Starting Balance USD",
        "type": "number",
        "min": 0,
        "max": 1000000,
        "step": 1,
        "group": "paper",
        "requires_restart": True,
    },
    "paper_starting_balance_per_strategy": {
        "label": "Paper Starting Balance Per Strategy (id:amount,...)",
        "type": "text",
        "group": "paper",
    },
    "paper_position_ttl_seconds": {
        "label": "Paper Position TTL Seconds",
        "type": "number",
        "min": 1,
        "max": 86400,
        "step": 1,
        "group": "paper",
    },
    "paper_entry_slippage_bps": {
        "label": "Paper Entry Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    "paper_exit_slippage_bps": {
        "label": "Paper Exit Slippage BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    "paper_follow_limit_discount_bps": {
        "label": "Follow Maker Discount BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 1,
        "group": "paper",
    },
    "paper_follow_maker_ttl_seconds": {
        "label": "Follow Maker TTL (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "paper_follow_cancel_price_threshold": {
        "label": "Follow Maker Cancel Price Threshold",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.001,
        "group": "paper",
    },
    "paper_follow_cancel_size_threshold_pct": {
        "label": "Follow Maker Cancel Size Threshold (%)",
        "type": "number",
        "min": 0,
        "max": 100,
        "step": 0.1,
        "group": "paper",
    },
    "paper_follow_min_level_size_shares": {
        "label": "Follow Maker Min Level Size (shares)",
        "type": "number",
        "min": 0,
        "max": 100000,
        "step": 1,
        "group": "paper",
    },
    "fade_post_only": {
        "label": "Fade Post-Only (route through paper-maker)",
        "type": "boolean",
        "group": "paper",
    },
    "adaptive_enabled": {
        "label": "Adaptive (V1, fade-clone) Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "penny_enabled": {
        "label": "Penny Strategy Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "penny_entry_thresh": {
        "label": "Penny Entry Threshold (ask ≤)",
        "type": "number",
        "min": 0,
        "max": 0.2,
        "step": 0.005,
        "group": "paper",
    },
    "penny_min_entry_tte_seconds": {
        "label": "Penny Min Entry TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 10,
        "group": "paper",
    },
    "penny_force_exit_tte_seconds": {
        "label": "Penny Force-Exit TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 10,
        "group": "paper",
    },
    "penny_tp_multiple": {
        "label": "Penny TP Multiple (× entry)",
        "type": "number",
        "min": 1,
        "max": 100,
        "step": 0.25,
        "group": "paper",
    },
    "penny_size_usd": {
        "label": "Penny Position Size (USD)",
        "type": "number",
        "min": 0.1,
        "max": 100,
        "step": 0.1,
        "group": "paper",
    },
    "penny_stop_loss_multiple": {
        "label": "Penny Stop-Loss Multiple (× entry)",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.05,
        "group": "paper",
    },
    "penny_min_favorable_move_bps": {
        "label": "Penny Min Favorable Move (bps over 30s)",
        "type": "number",
        "min": 0,
        "max": 2000,
        "step": 5,
        "group": "paper",
    },
    "adaptive_v2_enabled": {
        "label": "Adaptive V2 (Overreaction-Fade) Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "adaptive_v2_overreaction_threshold": {
        "label": "Adaptive V2 Overreaction Threshold",
        "type": "number",
        "min": 0,
        "max": 0.5,
        "step": 0.005,
        "group": "paper",
    },
    "adaptive_v2_sensitivity": {
        "label": "Adaptive V2 BTC→PM Sensitivity",
        "type": "number",
        "min": 0.5,
        "max": 50,
        "step": 0.5,
        "group": "paper",
    },
    "adaptive_v2_cost_floor": {
        "label": "Adaptive V2 Cost Floor (edge subtracted)",
        "type": "number",
        "min": 0,
        "max": 0.1,
        "step": 0.001,
        "group": "paper",
    },
    "adaptive_v2_min_seconds_to_expiry": {
        "label": "Adaptive V2 Min TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 900,
        "step": 10,
        "group": "paper",
    },
    "adaptive_v2_max_abs_edge": {
        "label": "Adaptive V2 Max |Edge| Ceiling",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "thresholds",
    },
    "adaptive_v2_post_only": {
        "label": "Adaptive V2 Post-Only (paper-maker lifecycle)",
        "type": "boolean",
        "group": "paper",
    },
    "adaptive_v2_stop_loss_pct": {
        "label": "Adaptive V2 Stop Loss % (overrides global)",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "adaptive_v2_invert": {
        "label": "Adaptive V2 Invert (continuation instead of reversion)",
        "type": "boolean",
        "group": "thresholds",
    },
    "adaptive_v2_imbalance_gate_enabled": {
        "label": "Adaptive V2 Imbalance Gate Enabled",
        "type": "boolean",
        "group": "thresholds",
    },
    "adaptive_v2_imbalance_gate_min_abs": {
        "label": "Adaptive V2 Imbalance Gate Min |Imbalance|",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "thresholds",
    },
    "adaptive_v2_min_candle_elapsed_seconds": {
        "label": "Adaptive V2 Min Candle Elapsed (s)",
        "type": "number",
        "min": 0,
        "max": 900,
        "step": 1,
        "group": "thresholds",
    },
    "mm_enabled": {
        "label": "Market Maker Strategy Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "mm_size_usd": {
        "label": "MM Size per Leg (USD)",
        "type": "number",
        "min": 0.1,
        "max": 100,
        "step": 0.1,
        "group": "paper",
    },
    "mm_target_half_spread": {
        "label": "MM Target Half-Spread",
        "type": "number",
        "min": 0.001,
        "max": 0.5,
        "step": 0.001,
        "group": "paper",
    },
    "mm_min_market_spread": {
        "label": "MM Min Market Spread (skip below)",
        "type": "number",
        "min": 0.0,
        "max": 0.5,
        "step": 0.001,
        "group": "paper",
    },
    "mm_max_market_spread": {
        "label": "MM Max Market Spread (skip above)",
        "type": "number",
        "min": 0.0,
        "max": 1.0,
        "step": 0.001,
        "group": "paper",
    },
    "mm_min_tte_seconds": {
        "label": "MM Min TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 5,
        "group": "paper",
    },
    "mm_inventory_skew_strength": {
        "label": "MM Inventory Skew Strength",
        "type": "number",
        "min": 0.0,
        "max": 2.0,
        "step": 0.05,
        "group": "paper",
    },
    "mm_max_inventory_usd": {
        "label": "MM Max Inventory per Side (USD)",
        "type": "number",
        "min": 0.0,
        "max": 1000,
        "step": 0.5,
        "group": "paper",
    },
    "mm_require_rewards": {
        "label": "MM Require Reward-Paying Markets",
        "type": "boolean",
        "group": "paper",
    },
    "mm_quote_ttl_seconds": {
        "label": "MM Quote TTL (seconds)",
        "type": "number",
        "min": 5,
        "max": 3600,
        "step": 5,
        "group": "paper",
    },
    "mm_replace_min_ticks": {
        "label": "MM Replace Min Ticks (hysteresis)",
        "type": "number",
        "min": 0.0,
        "max": 10.0,
        "step": 0.5,
        "group": "paper",
    },
    "mm_replace_min_size_pct": {
        "label": "MM Replace Min Size Drift (%)",
        "type": "number",
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "group": "paper",
    },
    "mm_force_exit_tte_seconds": {
        "label": "MM Force Exit TTE (seconds)",
        "type": "number",
        "min": 0,
        "max": 600,
        "step": 5,
        "group": "paper",
    },
    "mm_freshness_interval_seconds": {
        "label": "MM Freshness Sweep Interval (seconds)",
        "type": "number",
        "min": 0,
        "max": 60,
        "step": 0.5,
        "group": "paper",
    },
    "mm_max_fill_drift_pct": {
        "label": "MM Max Fill Drift % (refuse adverse-selection fills)",
        "type": "number",
        "min": 0.0,
        "max": 50.0,
        "step": 0.5,
        "group": "paper",
    },
    "mm_no_fill_tte_seconds": {
        "label": "MM No-Fill TTE Floor (seconds)",
        "type": "number",
        "min": 0,
        "max": 600,
        "step": 5,
        "group": "paper",
    },
    "mm_max_quote_age_seconds": {
        "label": "MM Max Quote Age (seconds, defense-in-depth)",
        "type": "number",
        "min": 0,
        "max": 7200,
        "step": 30,
        "group": "paper",
    },
    "mm_universe_enabled": {
        "label": "MM Universe Scanner Enabled",
        "type": "boolean",
        "group": "paper",
    },
    "mm_universe_min_rewards_daily_usd": {
        "label": "MM Min Rewards Daily (USD)",
        "type": "number",
        "min": 0.0,
        "max": 10000.0,
        "step": 0.5,
        "group": "paper",
    },
    "mm_universe_min_liquidity_usd": {
        "label": "MM Min Liquidity (USD)",
        "type": "number",
        "min": 0.0,
        "max": 1_000_000.0,
        "step": 500.0,
        "group": "paper",
    },
    "mm_universe_min_tte_seconds": {
        "label": "MM Min TTE for Universe (seconds)",
        "type": "number",
        "min": 60,
        "max": 30 * 24 * 3600,
        "step": 60,
        "group": "paper",
    },
    "mm_universe_max_markets": {
        "label": "MM Max Markets to Subscribe",
        "type": "number",
        "min": 1,
        "max": 50,
        "step": 1,
        "group": "paper",
    },
    "mm_universe_refresh_seconds": {
        "label": "MM Universe Refresh (seconds)",
        "type": "number",
        "min": 30,
        "max": 3600,
        "step": 30,
        "group": "paper",
    },
    "mm_universe_require_size_eligible": {
        "label": "MM Require Size-Eligible Markets (rewardsMinSize ≤ mm_size_usd)",
        "type": "boolean",
        "group": "paper",
    },
    "paper_take_profit_pct": {
        "label": "Paper Take Profit %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_stop_loss_pct": {
        "label": "Paper Stop Loss %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_trailing_stop_pct": {
        "label": "Paper Trailing Stop %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_tp_ladder": {
        "label": "Paper TP Ladder (pct:fraction,...)",
        "type": "text",
        "group": "paper",
    },
    "paper_trail_arm_pct": {
        "label": "Paper Trail Arm %",
        "type": "number",
        "min": 0,
        "max": 1,
        "step": 0.01,
        "group": "paper",
    },
    "paper_trail_confirmation_ticks": {
        "label": "Paper Trail Confirmation Ticks",
        "type": "number",
        "min": 0,
        "max": 20,
        "step": 1,
        "group": "paper",
    },
    "paper_sl_limit_ttl_ticks": {
        "label": "Paper SL Limit-Out TTL (ticks)",
        "type": "number",
        "min": 0,
        "max": 20,
        "step": 1,
        "group": "paper",
    },
    "paper_sl_limit_slippage_ticks": {
        "label": "Paper SL Limit Slippage (ticks)",
        "type": "number",
        "min": 0,
        "max": 10,
        "step": 1,
        "group": "paper",
    },
    "min_exit_depth_multiplier": {
        "label": "Min Exit-Side Bid Depth (× position size)",
        "type": "number",
        "min": 0,
        "max": 50,
        "step": 0.1,
        "group": "thresholds",
    },
    "paper_entry_cooldown_seconds": {
        "label": "Paper Entry Cooldown Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "min_entry_tte_seconds": {
        "label": "Min Entry TTE Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "position_force_exit_tte_seconds": {
        "label": "Position Force-Exit TTE Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "paper",
    },
    "max_consecutive_losses": {
        "label": "Max Consecutive Losses",
        "type": "number",
        "min": 0,
        "max": 100,
        "step": 1,
        "group": "thresholds",
    },
    "min_candle_elapsed_seconds": {
        "label": "Min Candle Elapsed Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "max_candle_elapsed_seconds": {
        "label": "Max Candle Elapsed Seconds",
        "type": "number",
        "min": 0,
        "max": 3600,
        "step": 1,
        "group": "thresholds",
    },
    "fee_bps": {
        "label": "Fee BPS",
        "type": "number",
        "min": 0,
        "max": 10000,
        "step": 0.1,
        "group": "paper",
    },
    # Quant scorer gates
    "quant_invert_drift": {"label": "Invert Drift", "type": "boolean", "group": "thresholds"},
    "quant_drift_damping": {"label": "Drift Damping", "type": "number", "min": 0, "max": 5, "step": 0.05, "group": "thresholds"},
    "quant_max_abs_edge": {"label": "Max |Edge| Ceiling", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_filter_enabled": {"label": "Trend Filter Enabled", "type": "boolean", "group": "thresholds"},
    "quant_trend_filter_min_abs_return": {"label": "Trend Filter Min |Return|", "type": "number", "min": 0, "max": 0.1, "step": 0.0005, "group": "thresholds"},
    "quant_trend_opposed_strong_min_edge": {"label": "Counter-Trend Min Edge (4h)", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_opposed_weak_min_edge": {"label": "Counter-Trend Min Edge (1h)", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_trend_distressed_max_ask": {"label": "Distressed Max Ask", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_min_entry_price": {"label": "Min Entry Price", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_max_entry_price": {"label": "Max Entry Price", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_ofi_gate_enabled": {"label": "OFI Gate Enabled", "type": "boolean", "group": "thresholds"},
    "quant_ofi_gate_min_abs_flow": {"label": "OFI Gate Min |Flow|", "type": "number", "min": 0, "max": 10000, "step": 1, "group": "thresholds"},
    "quant_min_candle_elapsed_seconds": {"label": "Quant Min Candle Elapsed (s)", "type": "number", "min": 0, "max": 900, "step": 1, "group": "thresholds"},
    "quant_vol_regime_enabled": {"label": "Vol Regime Enabled", "type": "boolean", "group": "thresholds"},
    "quant_vol_regime_high_threshold": {"label": "Vol Regime High Threshold", "type": "number", "min": 0, "max": 1, "step": 0.0005, "group": "thresholds"},
    "quant_vol_regime_extreme_threshold": {"label": "Vol Regime Extreme Threshold", "type": "number", "min": 0, "max": 1, "step": 0.0005, "group": "thresholds"},
    "quant_vol_regime_high_min_edge": {"label": "Vol Regime High Min Edge", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_variant": {"label": "Shadow Variant", "type": "text", "group": "thresholds"},
    "quant_shadow_htf_tilt_strength": {"label": "Shadow HTF Tilt Strength", "type": "number", "min": 0, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_session_bias_eu": {"label": "Shadow Session Bias (EU)", "type": "number", "min": -1, "max": 1, "step": 0.01, "group": "thresholds"},
    "quant_shadow_session_bias_us": {"label": "Shadow Session Bias (US)", "type": "number", "min": -1, "max": 1, "step": 0.01, "group": "thresholds"},
}

# Mark fields requires_restart=True on their metadata entries above. Mirrors
# initial_settings.REQUIRES_RESTART so callers that touch config.py don't
# have to import two modules.
REQUIRES_RESTART_FIELDS: frozenset[str] = frozenset(
    key for key, meta in EDITABLE_SETTINGS_METADATA.items() if meta.get("requires_restart")
)



# Cache Settings, but invalidate when .env changes so a live edit (e.g. MARKET_FAMILY)
# takes effect without restarting the API / daemon process.
_ENV_FILE = Path(".env")
_settings_cache: dict[str, Any] = {"settings": None, "env_mtime": None}


def _env_mtime() -> float | None:
    try:
        return _ENV_FILE.stat().st_mtime if _ENV_FILE.exists() else None
    except OSError:
        return None


def get_settings() -> Settings:
    current_mtime = _env_mtime()
    cached = _settings_cache["settings"]
    if cached is not None and _settings_cache["env_mtime"] == current_mtime:
        return cached
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.events_path.parent.mkdir(parents=True, exist_ok=True)
    settings.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    _settings_cache["settings"] = settings
    _settings_cache["env_mtime"] = current_mtime
    return settings


def _settings_store_for(settings: Settings):
    # Local import avoids a config ↔ engine cycle at module import time.
    from polymarket_trading_engine.engine.settings_store import SettingsStore

    return SettingsStore(settings.db_path)


def load_runtime_overrides(settings: Settings) -> dict[str, Any]:
    """Return the editable-field overrides currently in the DB.

    Filtered through ``EDITABLE_SETTINGS_METADATA`` so stray rows for fields
    that have since been removed from the whitelist don't leak into the
    effective settings. Safe to call before the DB has been initialised;
    returns ``{}`` if the table doesn't exist yet.
    """
    try:
        overrides = _settings_store_for(settings).current_overrides()
    except Exception:
        # DB not yet migrated (table missing) or unreadable — treat as no
        # overrides and let the caller fall back to code defaults.
        return {}
    return {key: value for key, value in overrides.items() if key in EDITABLE_SETTINGS_METADATA}


def save_runtime_overrides(settings: Settings, updates: dict[str, Any]) -> dict[str, Any]:
    """Persist ``updates`` as new ``settings_changes`` rows and return the
    materialised effective overrides after the write.

    Only fields that (a) appear in ``EDITABLE_SETTINGS_METADATA`` and (b)
    differ from the current effective value are recorded — idempotent writes
    don't pollute the audit trail.
    """
    editable_updates = {key: value for key, value in updates.items() if key in EDITABLE_SETTINGS_METADATA}
    if not editable_updates:
        return load_runtime_overrides(settings)
    # Validate via pydantic so string→int/bool coercion matches the Settings
    # class — keeps the DB clean of weird string "true" values where bools
    # are expected.
    current_overrides = load_runtime_overrides(settings)
    candidate = Settings.model_validate({**settings.model_dump(), **current_overrides, **editable_updates})
    effective_now = Settings.model_validate({**settings.model_dump(), **current_overrides})
    changes: list[tuple[str, Any, Any]] = []
    for key in editable_updates:
        new_value = getattr(candidate, key)
        old_value = getattr(effective_now, key)
        if new_value != old_value:
            changes.append((key, old_value, new_value))
    if changes:
        _settings_store_for(settings).record_changes(changes, source="api")
    return load_runtime_overrides(settings)


def get_effective_settings() -> Settings:
    base = get_settings()
    overrides = load_runtime_overrides(base)
    if not overrides:
        return base
    effective = Settings.model_validate({**base.model_dump(), **overrides})
    effective.data_dir.mkdir(parents=True, exist_ok=True)
    effective.log_dir.mkdir(parents=True, exist_ok=True)
    effective.db_path.parent.mkdir(parents=True, exist_ok=True)
    effective.events_path.parent.mkdir(parents=True, exist_ok=True)
    effective.runtime_settings_path.parent.mkdir(parents=True, exist_ok=True)
    return effective


def diff_editable(old: Settings, new: Settings) -> dict[str, dict[str, Any]]:
    """Return ``{field: {'before': old_value, 'after': new_value}}`` for every
    editable field whose value changed between ``old`` and ``new``.

    Used by the daemon reload loop to emit precise ``settings_changed``
    journal events and by the API handler to skip no-op writes.
    """
    out: dict[str, dict[str, Any]] = {}
    for field in EDITABLE_SETTINGS_METADATA:
        before = getattr(old, field, None)
        after = getattr(new, field, None)
        if before != after:
            out[field] = {"before": before, "after": after}
    return out


def editable_values_snapshot(settings: Settings) -> dict[str, Any]:
    """Flat ``{field: value}`` snapshot of every editable field — the shape the
    daemon emits on startup as the baseline for audit segmentation.
    """
    return {field: getattr(settings, field, None) for field in EDITABLE_SETTINGS_METADATA}


def runtime_settings_payload(settings: Settings) -> dict[str, Any]:
    overrides = load_runtime_overrides(settings)
    return {
        "values": {key: getattr(settings, key) for key in EDITABLE_SETTINGS_METADATA},
        "overrides": overrides,
        "fields": EDITABLE_SETTINGS_METADATA,
    }


@dataclass(slots=True, frozen=True)
class RiskProfile:
    """Resolved per-family risk parameters consumed by :class:`RiskEngine`.

    The profile is derived from :class:`Settings` via :func:`resolve_risk_profile`:
    if a field was explicitly overridden on ``Settings`` the override wins, so
    existing deployments that tune globals via env vars are unchanged. If the
    field is left at the built-in default and the active ``market_family`` has
    a per-family override in :data:`FAMILY_PROFILE_OVERRIDES`, the tighter
    family value is applied.
    """

    family: str
    min_edge: float
    max_spread: float
    min_depth_usd: float
    stale_data_seconds: int
    exit_buffer_floor_seconds: int
    exit_buffer_pct_of_tte: float
    family_window_seconds: int
    max_position_usd: float
    max_concurrent_positions: int
    max_net_btc_exposure_usd: float


FAMILY_PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    "btc_1h": {
        "stale_data_seconds": 5,
        "exit_buffer_pct_of_tte": 0.05,
        "max_concurrent_positions": 2,
        "family_window_seconds": 3600,
    },
    "btc_15m": {
        "stale_data_seconds": 3,
        "exit_buffer_pct_of_tte": 0.07,
        "max_concurrent_positions": 2,
        "family_window_seconds": 900,
    },
    "btc_5m": {
        "stale_data_seconds": 2,
        "exit_buffer_pct_of_tte": 0.10,
        "max_concurrent_positions": 1,
        "family_window_seconds": 300,
    },
}


_PROFILE_FIELD_TO_SETTING = {
    "stale_data_seconds": "stale_data_seconds",
    "exit_buffer_floor_seconds": "exit_buffer_seconds",
    "exit_buffer_pct_of_tte": "exit_buffer_pct_of_tte",
    "max_position_usd": "max_position_usd",
    "max_concurrent_positions": "max_concurrent_positions",
}


def resolve_risk_profile(settings: Settings) -> RiskProfile:
    family = settings.market_family
    overrides = FAMILY_PROFILE_OVERRIDES.get(family, {})
    explicit = settings.model_fields_set

    def pick(profile_field: str) -> Any:
        settings_field = _PROFILE_FIELD_TO_SETTING.get(profile_field, profile_field)
        # Explicit operator override wins.
        if settings_field in explicit:
            return getattr(settings, settings_field)
        if profile_field in overrides:
            return overrides[profile_field]
        return getattr(settings, settings_field)

    family_window = int(overrides.get("family_window_seconds", 0))
    return RiskProfile(
        family=family,
        min_edge=settings.min_edge,
        max_spread=settings.max_spread,
        min_depth_usd=settings.min_depth_usd,
        stale_data_seconds=int(pick("stale_data_seconds")),
        exit_buffer_floor_seconds=int(pick("exit_buffer_floor_seconds")),
        exit_buffer_pct_of_tte=float(pick("exit_buffer_pct_of_tte")),
        family_window_seconds=family_window,
        max_position_usd=float(pick("max_position_usd")),
        max_concurrent_positions=int(pick("max_concurrent_positions")),
        max_net_btc_exposure_usd=float(settings.max_net_btc_exposure_usd),
    )
