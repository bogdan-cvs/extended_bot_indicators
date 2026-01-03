#!/usr/bin/env python3
"""
Extended Exchange Market Making Bot - Main Entry Point

A conservative market making bot for Extended Exchange.
Designed for risk-controlled liquidity provision with inventory management.

Usage:
    python -m src.main --env testnet --profile TESTNET_SAFE
    python -m src.main --env mainnet --profile MAINNET_CONSERVATIVE_200USD
    python -m src.main --env testnet --dry-run  # Simulation mode
"""

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path
import logging

from .config import (
    BotConfig,
    create_config,
    validate_config,
    print_config_summary,
    Environment
)
from .api_client import ExtendedAPIClient, test_api_connection
from .ws_client import ExtendedWSClient, OrderBook, Trade, test_ws_connection
from .signing import StarkSigner, update_market_l2_config
from .order_manager import OrderManager
from .risk import RiskManager, BotState, Position
from .strategy_mm import MarketMakingStrategy, StrategyRunner
from .metrics import MetricsCollector, setup_logging

logger = logging.getLogger(__name__)


class MarketMakingBot:
    """
    Main bot orchestrator.
    
    Coordinates all components: API client, WebSocket, strategy,
    risk management, and metrics collection.
    """
    
    def __init__(self, config: BotConfig):
        self.config = config
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        # Components (initialized in start())
        self.api_client: ExtendedAPIClient = None
        self.ws_client: ExtendedWSClient = None
        self.signer: StarkSigner = None
        self.order_manager: OrderManager = None
        self.risk_manager: RiskManager = None
        self.strategy: MarketMakingStrategy = None
        self.strategy_runner: StrategyRunner = None
        self.metrics: MetricsCollector = None
    
    async def start(self):
        """Initialize and start the bot."""
        logger.info("="*60)
        logger.info("EXTENDED MM BOT - STARTING")
        logger.info("="*60)
        
        # Print configuration
        print_config_summary(self.config)
        
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
        
        # Start components
        self._running = True
        await self._start_components()
        
        logger.info("Bot started successfully")
        
        # Main loop
        await self._main_loop()
    
    async def stop(self, reason: str = "shutdown"):
        """Stop the bot gracefully."""
        logger.info(f"Stopping bot: {reason}")
        self._running = False
        
        # Cancel all orders
        if self.order_manager:
            logger.info("Cancelling all orders...")
            await self.order_manager.cancel_all_quotes()
        
        # Stop strategy
        if self.strategy_runner:
            await self.strategy_runner.stop()
        
        # Stop WebSocket
        if self.ws_client:
            await self.ws_client.stop()
        
        # Close API client
        if self.api_client:
            await self.api_client.close()
        
        # Final metrics report
        if self.metrics:
            self.metrics.report()
        
        logger.info("Bot stopped")
        self._shutdown_event.set()
    
    async def _initialize_components(self):
        """Initialize all bot components."""
        logger.info("Initializing components...")
        
        # Metrics
        self.metrics = MetricsCollector(self.config)
        
        # API Client
        self.api_client = ExtendedAPIClient(self.config)
        await self.api_client.start()
        
        # Order Manager (pass private key directly)
        self.order_manager = OrderManager(
            config=self.config,
            api_client=self.api_client,
            private_key=self.config.stark_private_key
        )
        await self.order_manager.initialize()
        
        # Risk Manager
        self.risk_manager = RiskManager(self.config)
        
        # WebSocket Client
        self.ws_client = ExtendedWSClient(
            config=self.config,
            on_orderbook=self._on_orderbook,
            on_trade=self._on_trade,
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect
        )
        
        # Strategy
        self.strategy = MarketMakingStrategy(
            config=self.config,
            order_manager=self.order_manager,
            risk_manager=self.risk_manager,
            metrics=self.metrics
        )
        
        self.strategy_runner = StrategyRunner(self.strategy, self.config)
        
        logger.info("Components initialized")
    
    async def _preflight_checks(self) -> bool:
        """Run pre-flight checks before starting."""
        logger.info("Running pre-flight checks...")
        
        market = self.config.strategy.market
        
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
            await self.strategy.initialize(market_info)
            logger.info(f"[OK] Market info: {market_info}")

            # Update L2 config for signing if available
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
            
            # Initialize risk manager with balance
            # Try to extract USD balance
            if isinstance(balance_data, dict):
                usd_balance = float(balance_data.get("USD", balance_data.get("USDC", balance_data.get("total", 0))))
            else:
                usd_balance = 100  # Default for dry run
            
            self.risk_manager.initialize(usd_balance)
        else:
            logger.warning(f"Could not fetch balance: {balance_response.error}")
            self.risk_manager.initialize(100)  # Default
        
        # 4. Check existing orders
        logger.info("Checking existing orders...")
        orders_response = await self.api_client.get_open_orders(market)
        if orders_response.success:
            existing_orders = orders_response.data or []
            if existing_orders:
                logger.warning(f"Found {len(existing_orders)} existing orders. Cancelling...")
                await self.order_manager.cancel_all_quotes(market)
        
        # 5. Test WebSocket (brief)
        logger.info("Testing WebSocket connectivity...")
        if not self.config.dry_run:
            ws_ok = await test_ws_connection(self.config, market, timeout=5.0)
            if not ws_ok:
                logger.warning("WebSocket test did not receive data (may be normal if market is quiet)")
        
        logger.info("[OK] Pre-flight checks completed")
        return True
    
    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}")
            asyncio.create_task(self.stop("signal"))
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def _start_components(self):
        """Start all runtime components."""
        market = self.config.strategy.market
        self._use_rest_fallback = False

        # Start WebSocket
        await self.ws_client.start()
        await self.ws_client.subscribe_orderbook(market)
        await self.ws_client.subscribe_trades(market)

        # Wait for initial data
        logger.info("Waiting for initial market data...")
        ws_connected = False
        for _ in range(10):
            state = self.ws_client.get_market_state(market)
            if state and state.orderbook.mid_price:
                ws_connected = True
                break
            await asyncio.sleep(0.5)

        if not ws_connected:
            logger.warning("WebSocket not available, using REST API fallback for market data")
            self._use_rest_fallback = True
            # Initialize REST fallback market state
            await self._update_market_state_from_rest()

        # Start strategy with appropriate market state getter
        await self.strategy_runner.start(
            lambda: self._get_market_state_with_fallback(market)
        )

        self.metrics.update_state("running")

    def _get_market_state_with_fallback(self, market: str):
        """Get market state from WS or REST fallback."""
        if self._use_rest_fallback:
            return self._rest_market_state
        return self.ws_client.get_market_state(market)

    async def _update_market_state_from_rest(self):
        """Update market state from REST API."""
        from .ws_client import MarketState, OrderBook, OrderBookLevel
        market = self.config.strategy.market

        # Get orderbook
        ob_response = await self.api_client.get_orderbook(market)
        bids = []
        asks = []
        if ob_response.success and ob_response.data:
            data = ob_response.data.get("data", {})
            for b in data.get("bids", []):
                bids.append(OrderBookLevel(price=float(b[0]), size=float(b[1])))
            for a in data.get("asks", []):
                asks.append(OrderBookLevel(price=float(a[0]), size=float(a[1])))

        # If orderbook empty, get mark price from market info
        mid_price = None
        if not bids or not asks:
            market_response = await self.api_client.get_market(market)
            if market_response.success and market_response.data:
                data = market_response.data.get("data", [])
                if data:
                    stats = data[0].get("marketStats", {})
                    mark_price = float(stats.get("markPrice", 0))
                    if mark_price > 0:
                        # Create synthetic orderbook around mark price
                        spread_bps = self.config.strategy.spread_min_bps
                        half_spread = (spread_bps / 10000) * mark_price / 2
                        bid_price = mark_price - half_spread
                        ask_price = mark_price + half_spread
                        bids = [OrderBookLevel(price=bid_price, size=1.0)]
                        asks = [OrderBookLevel(price=ask_price, size=1.0)]
                        logger.info(f"Using mark price {mark_price:.2f} for synthetic orderbook")

        orderbook = OrderBook(
            market=market,
            bids=bids,
            asks=asks
        )

        if not hasattr(self, "_rest_market_state"):
            self._rest_market_state = MarketState(market=market)

        self._rest_market_state.orderbook = orderbook
        self._rest_market_state.ws_connected = True  # Fake connected for strategy to work
    
    async def _main_loop(self):
        """Main bot loop for monitoring and metrics."""
        market = self.config.strategy.market
        
        while self._running:
            try:
                # Update market state from REST if using fallback
                if self._use_rest_fallback:
                    await self._update_market_state_from_rest()

                # Update risk state from exchange
                await self._sync_state()

                # Check stop-loss
                should_stop_loss, stop_loss_params = self.risk_manager.check_stop_loss()
                if should_stop_loss:
                    await self._execute_stop_loss(stop_loss_params)

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
                
                await asyncio.sleep(5)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                self.risk_manager.record_error(str(e))
                await asyncio.sleep(1)
        
        # Wait for shutdown to complete
        await self._shutdown_event.wait()
    
    async def _sync_state(self):
        """Sync state from exchange."""
        market = self.config.strategy.market

        # Sync positions - use get_positions (all) instead of get_position (single)
        # because get_position returns 404 when no position exists
        pos_response = await self.api_client.get_positions()
        if pos_response.success and pos_response.data:
            # Response is {'status': 'OK', 'data': [...]}
            raw_data = pos_response.data
            positions = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data
            # Find position for our market
            pos_data = None
            for p in positions:
                if isinstance(p, dict) and p.get("market") == market:
                    pos_data = p
                    break

            if pos_data:
                position = Position(
                    market=market,
                    size=float(pos_data.get("size", 0)),
                    entry_price=float(pos_data.get("entryPrice", pos_data.get("entry_price", 0))),
                    mark_price=float(pos_data.get("markPrice", pos_data.get("mark_price", 0))),
                    unrealized_pnl=float(pos_data.get("unrealizedPnl", pos_data.get("unrealized_pnl", 0)))
                )
                # Handle side - SHORT position has negative size
                side = pos_data.get("side", "LONG")
                if side == "SHORT":
                    position.size = -abs(position.size)
                position.notional_usd = abs(position.size * position.mark_price)
                self.risk_manager.update_position(position)
                logger.debug(f"Position synced: {side} {abs(position.size)} @ {position.entry_price}")
            else:
                # No position - reset to zero
                self.risk_manager.update_position(Position(market=market, size=0, entry_price=0, mark_price=0, unrealized_pnl=0))
        
        # Sync orders
        await self.order_manager.sync_open_orders()

    async def _execute_stop_loss(self, params: dict):
        """
        Execute stop-loss by placing an aggressive IOC order to close position.

        Args:
            params: {"side": str, "size": float, "market": str, "loss_pct": float}
        """
        from decimal import Decimal

        side = params["side"]
        size = Decimal(str(params["size"]))
        market = params["market"]
        loss_pct = params.get("loss_pct", 0)

        logger.warning(f"EXECUTING STOP-LOSS: {side} {size} {market} (loss: {loss_pct:.2f}%)")

        # Cancel all open orders first
        await self.order_manager.cancel_all_orders()

        # Get current mark price for aggressive pricing
        position = self.risk_manager.state.position
        mark_price = position.mark_price

        if mark_price <= 0:
            # Fallback to API
            market_response = await self.api_client.get_market(market)
            if market_response.success and market_response.data:
                data = market_response.data
                if isinstance(data, dict) and "data" in data:
                    markets = data["data"]
                    if markets:
                        stats = markets[0].get("marketStats", {})
                        mark_price = float(stats.get("markPrice", 0))

        if mark_price <= 0:
            logger.error("Cannot execute stop-loss: no mark price available")
            return

        # Set aggressive price (5% worse than mark to ensure fill)
        if side == "BUY":
            aggressive_price = Decimal(str(mark_price)) * Decimal("1.05")
        else:
            aggressive_price = Decimal(str(mark_price)) * Decimal("0.95")

        # Round to 2 decimals
        aggressive_price = Decimal(str(round(float(aggressive_price), 2)))

        logger.info(f"Stop-loss order: {side} {size} @ {aggressive_price} (mark: {mark_price})")

        # Build IOC order to close position
        order_payload = self.order_manager.order_builder.build_ioc_order(
            market=market,
            side=side,
            size=size,
            price=aggressive_price,
            reduce_only=True,
        )

        # Place order
        response = await self.api_client.create_order(order_payload)

        if response.success:
            logger.warning(f"Stop-loss order placed successfully: {response.data}")
        else:
            logger.error(f"Stop-loss order failed: {response.error}")

    def _update_metrics(self):
        """Update all metrics."""
        market = self.config.strategy.market
        
        # Update state
        self.metrics.update_state(self.risk_manager.state.state.value)
        
        # Update from risk manager
        risk_summary = self.risk_manager.get_risk_summary()
        self.metrics.update_pnl(
            realized=risk_summary["realized_pnl"],
            unrealized=risk_summary["unrealized_pnl"],
            fees=risk_summary["fees_paid"]
        )
        
        self.metrics.update_inventory(
            position_size=risk_summary["position_size"],
            position_notional=risk_summary["inventory_usd"],
            inventory_usd=risk_summary["inventory_usd"],
            max_inventory_usd=self.config.risk.max_inventory_usd,
            skew_factor=risk_summary["inventory_skew"]
        )
        
        # Update from order manager
        order_stats = self.order_manager.get_stats()
        
        # Update from WebSocket
        ws_stats = self.ws_client.get_stats()
        api_stats = self.api_client.get_stats()
        
        self.metrics.update_system(
            ws_connected=ws_stats["connected"],
            ws_messages=ws_stats["message_count"],
            api_requests=api_stats["total_requests"],
            api_errors=0,  # Track separately
            rate_limit_hits=api_stats["rate_limit_hits"]
        )
    
    def _on_orderbook(self, orderbook: OrderBook):
        """Handle orderbook update."""
        # Handled by strategy via market state
        pass
    
    def _on_trade(self, trade: Trade):
        """Handle trade update."""
        # Handled by strategy via market state
        pass
    
    def _on_ws_connect(self):
        """Handle WebSocket connection."""
        logger.info("WebSocket connected")
        market = self.config.strategy.market
        if market in self.ws_client.market_state:
            self.ws_client.market_state[market].ws_connected = True
    
    def _on_ws_disconnect(self):
        """Handle WebSocket disconnection."""
        logger.warning("WebSocket disconnected")
        market = self.config.strategy.market
        if market in self.ws_client.market_state:
            self.ws_client.market_state[market].ws_connected = False


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Extended Exchange Market Making Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry-run on testnet with safe profile
  python -m src.main --env testnet --profile TESTNET_SAFE --dry-run

  # Live trading on testnet
  python -m src.main --env testnet --profile TESTNET_SAFE

  # Live trading on mainnet (conservative)
  python -m src.main --env mainnet --profile MAINNET_CONSERVATIVE_200USD

  # Custom config file
  python -m src.main --env testnet --config my_config.yaml
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
        help="Configuration profile name (e.g., TESTNET_SAFE, MAINNET_CONSERVATIVE_200USD)"
    )
    
    parser.add_argument(
        "--config",
        type=str,
        help="Path to custom configuration file (YAML)"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulation mode - no real orders placed"
    )
    
    parser.add_argument(
        "--market",
        type=str,
        help="Override market symbol (e.g., ETH-USDC)"
    )
    
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging level (default: INFO)"
    )
    
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only run pre-flight checks, don't start trading"
    )
    
    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_args()
    
    # Setup basic logging first
    setup_logging(log_level=args.log_level, json_format=False)
    
    # Build overrides from CLI args
    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.market:
        overrides["strategy"] = {"market": args.market}
    
    # Create configuration
    try:
        config = create_config(
            env=args.env,
            profile=args.profile,
            config_path=args.config,
            overrides=overrides
        )
    except Exception as e:
        logger.error(f"Failed to load configuration: {e}")
        sys.exit(1)
    
    # Override log level if specified
    config.log_level = args.log_level
    
    # Check-only mode
    if args.check_only:
        logger.info("Running pre-flight checks only...")
        bot = MarketMakingBot(config)
        await bot._initialize_components()
        success = await bot._preflight_checks()
        await bot.api_client.close()
        sys.exit(0 if success else 1)
    
    # Create and run bot
    bot = MarketMakingBot(config)
    
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
