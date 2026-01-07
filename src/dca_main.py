#!/usr/bin/env python3
"""
Extended Exchange DCA Bot - Main Entry Point

A Dollar Cost Averaging (DCA) bot for Extended Exchange.
Implements safety orders to average down entry price and maximize profit potential.

Usage:
    python -m src.dca_main --env mainnet --profile DCA_ETH
    python -m src.dca_main --env testnet --profile DCA_ETH --dry-run
"""

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path
import logging
import yaml

from .config import (
    BotConfig,
    create_config,
    validate_config,
    Environment,
    load_env_credentials,
    load_profile,
    RiskConfig,
    OrderConfig,
    StrategyConfig,
)
from .api_client import ExtendedAPIClient, test_api_connection
from .signing import StarkSigner, update_market_l2_config, create_order_builder
from .order_manager import OrderManager
from .risk import RiskManager, Position
from .strategy_dca import DCAStrategy, DCAConfig, DCADirection, DCATradeState
from .metrics import MetricsCollector, setup_logging
from .indicators import TechnicalIndicators, Signal, get_entry_signal

logger = logging.getLogger(__name__)


class DCABot:
    """
    DCA Bot orchestrator.

    Coordinates DCA strategy with API client, order management,
    and risk management.
    """

    def __init__(self, config: BotConfig, dca_config: DCAConfig, indicators_config: dict = None):
        self.config = config
        self.dca_config = dca_config
        self.indicators_config = indicators_config or {}
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Components
        self.api_client: ExtendedAPIClient = None
        self.order_manager: OrderManager = None
        self.risk_manager: RiskManager = None
        self.dca_strategy: DCAStrategy = None
        self.metrics: MetricsCollector = None
        self.indicators: TechnicalIndicators = None

        # Market info
        self._tick_size = 0.01
        self._min_order_size = 0.001

        # Indicators state
        self._indicators_enabled = self.indicators_config.get("enabled", False)
        self._last_indicator_check = 0
        self._indicator_update_interval = self.indicators_config.get("update_interval_sec", 60)
        self._candle_timeframe = self.indicators_config.get("candle_timeframe_sec", 900)  # 15m default
        self._candles_count = self.indicators_config.get("candles_count", 250)
        self._min_confirmations = self.indicators_config.get("min_confirmations", 2)
        self._cached_prices = []
        self._last_signal = None

    async def start(self):
        """Initialize and start the bot."""
        logger.info("=" * 60)
        logger.info("EXTENDED DCA BOT - STARTING")
        logger.info("=" * 60)

        # Print configuration
        self._print_dca_config()

        # Validate configuration
        errors = validate_config(self.config)
        if errors:
            for error in errors:
                logger.error(f"Config error: {error}")
            if not self.config.dry_run:
                raise ValueError("Configuration validation failed")
            logger.warning("Continuing in dry-run mode despite config errors")

        # Initialize components
        await self._initialize_components()

        # Run pre-flight checks
        if not await self._preflight_checks():
            raise RuntimeError("Pre-flight checks failed")

        # Setup signal handlers
        self._setup_signal_handlers()

        # Start bot
        self._running = True
        logger.info("DCA Bot started successfully")

        # Main loop
        await self._main_loop()

    async def stop(self, reason: str = "shutdown"):
        """Stop the bot gracefully."""
        logger.info(f"Stopping DCA bot: {reason}")
        self._running = False

        # Cancel all orders
        if self.dca_strategy:
            await self.dca_strategy.cancel_all_orders()

        if self.order_manager:
            await self.order_manager.cancel_all_quotes()

        # Close API client
        if self.api_client:
            await self.api_client.close()

        # Final metrics report
        if self.metrics:
            self.metrics.report()

        logger.info("DCA Bot stopped")
        self._shutdown_event.set()

    def _print_dca_config(self):
        """Print DCA configuration summary."""
        cfg = self.dca_config
        ind = self.indicators_config
        print("\n" + "=" * 60)
        print("DCA BOT CONFIGURATION")
        print("=" * 60)
        print(f"Environment:        {self.config.environment.value.upper()}")
        print(f"Dry Run:            {self.config.dry_run}")
        print(f"Market:             {cfg.market}")
        print(f"Direction:          {cfg.direction.value}")
        print("-" * 60)
        print("ENTRY:")
        print(f"  Base Order:       ${cfg.base_order_size_usd:.2f}")
        print(f"  Start Immediately: {cfg.start_immediately}")
        print("-" * 60)
        print("SAFETY ORDERS:")
        print(f"  Max Safety Orders: {cfg.max_safety_orders}")
        print(f"  First SO Size:    ${cfg.safety_order_size_usd:.2f}")
        print(f"  Price Deviation:  {cfg.price_deviation_pct:.1f}%")
        print(f"  Step Scale:       {cfg.safety_order_step_scale}x")
        print(f"  Volume Scale:     {cfg.safety_order_volume_scale}x")

        # Calculate total capital needed
        total_capital = cfg.base_order_size_usd
        so_size = cfg.safety_order_size_usd
        for i in range(cfg.max_safety_orders):
            total_capital += so_size
            so_size *= cfg.safety_order_volume_scale
        print(f"  Total Capital:    ${total_capital:.2f}")
        print("-" * 60)
        print("TAKE PROFIT / STOP LOSS:")
        print(f"  Take Profit:      {cfg.take_profit_pct:.1f}%")
        print(f"  Trailing TP:      {cfg.trailing_take_profit}")
        if cfg.stop_loss_pct > 0:
            print(f"  Stop Loss:        {cfg.stop_loss_pct:.1f}%")
        else:
            print(f"  Stop Loss:        Disabled")
        print("-" * 60)

        # Indicators section
        if ind.get("enabled", False):
            print("TECHNICAL INDICATORS:")
            strategy = ind.get("strategy", "permissive")
            min_conf = ind.get("min_confirmations", 2)
            timeframe = ind.get("candle_timeframe_sec", 900)
            print(f"  Strategy:         {strategy.upper()} ({min_conf}/4 confirmations)")
            print(f"  Timeframe:        {timeframe // 60}m candles")

            rsi = ind.get("rsi", {})
            if rsi.get("enabled", True):
                print(f"  RSI:              Period={rsi.get('period', 14)}, Oversold<{rsi.get('oversold', 35)}, Overbought>{rsi.get('overbought', 65)}")

            macd = ind.get("macd", {})
            if macd.get("enabled", True):
                print(f"  MACD:             {macd.get('fast_period', 12)}/{macd.get('slow_period', 26)}/{macd.get('signal_period', 9)}")

            bb = ind.get("bollinger", {})
            if bb.get("enabled", True):
                print(f"  Bollinger:        Period={bb.get('period', 20)}, StdDev={bb.get('std_dev', 2.0)}")

            ema = ind.get("ema", {})
            if ema.get("enabled", True):
                print(f"  EMA:              Period={ema.get('period', 200)}")
            print("-" * 60)
        else:
            print("TECHNICAL INDICATORS: Disabled")
            print("-" * 60)

        print(f"Cooldown:           {cfg.cooldown_between_trades_sec}s")
        if cfg.entry_refresh_seconds > 0:
            print(f"Entry Refresh:      {cfg.entry_refresh_seconds}s")
        else:
            print(f"Entry Refresh:      Disabled")
        print("=" * 60 + "\n")

    async def _initialize_components(self):
        """Initialize all bot components."""
        logger.info("Initializing components...")

        # Metrics
        self.metrics = MetricsCollector(self.config)

        # API Client
        self.api_client = ExtendedAPIClient(self.config)
        await self.api_client.start()

        # Order Manager
        self.order_manager = OrderManager(
            config=self.config,
            api_client=self.api_client,
            private_key=self.config.stark_private_key
        )
        await self.order_manager.initialize()

        # Risk Manager
        self.risk_manager = RiskManager(self.config)

        # DCA Strategy (initialized after market info is fetched)
        logger.info("Components initialized")

    async def _preflight_checks(self) -> bool:
        """Run pre-flight checks before starting."""
        logger.info("Running pre-flight checks...")

        market = self.dca_config.market

        # 1. Test API connectivity
        logger.info("Checking API connectivity...")
        if not await test_api_connection(self.config):
            logger.error("API connectivity check failed")
            if not self.config.dry_run:
                return False

        # 2. Get market info
        logger.info(f"Fetching market info for {market}...")
        try:
            market_info = await self.api_client.get_market_info(market)
            logger.info(f"[OK] Market info: {market_info}")

            # Extract tick size and min order size from tradingConfig
            trading_config = market_info.get("tradingConfig", {})
            self._tick_size = float(trading_config.get("minPriceChange", 0.1))
            self._min_order_size = float(trading_config.get("minOrderSize", 0.01))

            logger.info(f"Tick size: {self._tick_size}, Min order size: {self._min_order_size}")

            # Update L2 config for signing
            if "l2Config" in market_info:
                update_market_l2_config(market, market_info["l2Config"])
            elif "syntheticAssetId" in market_info:
                update_market_l2_config(market, market_info)

        except Exception as e:
            logger.error(f"Failed to get market info: {e}")
            if not self.config.dry_run:
                return False

        # 3. Check balance
        logger.info("Checking account balance...")
        balance_response = await self.api_client.get_balance()
        if balance_response.success:
            balance_data = balance_response.data
            logger.info(f"[OK] Balance: {balance_data}")

            if isinstance(balance_data, dict):
                inner_data = balance_data.get("data", balance_data)
                if isinstance(inner_data, dict):
                    usd_balance = float(inner_data.get("balance",
                                       inner_data.get("equity",
                                       inner_data.get("USD", 100))))
                else:
                    usd_balance = 100
            else:
                usd_balance = 100

            # Update risk limits
            self.config.risk.max_inventory_usd = usd_balance
            self.config.risk.max_position_notional = usd_balance * self.config.risk.max_leverage

            logger.info(f"Balance: ${usd_balance:.2f}")
            self.risk_manager.initialize(usd_balance)

            # Calculate total capital needed for DCA
            total_needed = self._calculate_total_capital_needed()
            if total_needed > usd_balance * self.config.risk.max_leverage:
                logger.warning(
                    f"DCA strategy requires ${total_needed:.2f} but balance is ${usd_balance:.2f} "
                    f"(with {self.config.risk.max_leverage}x leverage = ${usd_balance * self.config.risk.max_leverage:.2f})"
                )
        else:
            logger.warning(f"Could not fetch balance: {balance_response.error}")
            self.risk_manager.initialize(100)

        # 4. Initialize DCA Strategy
        self.dca_strategy = DCAStrategy(
            config=self.dca_config,
            api_client=self.api_client,
            order_builder=self.order_manager.order_builder,
            tick_size=self._tick_size,
            min_order_size=self._min_order_size
        )

        # 5. Cancel existing orders
        logger.info("Cancelling existing orders...")
        await self.order_manager.cancel_all_quotes(market)

        logger.info("[OK] Pre-flight checks completed")
        return True

    def _calculate_total_capital_needed(self) -> float:
        """Calculate total capital needed for DCA strategy."""
        cfg = self.dca_config
        total = cfg.base_order_size_usd
        so_size = cfg.safety_order_size_usd

        for i in range(cfg.max_safety_orders):
            total += so_size
            so_size *= cfg.safety_order_volume_scale

        return total

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}")
            asyncio.create_task(self.stop("signal"))

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    async def _main_loop(self):
        """Main bot loop."""
        market = self.dca_config.market
        loop_interval = 5.0  # Check every 5 seconds

        while self._running:
            try:
                # Get current price and orderbook data (now includes mark_price from marketStats)
                current_price, best_bid, best_ask, mark_price = await self._get_current_price(market)

                if current_price <= 0:
                    logger.warning("Could not get current price, waiting...")
                    await asyncio.sleep(loop_interval)
                    continue

                # Sync position from exchange, passing mark_price for PnL calculation
                position_size, pos_mark_price, exchange_pnl, exchange_entry, margin = await self._sync_position(market, mark_price)

                # Use mark_price from marketStats (most accurate for PnL)
                price_for_pnl = mark_price if mark_price > 0 else current_price

                # Calculate exchange PnL percentage (matches Extended Exchange UI)
                # Formula: unrealisedPnl / margin × 100
                # Where margin = value / leverage (from position)
                if margin > 0:
                    exchange_pnl_pct = (exchange_pnl / margin) * 100
                else:
                    exchange_pnl_pct = 0.0

                # Check DCA strategy
                if self.dca_strategy.has_active_trade:
                    # Update existing trade using exchange PnL percentage for accurate TP/SL
                    await self.dca_strategy.check_and_update_trade(
                        price_for_pnl, position_size, best_bid, exchange_pnl, exchange_pnl_pct
                    )
                else:
                    # Start new trade if conditions met
                    # First check indicators if enabled
                    should_enter, entry_reason, signal_details = await self._check_indicator_signal(market)

                    if should_enter:
                        if self.dca_config.start_immediately or self._indicators_enabled:
                            logger.info(f"[DCA] Entry signal: {entry_reason}")
                            logger.info(f"Starting new DCA trade at best_bid=${best_bid:.2f} (mid=${current_price:.2f})")
                            await self.dca_strategy.start_new_trade(current_price, best_bid)
                            # Clear signal after entry
                            self._last_signal = None
                    else:
                        # Log why we're not entering (only periodically to avoid spam)
                        if self._last_indicator_check == time.time():
                            logger.info(f"[DCA] No entry: {entry_reason}")

                # Log status with exchange PnL and percentage
                status = self.dca_strategy.get_status()
                if status["has_active_trade"]:
                    trade = status["current_trade"]

                    logger.info(
                        f"[DCA] State: {trade['state']}, "
                        f"Mark: ${price_for_pnl:.2f}, "
                        f"Entry: ${exchange_entry:.2f}, "
                        f"xPnL: ${exchange_pnl:.4f} ({exchange_pnl_pct:+.3f}%), "
                        f"Size: {trade['total_size']:.4f}, "
                        f"SOs: {trade['filled_safety_orders']}"
                    )

                # Check kill switch
                should_kill, kill_reason = self.risk_manager.check_kill_switch()
                if should_kill:
                    logger.critical(f"KILL SWITCH ACTIVATED: {kill_reason}")
                    await self.stop(f"kill_switch:{kill_reason.value}")
                    break

                # Update metrics
                self._update_metrics()

                # Periodic report
                if self.metrics.should_report():
                    self.metrics.report()

                # Heartbeat
                if not self.config.dry_run:
                    await self.api_client.heartbeat()

                await asyncio.sleep(loop_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                self.risk_manager.record_error(str(e))
                await asyncio.sleep(1)

        # Wait for shutdown
        await self._shutdown_event.wait()

    async def _get_current_price(self, market: str) -> tuple[float, float, float, float]:
        """
        Get current market price and orderbook data.

        Returns:
            Tuple of (mid_price, best_bid, best_ask, mark_price)
        """
        try:
            mark_price = 0.0

            # Always get mark_price from market stats (most accurate for PnL)
            market_response = await self.api_client.get_market(market)
            if market_response.success and market_response.data:
                data = market_response.data
                if isinstance(data, dict) and "data" in data:
                    markets = data["data"]
                    if markets:
                        stats = markets[0].get("marketStats", {})
                        mark_price = float(stats.get("markPrice", 0))

            # Try orderbook for bid/ask
            ob_response = await self.api_client.get_orderbook(market)
            if ob_response.success and ob_response.data:
                data = ob_response.data.get("data", ob_response.data)
                bids = data.get("bids", [])
                asks = data.get("asks", [])

                if bids and asks:
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    mid_price = (best_bid + best_ask) / 2
                    if mark_price <= 0:
                        mark_price = mid_price
                    return mid_price, best_bid, best_ask, mark_price

            # Fallback to mark_price for everything
            if mark_price > 0:
                return mark_price, mark_price, mark_price, mark_price

            return 0.0, 0.0, 0.0, 0.0

        except Exception as e:
            logger.error(f"Error getting price: {e}")
            return 0.0, 0.0, 0.0, 0.0

    async def _sync_position(self, market: str, mark_price_from_stats: float = 0) -> tuple[float, float, float, float, float]:
        """
        Sync position from exchange.

        Args:
            market: Market symbol
            mark_price_from_stats: Mark price from marketStats (more reliable than position response)

        Returns:
            Tuple of (position_size, mark_price, unrealized_pnl, entry_price, margin)
        """
        try:
            pos_response = await self.api_client.get_positions()
            if pos_response.success and pos_response.data:
                raw_data = pos_response.data
                positions = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data

                for p in positions:
                    if isinstance(p, dict) and p.get("market") == market:
                        size = float(p.get("size", 0))
                        side = p.get("side", "LONG")

                        if side == "SHORT":
                            size = -abs(size)

                        entry_price = float(p.get("openPrice", p.get("entryPrice", p.get("avgEntryPrice", 0))))

                        # Calculate margin correctly: value / leverage
                        # Note: position.margin field is NOT the same as UI margin
                        # UI shows: value / leverage (e.g., 32.14 / 10 = 3.214)
                        value = float(p.get("value", 0))
                        leverage = float(p.get("leverage", 10))
                        margin = value / leverage if leverage > 0 else 0

                        # Try to get mark_price from position, fallback to marketStats
                        mark_price = float(p.get("markPrice", 0))
                        if mark_price <= 0:
                            mark_price = mark_price_from_stats

                        # Use midPriceUnrealisedPnl (matches Extended Exchange UI)
                        # UI shows PnL based on mid price, not mark price
                        unrealized_pnl = float(p.get("midPriceUnrealisedPnl", p.get("unrealisedPnl", 0)))
                        if unrealized_pnl == 0 and mark_price > 0 and entry_price > 0 and size != 0:
                            # Calculate PnL: (mark_price - entry_price) * size for LONG
                            if size > 0:  # LONG
                                unrealized_pnl = (mark_price - entry_price) * abs(size)
                            else:  # SHORT
                                unrealized_pnl = (entry_price - mark_price) * abs(size)

                        # Update risk manager
                        position = Position(
                            market=market,
                            size=size,
                            entry_price=entry_price,
                            mark_price=mark_price,
                            unrealized_pnl=unrealized_pnl
                        )
                        self.risk_manager.update_position(position)

                        return size, mark_price, unrealized_pnl, entry_price, margin

                # No position found
                self.risk_manager.update_position(Position(market=market, size=0))
                return 0.0, 0.0, 0.0, 0.0, 0.0

        except Exception as e:
            logger.error(f"Error syncing position: {e}")
            return 0.0, 0.0, 0.0, 0.0, 0.0

    def _update_metrics(self):
        """Update metrics from strategy status."""
        status = self.dca_strategy.get_status()

        self.metrics.update_state(
            "active" if status["has_active_trade"] else "idle"
        )

        self.metrics.update_pnl(
            realized=status["total_pnl"],
            unrealized=0,  # Would need to calculate from position
            fees=0
        )

    async def _fetch_candles(self, market: str) -> list:
        """
        Fetch historical candles for indicator calculation.

        Extended Exchange API: /api/v1/info/candles/{market}/trades
        Returns candles with: timestamp, open, high, low, close, volume
        """
        try:
            # Calculate time range
            end_time = int(time.time() * 1000)  # Current time in ms
            # Need enough history for EMA200 on chosen timeframe
            start_time = end_time - (self._candles_count * self._candle_timeframe * 1000)

            # Fetch candles from API
            response = await self.api_client.get_candles(
                market=market,
                timeframe=self._candle_timeframe,
                start_time=start_time,
                end_time=end_time,
                limit=self._candles_count
            )

            if response.success and response.data:
                data = response.data
                candles = data.get("data", data) if isinstance(data, dict) else data

                if isinstance(candles, list) and len(candles) > 0:
                    # Extract closing prices (oldest to newest)
                    # Candle format: [timestamp, open, high, low, close, volume]
                    prices = []
                    for candle in candles:
                        if isinstance(candle, list) and len(candle) >= 5:
                            prices.append(float(candle[4]))  # Close price
                        elif isinstance(candle, dict):
                            prices.append(float(candle.get("close", candle.get("c", 0))))

                    if prices:
                        logger.debug(f"[INDICATORS] Fetched {len(prices)} candles, latest close: ${prices[-1]:.2f}")
                        return prices

            logger.warning(f"[INDICATORS] Could not fetch candles: {response.error if not response.success else 'empty data'}")
            return []

        except Exception as e:
            logger.error(f"[INDICATORS] Error fetching candles: {e}")
            return []

    async def _check_indicator_signal(self, market: str) -> tuple:
        """
        Check indicator signals for entry.

        Returns:
            Tuple of (should_enter, reason, signal_details)
        """
        if not self._indicators_enabled:
            return True, "Indicators disabled", None

        # Check if we need to update indicators
        now = time.time()
        if now - self._last_indicator_check < self._indicator_update_interval:
            # Use cached signal
            if self._last_signal:
                return self._last_signal
            return False, "Waiting for first indicator check", None

        self._last_indicator_check = now

        # Fetch candles
        prices = await self._fetch_candles(market)
        if len(prices) < 200:
            logger.warning(f"[INDICATORS] Not enough candles: {len(prices)} < 200 needed for EMA200")
            return False, f"Not enough data ({len(prices)} candles)", None

        self._cached_prices = prices

        # Get indicator settings from config
        rsi_config = self.indicators_config.get("rsi", {})
        macd_config = self.indicators_config.get("macd", {})
        bb_config = self.indicators_config.get("bollinger", {})
        ema_config = self.indicators_config.get("ema", {})

        # Initialize indicators with config
        self.indicators = TechnicalIndicators(
            rsi_period=rsi_config.get("period", 14),
            rsi_oversold=rsi_config.get("oversold", 35.0),
            rsi_overbought=rsi_config.get("overbought", 65.0),
            macd_fast=macd_config.get("fast_period", 12),
            macd_slow=macd_config.get("slow_period", 26),
            macd_signal=macd_config.get("signal_period", 9),
            bb_period=bb_config.get("period", 20),
            bb_std_dev=bb_config.get("std_dev", 2.0),
            ema_period=ema_config.get("period", 200),
        )

        # Get combined signal
        direction = self.dca_config.direction.value
        use_rsi = rsi_config.get("enabled", True)
        use_macd = macd_config.get("enabled", True)
        use_bb = bb_config.get("enabled", True)
        use_ema = ema_config.get("enabled", True)

        signal = self.indicators.get_combined_signal(
            prices=prices,
            use_rsi=use_rsi,
            use_macd=use_macd,
            use_bollinger=use_bb,
            use_ema=use_ema,
            min_confirmations=self._min_confirmations,
        )

        # Check if signal matches our direction
        target_signal = Signal.LONG if direction == "LONG" else Signal.SHORT

        # Log indicator values
        indicator_summary = []
        for ind in signal.indicators:
            indicator_summary.append(f"{ind.name}={ind.signal.value}({ind.value:.2f})")

        logger.info(
            f"[INDICATORS] {' | '.join(indicator_summary)} | "
            f"LONG:{signal.long_count} SHORT:{signal.short_count} NEUTRAL:{signal.neutral_count} | "
            f"Final: {signal.final_signal.value} (confidence: {signal.confidence:.0%})"
        )

        if signal.final_signal == target_signal:
            reason = f"{signal.long_count if direction == 'LONG' else signal.short_count}/{len(signal.indicators)} indicators confirm {direction}"
            self._last_signal = (True, reason, signal)
            return True, reason, signal
        else:
            reason = f"Signal is {signal.final_signal.value}, need {direction} ({signal.long_count}L/{signal.short_count}S)"
            self._last_signal = (False, reason, signal)
            return False, reason, signal


def load_dca_config(profile_name: str) -> tuple:
    """
    Load DCA configuration from profile.

    Returns:
        Tuple of (DCAConfig, indicators_config_dict)
    """
    profile = load_profile(profile_name)
    dca_dict = profile.get("dca", {})
    indicators_dict = profile.get("indicators", {})

    # Parse direction
    direction_str = dca_dict.get("direction", "LONG").upper()
    direction = DCADirection.LONG if direction_str == "LONG" else DCADirection.SHORT

    dca_config = DCAConfig(
        direction=direction,
        market=dca_dict.get("market", "ETH-USD"),
        base_order_size_usd=float(dca_dict.get("base_order_size_usd", 20.0)),
        max_safety_orders=int(dca_dict.get("max_safety_orders", 4)),
        safety_order_size_usd=float(dca_dict.get("safety_order_size_usd", 25.0)),
        price_deviation_pct=float(dca_dict.get("price_deviation_pct", 2.5)),
        safety_order_step_scale=float(dca_dict.get("safety_order_step_scale", 1.5)),
        safety_order_volume_scale=float(dca_dict.get("safety_order_volume_scale", 1.2)),
        take_profit_pct=float(dca_dict.get("take_profit_pct", 1.5)),
        trailing_take_profit=bool(dca_dict.get("trailing_take_profit", False)),
        trailing_deviation_pct=float(dca_dict.get("trailing_deviation_pct", 0.5)),
        stop_loss_pct=float(dca_dict.get("stop_loss_pct", 0.0)),
        start_immediately=bool(dca_dict.get("start_immediately", True)),
        cooldown_between_trades_sec=float(dca_dict.get("cooldown_between_trades_sec", 60.0)),
        entry_refresh_seconds=float(dca_dict.get("entry_refresh_seconds", 60.0)),
    )

    return dca_config, indicators_dict


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Extended Exchange DCA Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run on testnet
  python -m src.dca_main --env testnet --profile DCA_ETH --dry-run

  # Live trading on mainnet
  python -m src.dca_main --env mainnet --profile DCA_ETH

  # Override direction to SHORT
  python -m src.dca_main --env mainnet --profile DCA_ETH --direction SHORT
        """
    )

    parser.add_argument(
        "--env",
        choices=["testnet", "mainnet"],
        default="testnet",
        help="Trading environment (default: testnet)"
    )

    parser.add_argument(
        "--profile",
        type=str,
        required=True,
        help="DCA configuration profile name (e.g., DCA_ETH)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulation mode - no real orders placed"
    )

    parser.add_argument(
        "--direction",
        choices=["LONG", "SHORT"],
        help="Override DCA direction"
    )

    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)"
    )

    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()

    # Setup logging
    setup_logging(log_level=args.log_level, json_format=False)

    # Load DCA config from profile
    try:
        dca_config, indicators_config = load_dca_config(args.profile)
    except Exception as e:
        logger.error(f"Failed to load DCA config: {e}")
        sys.exit(1)

    # Override direction if specified
    if args.direction:
        dca_config.direction = DCADirection.LONG if args.direction == "LONG" else DCADirection.SHORT

    # Build overrides for bot config
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True

    # The DCA config market should be used in strategy config
    overrides["strategy"] = {"market": dca_config.market}

    # Create bot configuration
    try:
        config = create_config(
            env=args.env,
            profile=args.profile,
            overrides=overrides
        )
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    config.log_level = args.log_level

    # Create and run bot with indicators config
    bot = DCABot(config, dca_config, indicators_config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Bot error: {e}", exc_info=True)
    finally:
        if bot._running:
            await bot.stop("error")


if __name__ == "__main__":
    asyncio.run(main())
