"""
Market Making Strategy module for Extended MM Bot.
Implements conservative market making with dynamic spread and inventory control.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional
import logging

from .config import BotConfig, StrategyConfig
from .ws_client import OrderBook, MarketState
from .risk import RiskManager, BotState
from .order_manager import OrderManager, QuotePair
from .metrics import MetricsCollector

logger = logging.getLogger(__name__)


@dataclass
class QuoteParams:
    """Calculated quote parameters."""
    bid_price: float
    bid_size: float
    ask_price: float
    ask_size: float
    spread_bps: float
    mid_price: float
    skew_applied: float


class MarketMakingStrategy:
    """
    Conservative market making strategy.
    
    Features:
    - Dynamic spread based on volatility
    - Inventory skew to maintain neutral position
    - Pause on volatility spikes
    - Configurable refresh intervals
    - Anti-self-trade protection
    """
    
    def __init__(
        self,
        config: BotConfig,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        metrics: MetricsCollector
    ):
        self.config = config
        self.strategy_config: StrategyConfig = config.strategy
        self.order_manager = order_manager
        self.risk_manager = risk_manager
        self.metrics = metrics
        
        # State
        self._last_quote_time = 0.0
        self._last_mid_price = 0.0
        self._quote_uptime_start = 0.0
        self._total_quote_time = 0.0
        
        # Market info (to be fetched)
        self._tick_size: float = 0.0001
        self._min_order_size: float = 0.001
        self._price_precision: int = 4
        self._size_precision: int = 6
    
    async def initialize(self, market_info: dict):
        """Initialize strategy with market information."""
        # Parse market info from Extended API
        # tradingConfig contains minOrderSize, minOrderSizeChange, minPriceChange
        trading_config = market_info.get("tradingConfig", {})

        # Get min order size from tradingConfig (e.g., "0.1" for AAVE)
        self._min_order_size = float(trading_config.get("minOrderSize", "0.1"))

        # Get tick sizes from tradingConfig
        min_size_change = float(trading_config.get("minOrderSizeChange", "0.01"))
        min_price_change = float(trading_config.get("minPriceChange", "0.01"))

        self._tick_size = min_price_change
        self._price_precision = self._get_precision(min_price_change)
        self._size_precision = self._get_precision(min_size_change)

        logger.info(
            f"Strategy initialized: tick_size={self._tick_size}, "
            f"min_size={self._min_order_size}, "
            f"price_precision={self._price_precision}, size_precision={self._size_precision}"
        )
    
    def calculate_quotes(
        self,
        orderbook: OrderBook,
        volatility_bps: float
    ) -> Optional[QuoteParams]:
        """
        Calculate bid and ask quote parameters.
        
        Args:
            orderbook: Current order book
            volatility_bps: Recent volatility in basis points
        
        Returns:
            QuoteParams or None if unable to quote
        """
        mid_price = orderbook.mid_price
        
        if mid_price is None or mid_price <= 0:
            logger.warning("Invalid mid price, cannot quote")
            return None
        
        # Calculate dynamic spread
        spread_bps = self._calculate_spread(volatility_bps)
        
        # Get inventory skew
        skew = self.risk_manager.calculate_inventory_skew()
        
        # Calculate half spread
        half_spread = (spread_bps / 10000) * mid_price / 2
        
        # Apply skew to shift quotes
        # Positive skew = long position = shift quotes down (more aggressive ask)
        skew_offset = skew * half_spread * 0.5
        
        # Calculate prices
        bid_price = mid_price - half_spread - skew_offset
        ask_price = mid_price + half_spread - skew_offset
        
        # Round to tick size
        bid_price = self._round_to_tick(bid_price, down=True)
        ask_price = self._round_to_tick(ask_price, down=False)

        # Ensure spread is positive after rounding
        if bid_price >= ask_price:
            ask_price = bid_price + self._tick_size

        # POST_ONLY protection: ensure our prices don't cross the book
        # BID must be < best_ask, ASK must be > best_bid
        best_bid = orderbook.best_bid
        best_ask = orderbook.best_ask

        if best_ask and bid_price >= best_ask:
            # Our bid would take liquidity, adjust down
            bid_price = self._round_to_tick(best_ask - self._tick_size, down=True)
            logger.debug(f"Adjusted BID down to {bid_price} (was crossing best_ask {best_ask})")

        if best_bid and ask_price <= best_bid:
            # Our ask would take liquidity, adjust up
            ask_price = self._round_to_tick(best_bid + self._tick_size, down=False)
            logger.debug(f"Adjusted ASK up to {ask_price} (was crossing best_bid {best_bid})")
        
        # Calculate sizes
        notional = self.strategy_config.order_notional_usd
        bid_size = self._calculate_size(notional, bid_price)
        ask_size = self._calculate_size(notional, ask_price)
        
        # Validate sizes
        if bid_size < self._min_order_size or ask_size < self._min_order_size:
            logger.warning(
                f"Order size too small: bid={bid_size:.6f}, ask={ask_size:.6f}, "
                f"min={self._min_order_size:.6f}"
            )
            return None
        
        return QuoteParams(
            bid_price=bid_price,
            bid_size=bid_size,
            ask_price=ask_price,
            ask_size=ask_size,
            spread_bps=spread_bps,
            mid_price=mid_price,
            skew_applied=skew
        )
    
    def _calculate_spread(self, volatility_bps: float) -> float:
        """
        Calculate dynamic spread based on volatility.
        
        Spread = base_spread + volatility_component
        """
        base_spread = self.strategy_config.spread_min_bps
        
        # Add volatility component
        vol_component = volatility_bps * self.strategy_config.volatility_spread_multiplier
        
        # Total spread, clamped to max
        spread = base_spread + vol_component
        spread = min(spread, self.strategy_config.spread_max_bps)
        
        return spread
    
    def _calculate_size(self, notional_usd: float, price: float) -> float:
        """Calculate order size from notional and price."""
        if price <= 0:
            return 0.0
        
        size = notional_usd / price
        
        # Round to size precision
        size = round(size, self._size_precision)
        
        return size
    
    def _round_to_tick(self, price: float, down: bool = True) -> float:
        """Round price to tick size."""
        if self._tick_size <= 0:
            return round(price, self._price_precision)
        
        ticks = price / self._tick_size
        if down:
            ticks = int(ticks)
        else:
            ticks = int(ticks) + 1 if ticks != int(ticks) else int(ticks)
        
        return round(ticks * self._tick_size, self._price_precision)
    
    def _get_precision(self, value: float) -> int:
        """Get decimal precision from a value like 0.0001."""
        if value >= 1:
            return 0
        s = f"{value:.10f}".rstrip('0')
        if '.' in s:
            return len(s.split('.')[1])
        return 0
    
    async def update_quotes(
        self,
        market_state: MarketState,
        force: bool = False
    ) -> bool:
        """
        Update quotes based on current market state.
        
        Args:
            market_state: Current market state from WebSocket
            force: Force requote even if conditions not met
        
        Returns:
            True if quotes were updated
        """
        orderbook = market_state.orderbook
        mid_price = orderbook.mid_price
        
        if mid_price is None:
            return False
        
        # Update risk manager with current mid
        self.risk_manager.update_mid_price(mid_price)
        
        # Check if we should requote
        should_requote = force
        requote_reason = "forced" if force else ""
        
        # Check refresh interval
        elapsed = time.time() - self._last_quote_time
        if elapsed >= self.strategy_config.refresh_sec:
            should_requote = True
            requote_reason = "refresh"
        
        # Check mid price movement
        if self._last_mid_price > 0:
            move_bps = abs(mid_price - self._last_mid_price) / self._last_mid_price * 10000
            if move_bps >= self.strategy_config.mid_move_requote_bps:
                should_requote = True
                requote_reason = "mid_move"
        
        if not should_requote:
            return False
        
        # Check pause conditions
        should_pause, pause_reason = self.risk_manager.check_pause_conditions(mid_price)
        if should_pause:
            if self.risk_manager.state.state != BotState.PAUSED:
                self.risk_manager.enter_pause(pause_reason)
                await self.order_manager.cancel_all_quotes()
                self.metrics.record_pause()
            return False
        
        # Check kill switch
        should_kill, kill_reason = self.risk_manager.check_kill_switch()
        if should_kill:
            self.risk_manager.activate_kill_switch(kill_reason)
            await self.order_manager.cancel_all_quotes()
            return False
        
        # Calculate volatility
        volatility_bps = market_state.get_volatility(
            self.strategy_config.volatility_lookback_sec
        )
        
        # Calculate quote parameters
        quote_params = self.calculate_quotes(orderbook, volatility_bps)
        
        if quote_params is None:
            logger.warning("Cannot calculate quotes")
            return False
        
        # Check if we can place orders
        can_bid, bid_reason = self.risk_manager.can_place_order("BUY", quote_params.bid_price * quote_params.bid_size)
        can_ask, ask_reason = self.risk_manager.can_place_order("SELL", quote_params.ask_price * quote_params.ask_size)

        if not can_bid:
            logger.warning(f"BID blocked: {bid_reason}")
        if not can_ask:
            logger.warning(f"ASK blocked: {ask_reason}")

        # Place quotes
        start_time = time.time()

        quote_pair = await self.order_manager.place_quotes(
            bid_price=quote_params.bid_price if can_bid else 0,
            bid_size=quote_params.bid_size if can_bid else 0,
            ask_price=quote_params.ask_price if can_ask else 0,
            ask_size=quote_params.ask_size if can_ask else 0,
            cancel_existing=True
        )
        
        latency_ms = (time.time() - start_time) * 1000
        
        # Update state
        self._last_quote_time = time.time()
        self._last_mid_price = mid_price
        
        # Update metrics
        if quote_pair.has_bid or quote_pair.has_ask:
            self.metrics.record_quote_placed(latency_ms)
            self.metrics.record_requote(requote_reason)
            self._update_quote_uptime(True)
        
        # Update market metrics
        self.metrics.update_market(
            mid_price=mid_price,
            spread_bps=quote_params.spread_bps,
            volatility_bps=volatility_bps,
            bid_liquidity=orderbook.get_bid_liquidity(),
            ask_liquidity=orderbook.get_ask_liquidity(),
            trade_imbalance=market_state.get_recent_trade_direction()
        )
        
        logger.info(
            f"Quotes updated: BID {quote_params.bid_size:.6f} @ {quote_params.bid_price:.4f} | "
            f"ASK {quote_params.ask_size:.6f} @ {quote_params.ask_price:.4f} | "
            f"spread={quote_params.spread_bps:.1f}bps, skew={quote_params.skew_applied:+.2f}, "
            f"reason={requote_reason}"
        )
        
        return True
    
    def _update_quote_uptime(self, has_quotes: bool):
        """Track quote uptime percentage."""
        now = time.time()
        
        if has_quotes:
            if self._quote_uptime_start == 0:
                self._quote_uptime_start = now
        else:
            if self._quote_uptime_start > 0:
                self._total_quote_time += now - self._quote_uptime_start
                self._quote_uptime_start = 0
        
        # Calculate uptime percentage
        total_time = now - self.metrics.start_time
        if total_time > 0:
            current_quote_time = self._total_quote_time
            if self._quote_uptime_start > 0:
                current_quote_time += now - self._quote_uptime_start
            
            uptime_pct = (current_quote_time / total_time) * 100
            self.metrics.update_uptime(uptime_pct)
    
    async def handle_fill(self, fill_data: dict):
        """Handle a fill notification."""
        # Update order manager
        order_id = fill_data.get("orderId", fill_data.get("order_id", ""))
        filled_size = float(fill_data.get("filledSize", fill_data.get("filled_size", 0)))
        remaining = float(fill_data.get("remainingSize", fill_data.get("remaining_size", 0)))
        
        self.order_manager.update_order_from_fill(order_id, filled_size, remaining)
        
        # Record fill in risk manager
        from .risk import Fill
        fill = Fill(
            market=fill_data.get("market", self.config.strategy.market),
            side=fill_data.get("side", "").upper(),
            price=float(fill_data.get("price", 0)),
            size=float(fill_data.get("size", fill_data.get("amount", 0))),
            fee=float(fill_data.get("fee", 0)),
            timestamp=fill_data.get("timestamp", time.time()),
            order_id=order_id,
            is_maker=fill_data.get("isMaker", fill_data.get("is_maker", True))
        )
        
        self.risk_manager.record_fill(fill)
        self.metrics.record_fill(fill.is_maker)
    
    async def emergency_flatten(self, current_mid: float) -> bool:
        """Emergency flatten all positions."""
        logger.warning("EMERGENCY FLATTEN initiated")
        
        # Cancel all orders first
        await self.order_manager.cancel_all_quotes()
        
        # Flatten position
        position_size = self.risk_manager.state.position.size
        if abs(position_size) > self._min_order_size:
            return await self.order_manager.flatten_position(current_mid, position_size)
        
        return True
    
    def get_strategy_state(self) -> dict:
        """Get current strategy state."""
        current_quotes = self.order_manager.get_current_quotes()
        
        return {
            "last_quote_time": self._last_quote_time,
            "last_mid_price": self._last_mid_price,
            "has_bid": current_quotes.has_bid,
            "has_ask": current_quotes.has_ask,
            "bid_price": current_quotes.bid.price if current_quotes.bid else 0,
            "ask_price": current_quotes.ask.price if current_quotes.ask else 0,
            "tick_size": self._tick_size,
            "min_order_size": self._min_order_size,
        }


class StrategyRunner:
    """
    Runs the market making strategy in a loop.
    """
    
    def __init__(
        self,
        strategy: MarketMakingStrategy,
        config: BotConfig
    ):
        self.strategy = strategy
        self.config = config
        self._running = False
        self._task: Optional[asyncio.Task] = None
    
    async def start(self, get_market_state):
        """Start the strategy loop."""
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(get_market_state)
        )
        logger.info("Strategy runner started")
    
    async def stop(self):
        """Stop the strategy loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Strategy runner stopped")
    
    async def _run_loop(self, get_market_state):
        """Main strategy loop."""
        while self._running:
            try:
                market_state = get_market_state()

                # Run if we have market state with valid mid price
                # (either from WS or from REST fallback)
                if market_state and market_state.orderbook.mid_price:
                    await self.strategy.update_quotes(market_state)
                elif market_state:
                    logger.debug("No mid price available, waiting for market data...")

                # Sleep for a fraction of refresh interval
                await asyncio.sleep(0.5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Strategy loop error: {e}", exc_info=True)
                await asyncio.sleep(1)
