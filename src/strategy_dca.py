"""
DCA (Dollar Cost Averaging) Strategy for Extended Exchange.

This strategy implements a DCA bot that:
1. Places a base order to enter a position
2. Places safety orders at lower prices (for LONG) or higher prices (for SHORT)
3. Averages down the entry price when safety orders are filled
4. Takes profit when price reaches target % from average entry
5. Optional stop loss protection
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)


class DCADirection(Enum):
    """DCA trade direction."""
    LONG = "LONG"    # Buy low, sell high
    SHORT = "SHORT"  # Sell high, buy low
    AUTO = "AUTO"    # Determined by indicators


def _extract_order_id(response_data: dict) -> Optional[str]:
    """
    Extract order ID from API response.

    Extended Exchange API returns:
    {
        "status": "OK",
        "data": {
            "id": 2008627445277171712,  # <- This is the internal order ID
            "externalId": "..."          # <- This is our client ID
        }
    }

    We need the internal 'id' from the nested 'data' dict.
    """
    if not response_data:
        return None

    # Check if response has nested 'data' structure
    if "data" in response_data and isinstance(response_data["data"], dict):
        inner_data = response_data["data"]
        # Get internal ID (preferred for cancel operations)
        order_id = inner_data.get("id")
        if order_id:
            return str(order_id)

    # Fallback to top-level fields
    return response_data.get("orderId") or response_data.get("id")


class DCATradeState(Enum):
    """State of a DCA trade."""
    IDLE = "IDLE"                    # No active trade
    WAITING_BASE = "WAITING_BASE"    # Waiting for base order to fill
    ACTIVE = "ACTIVE"                # Trade active, safety orders placed
    TAKING_PROFIT = "TAKING_PROFIT"  # TP order placed
    STOPPED = "STOPPED"              # Stop loss triggered
    COMPLETED = "COMPLETED"          # Trade completed with profit


@dataclass
class DCAConfig:
    """DCA Strategy Configuration."""
    # Direction
    direction: DCADirection = DCADirection.LONG

    # Base order
    base_order_size_usd: float = 20.0  # Size of initial order in USD

    # Safety orders
    max_safety_orders: int = 4         # Maximum number of safety orders
    safety_order_size_usd: float = 25.0  # Size of first safety order
    price_deviation_pct: float = 2.5   # % drop to trigger first safety order
    safety_order_step_scale: float = 1.5  # Multiplier for distance between SOs
    safety_order_volume_scale: float = 1.2  # Multiplier for SO size

    # Take profit
    take_profit_pct: float = 1.5       # Target profit % from average price
    trailing_take_profit: bool = False  # Enable trailing TP
    trailing_deviation_pct: float = 0.5  # Trail by this % below peak

    # Stop loss
    stop_loss_pct: float = 0.0         # 0 = disabled, e.g. 15.0 = -15% loss

    # Entry conditions
    start_immediately: bool = True     # Start trade immediately or wait for signal
    cooldown_between_trades_sec: float = 60.0  # Wait time after trade completes
    entry_refresh_seconds: float = 60.0  # Re-place base order if not filled within this time (0 = disabled)

    # Market
    market: str = "ETH-USD"


@dataclass
class SafetyOrder:
    """Represents a safety order."""
    index: int                    # SO number (1, 2, 3, ...)
    price: float                  # Target price
    size: float                   # Order size in base currency
    size_usd: float              # Order size in USD
    deviation_pct: float         # Deviation from base order price
    order_id: Optional[str] = None
    filled: bool = False
    filled_at: Optional[float] = None
    filled_price: Optional[float] = None


@dataclass
class DCATrade:
    """Represents an active DCA trade."""
    id: str
    direction: DCADirection
    market: str
    started_at: float

    # Base order
    base_order_price: float = 0.0
    base_order_size: float = 0.0
    base_order_filled: bool = False
    base_order_id: Optional[str] = None

    # Safety orders
    safety_orders: List[SafetyOrder] = field(default_factory=list)
    active_safety_orders: int = 0
    filled_safety_orders: int = 0

    # Position tracking
    total_size: float = 0.0           # Total position size
    total_cost: float = 0.0           # Total USD spent/received
    average_price: float = 0.0        # Average entry price

    # Take profit
    take_profit_price: float = 0.0
    take_profit_order_id: Optional[str] = None

    # Stop loss
    stop_loss_price: float = 0.0

    # State
    state: DCATradeState = DCATradeState.IDLE
    highest_price: float = 0.0        # For trailing TP
    lowest_price: float = float('inf')

    # Result
    realized_pnl: float = 0.0
    closed_at: Optional[float] = None
    close_reason: str = ""
    fees_paid: float = 0.0  # Trading fees for this trade


class DCAStrategy:
    """
    DCA Trading Strategy.

    Implements dollar-cost averaging with safety orders.
    """

    def __init__(
        self,
        config: DCAConfig,
        api_client,
        order_builder,
        tick_size: float = 0.1,
        min_order_size: float = 0.01,
    ):
        self.config = config
        self.api = api_client
        self.order_builder = order_builder
        self._tick_size = tick_size
        self._min_order_size = min_order_size

        # State
        self._current_trade: Optional[DCATrade] = None
        self._trade_history: List[DCATrade] = []
        self._last_trade_closed_at: float = 0

        # Stats
        self._total_trades = 0
        self._winning_trades = 0
        self._losing_trades = 0
        self._total_pnl = 0.0
        self._total_fees = 0.0
        self._total_wins_pnl = 0.0
        self._total_losses_pnl = 0.0

        logger.info(
            f"DCA Strategy initialized: {config.direction.value} "
            f"base=${config.base_order_size_usd}, "
            f"SOs={config.max_safety_orders}, "
            f"TP={config.take_profit_pct}%"
        )

    @property
    def has_active_trade(self) -> bool:
        """Check if there's an active trade."""
        return self._current_trade is not None and self._current_trade.state in (
            DCATradeState.WAITING_BASE,
            DCATradeState.ACTIVE,
            DCATradeState.TAKING_PROFIT,
        )

    @property
    def current_trade(self) -> Optional[DCATrade]:
        """Get current trade."""
        return self._current_trade

    def _round_price(self, price: float, down: bool = False) -> float:
        """Round price to tick size (e.g., 0.1 for ETH-USD)."""
        tick = Decimal(str(self._tick_size))
        p = Decimal(str(price))
        if down:
            rounded = (p / tick).to_integral_value(rounding='ROUND_DOWN') * tick
        else:
            rounded = (p / tick).to_integral_value(rounding='ROUND_HALF_UP') * tick
        return float(rounded)

    def _round_size(self, size: float) -> float:
        """Round size to minimum increment."""
        return float(round(Decimal(str(size)) / Decimal(str(self._min_order_size))) * Decimal(str(self._min_order_size)))

    def calculate_safety_orders(self, base_price: float) -> List[SafetyOrder]:
        """
        Calculate all safety order prices and sizes.

        For LONG: safety orders are placed below base price
        For SHORT: safety orders are placed above base price
        """
        safety_orders = []

        current_deviation = self.config.price_deviation_pct
        current_size_usd = self.config.safety_order_size_usd

        for i in range(1, self.config.max_safety_orders + 1):
            # Calculate price
            if self.config.direction == DCADirection.LONG:
                # Buy lower
                so_price = base_price * (1 - current_deviation / 100)
            else:
                # Sell higher
                so_price = base_price * (1 + current_deviation / 100)

            so_price = self._round_price(so_price)

            # Calculate size
            so_size = current_size_usd / so_price
            so_size = self._round_size(so_size)

            safety_orders.append(SafetyOrder(
                index=i,
                price=so_price,
                size=so_size,
                size_usd=current_size_usd,
                deviation_pct=current_deviation,
            ))

            # Scale for next SO
            current_deviation += self.config.price_deviation_pct * (self.config.safety_order_step_scale ** (i - 1))
            current_size_usd *= self.config.safety_order_volume_scale

        return safety_orders

    def calculate_average_price(self, trade: DCATrade) -> float:
        """Calculate average entry price including all filled orders."""
        if trade.total_size == 0:
            return 0.0
        return trade.total_cost / trade.total_size

    def calculate_take_profit_price(self, avg_price: float) -> float:
        """
        Calculate take profit price from average entry.

        The TP price is calculated so that NET PROFIT equals take_profit_pct.
        This accounts for taker fees on the TP order.

        Formula for LONG:
        - We want: (tp_price - avg_price) - tp_price * taker_fee = target_profit
        - Where target_profit = avg_price * take_profit_pct
        - Solving: tp_price = avg_price * (1 + take_profit_pct) / (1 - taker_fee)
        """
        taker_fee = 0.000225  # 0.0225% from Extended Exchange
        target_pct = self.config.take_profit_pct / 100

        if self.config.direction == DCADirection.LONG:
            # TP price that gives net profit of take_profit_pct after fees
            tp_price = avg_price * (1 + target_pct) / (1 - taker_fee)
        else:
            # For SHORT: we buy back lower
            tp_price = avg_price * (1 - target_pct) / (1 + taker_fee)
        return self._round_price(tp_price)

    def calculate_stop_loss_price(self, avg_price: float) -> float:
        """Calculate stop loss price from average entry."""
        if self.config.stop_loss_pct <= 0:
            return 0.0

        if self.config.direction == DCADirection.LONG:
            sl_price = avg_price * (1 - self.config.stop_loss_pct / 100)
        else:
            sl_price = avg_price * (1 + self.config.stop_loss_pct / 100)
        return self._round_price(sl_price)

    async def start_new_trade(self, current_price: float, best_bid: float = None) -> Optional[DCATrade]:
        """
        Start a new DCA trade.

        Args:
            current_price: Current market price (mid price)
            best_bid: Best bid price from orderbook (optional, preferred for entry)

        Returns:
            New trade object or None if cannot start
        """
        # Check cooldown
        if self._last_trade_closed_at > 0:
            elapsed = time.time() - self._last_trade_closed_at
            if elapsed < self.config.cooldown_between_trades_sec:
                logger.debug(f"Cooldown active: {elapsed:.0f}s / {self.config.cooldown_between_trades_sec}s")
                return None

        # Check if already in trade
        if self.has_active_trade:
            logger.warning("Cannot start new trade: already in active trade")
            return None

        # Create trade
        trade_id = f"dca_{int(time.time() * 1000)}"
        trade = DCATrade(
            id=trade_id,
            direction=self.config.direction,
            market=self.config.market,
            started_at=time.time(),
            state=DCATradeState.WAITING_BASE,
        )

        # Use best_bid for LONG entry (maker order at top of book)
        # This ensures we get maker fees (0%) and execute when a seller comes
        # Note: best_bid may have extra precision from exchange, so we round DOWN to tick_size
        # ROUND_DOWN ensures we stay at or above best_bid level (not below)
        if self.config.direction == DCADirection.LONG:
            if best_bid and best_bid > 0:
                base_price = self._round_price(best_bid, down=True)  # Round to tick_size
            else:
                base_price = self._round_price(current_price, down=True)
        else:
            # For SHORT, we'd use best_ask
            base_price = self._round_price(current_price)

        base_size = self.config.base_order_size_usd / base_price
        base_size = self._round_size(base_size)

        trade.base_order_price = base_price
        trade.base_order_size = base_size

        # Calculate safety orders based on entry price
        trade.safety_orders = self.calculate_safety_orders(base_price)

        # Place base order
        side = "BUY" if self.config.direction == DCADirection.LONG else "SELL"

        logger.info(
            f"[DCA] Starting new {self.config.direction.value} trade: "
            f"{side} {base_size:.4f} @ ${base_price:.2f} (best_bid: ${best_bid or 0:.2f})"
        )

        # Build and place order
        order_payload = self.order_builder.build_limit_order(
            market=self.config.market,
            side=side,
            size=Decimal(str(base_size)),
            price=Decimal(str(base_price)),
            post_only=True,
        )

        response = await self.api.create_order(order_payload)

        if response.success:
            trade.base_order_id = _extract_order_id(response.data) or trade_id
            self._current_trade = trade
            logger.info(f"[DCA] Base order placed: {trade.base_order_id}")
            return trade
        else:
            logger.error(f"[DCA] Failed to place base order: {response.error}")
            return None

    async def _refresh_base_order(self, trade: DCATrade, current_price: float, best_bid: float = None) -> bool:
        """
        Cancel existing base order and re-place at current price.
        Used when the order hasn't filled within entry_refresh_seconds.

        This function REPLACES the existing order - it cancels the old one first,
        then places a new one at the updated price.

        Note: Safety orders are NOT placed until base order fills, so we only
        need to recalculate them here (they'll be placed when base order fills).
        """
        old_order_id = trade.base_order_id

        # Step 1: Cancel existing order
        if old_order_id:
            try:
                cancel_response = await self.api.cancel_order(old_order_id)
                if cancel_response.success:
                    logger.info(f"[DCA] Cancelled old base order: {old_order_id}")
                else:
                    # Check if order was already filled or doesn't exist
                    error_msg = str(cancel_response.error).lower()
                    if "not found" in error_msg or "does not exist" in error_msg:
                        logger.info(f"[DCA] Old order {old_order_id} already gone (filled or cancelled)")
                    else:
                        logger.warning(f"[DCA] Failed to cancel old order {old_order_id}: {cancel_response.error}")
                        # Don't continue if we couldn't cancel - avoid duplicate orders
                        return False
            except Exception as e:
                logger.warning(f"[DCA] Exception cancelling old order {old_order_id}: {e}")
                # Don't continue if we couldn't cancel
                return False

        # Small delay to ensure cancel is processed
        await asyncio.sleep(0.3)

        # Step 2: Calculate new price - use best_bid for LONG (maker order)
        # Note: best_bid may have extra precision, so we round DOWN to tick_size
        if self.config.direction == DCADirection.LONG:
            if best_bid and best_bid > 0:
                new_price = self._round_price(best_bid, down=True)  # Round to tick_size
            else:
                new_price = self._round_price(current_price, down=True)
        else:
            new_price = self._round_price(current_price)

        new_size = self.config.base_order_size_usd / new_price
        new_size = self._round_size(new_size)

        # Step 3: Place new order
        side = "BUY" if self.config.direction == DCADirection.LONG else "SELL"

        logger.info(f"[DCA] Placing refreshed base order: {side} {new_size:.4f} @ ${new_price:.2f} (best_bid: ${best_bid or 0:.2f})")

        order_payload = self.order_builder.build_limit_order(
            market=self.config.market,
            side=side,
            size=Decimal(str(new_size)),
            price=Decimal(str(new_price)),
            post_only=True,
        )

        response = await self.api.create_order(order_payload)

        if response.success:
            # Get new order ID
            new_order_id = _extract_order_id(response.data)

            if not new_order_id:
                logger.error(f"[DCA] Order placed but no ID returned! Response: {response.data}")
                return False

            # Update trade with new order info
            trade.base_order_id = new_order_id
            trade.base_order_price = new_price
            trade.base_order_size = new_size
            trade.started_at = time.time()  # Reset timer for next refresh

            # Recalculate safety orders based on new price
            # Note: SOs are not placed yet - they're placed when base order fills
            trade.safety_orders = self.calculate_safety_orders(new_price)

            logger.info(f"[DCA] Base order refreshed: {new_order_id} @ ${new_price:.2f}")
            return True
        else:
            logger.error(f"[DCA] Failed to place refreshed base order: {response.error}")
            # Don't reset timer - will retry on next cycle
            return False

    async def place_safety_orders(self, trade: DCATrade) -> int:
        """
        Place safety orders for the trade.

        Returns number of orders placed.
        """
        placed = 0
        side = "BUY" if self.config.direction == DCADirection.LONG else "SELL"

        for so in trade.safety_orders:
            if so.filled or so.order_id:
                continue  # Already placed or filled

            logger.info(
                f"[DCA] Placing SO{so.index}: {side} {so.size:.4f} @ ${so.price:.2f} "
                f"(dev: -{so.deviation_pct:.1f}%)"
            )

            order_payload = self.order_builder.build_limit_order(
                market=self.config.market,
                side=side,
                size=Decimal(str(so.size)),
                price=Decimal(str(so.price)),
                post_only=True,
                expiration_sec=604800,  # 7 days - SO-uri trebuie să rămână active
            )

            response = await self.api.create_order(order_payload)

            if response.success:
                so.order_id = _extract_order_id(response.data)
                trade.active_safety_orders += 1
                placed += 1
                logger.info(f"[DCA] SO{so.index} placed: {so.order_id}")
            else:
                logger.error(f"[DCA] Failed to place SO{so.index}: {response.error}")

            await asyncio.sleep(0.2)  # Small delay between orders

        return placed

    async def place_take_profit_order(self, trade: DCATrade) -> bool:
        """
        Place take profit order.

        Note: TP uses post_only=False to allow immediate execution when price is reached.
        This means we pay taker fees (~0.05%) but ensure the order executes.
        """
        if trade.take_profit_order_id:
            # Cancel existing TP order first
            await self.api.cancel_order(trade.take_profit_order_id)
            trade.take_profit_order_id = None

        tp_price = self.calculate_take_profit_price(trade.average_price)
        trade.take_profit_price = tp_price

        # TP order is opposite direction
        side = "SELL" if self.config.direction == DCADirection.LONG else "BUY"

        logger.info(
            f"[DCA] Placing TP: {side} {trade.total_size:.4f} @ ${tp_price:.2f} "
            f"(avg entry: ${trade.average_price:.2f}, target: +{self.config.take_profit_pct}%)"
        )

        # TP order uses post_only=False so it executes immediately when price is reached
        # We accept taker fees for guaranteed execution
        order_payload = self.order_builder.build_limit_order(
            market=self.config.market,
            side=side,
            size=Decimal(str(trade.total_size)),
            price=Decimal(str(tp_price)),
            post_only=False,  # Allow taker execution for TP
            reduce_only=True,
        )

        response = await self.api.create_order(order_payload)

        if response.success:
            trade.take_profit_order_id = _extract_order_id(response.data)
            logger.info(f"[DCA] TP order placed: {trade.take_profit_order_id}")
            return True
        else:
            logger.error(f"[DCA] Failed to place TP: {response.error}")
            return False

    async def check_and_update_trade(
        self,
        current_price: float,
        position_size: float,
        best_bid: float = None,
        exchange_pnl: float = None,
        exchange_pnl_pct: float = None
    ) -> None:
        """
        Check and update the current trade state.

        Args:
            current_price: Current market price (mark_price preferred, fallback to mid_price)
            position_size: Current position size from exchange (positive=long, negative=short)
            best_bid: Best bid price from orderbook (used for entry refresh)
            exchange_pnl: Unrealized PnL in USD directly from exchange
            exchange_pnl_pct: Unrealized PnL percentage from exchange (used for TP/SL)
        """
        trade = self._current_trade
        if not trade:
            return

        # Update price tracking
        trade.highest_price = max(trade.highest_price, current_price)
        trade.lowest_price = min(trade.lowest_price, current_price)

        # State machine
        if trade.state == DCATradeState.WAITING_BASE:
            # Check if base order filled
            if abs(position_size) >= trade.base_order_size * 0.9:  # 90% tolerance
                trade.base_order_filled = True
                trade.total_size = abs(position_size)
                trade.total_cost = trade.total_size * trade.base_order_price
                trade.average_price = self.calculate_average_price(trade)
                trade.state = DCATradeState.ACTIVE

                logger.info(
                    f"[DCA] Base order FILLED: {trade.total_size:.4f} @ ${trade.base_order_price:.2f}"
                )

                # Place safety orders
                await self.place_safety_orders(trade)

                # Calculate TP target (for logging only - we monitor PnL instead of placing TP order)
                trade.take_profit_price = self.calculate_take_profit_price(trade.average_price)
                logger.info(
                    f"[DCA] Monitoring for TP: target PnL >= {self.config.take_profit_pct}% "
                    f"(~${trade.total_cost * self.config.take_profit_pct / 100:.2f})"
                )

            # Check if we need to refresh the base order (re-entry at new price)
            elif self.config.entry_refresh_seconds > 0:
                time_waiting = time.time() - trade.started_at
                if time_waiting >= self.config.entry_refresh_seconds:
                    # Cancel old order and re-place at current price (using best_bid for LONG)
                    logger.info(
                        f"[DCA] Base order not filled after {time_waiting:.0f}s. "
                        f"Refreshing entry at best_bid=${best_bid or current_price:.2f}"
                    )
                    await self._refresh_base_order(trade, current_price, best_bid)

        elif trade.state == DCATradeState.ACTIVE:
            # Check if any safety orders filled
            expected_size = trade.base_order_size
            for so in trade.safety_orders:
                if so.filled:
                    expected_size += so.size

            if abs(position_size) > expected_size * 0.9:
                # A safety order was filled
                for so in trade.safety_orders:
                    if not so.filled and abs(position_size) >= expected_size + so.size * 0.5:
                        so.filled = True
                        so.filled_at = time.time()
                        so.filled_price = current_price
                        trade.filled_safety_orders += 1
                        trade.active_safety_orders -= 1

                        # Update totals
                        trade.total_size = abs(position_size)
                        trade.total_cost += so.size * so.price
                        trade.average_price = self.calculate_average_price(trade)

                        logger.info(
                            f"[DCA] SO{so.index} FILLED @ ${so.price:.2f}. "
                            f"New avg: ${trade.average_price:.2f}, size: {trade.total_size:.4f}"
                        )

                        # Update TP target price (for logging)
                        trade.take_profit_price = self.calculate_take_profit_price(trade.average_price)
                        logger.info(
                            f"[DCA] Updated TP target: PnL >= {self.config.take_profit_pct}% "
                            f"(~${trade.total_cost * self.config.take_profit_pct / 100:.2f})"
                        )
                        break

            # Use exchange PnL percentage directly if available and valid (most accurate)
            # This matches exactly what the exchange shows
            # Note: exchange_pnl_pct can be 0.0 when margin is 0, so we need to check that it's truly valid
            if exchange_pnl_pct is not None and exchange_pnl is not None and abs(exchange_pnl) > 0.0001:
                pnl_pct = exchange_pnl_pct
                unrealized_pnl = exchange_pnl
            else:
                # Fallback to local calculation when exchange data is not available or invalid
                unrealized_pnl = self._calculate_pnl(trade, current_price)
                pnl_pct = (unrealized_pnl / trade.total_cost * 100) if trade.total_cost > 0 else 0

            # Log PnL values for debugging
            logger.debug(
                f"[DCA] PnL check: exchange_pnl={exchange_pnl}, exchange_pnl_pct={exchange_pnl_pct}, "
                f"pnl_pct={pnl_pct:.4f}%, TP threshold={self.config.take_profit_pct}%"
            )

            # Check take profit based on exchange PnL percentage
            if pnl_pct >= self.config.take_profit_pct:
                logger.info(
                    f"[DCA] TAKE PROFIT triggered! xPnL: ${unrealized_pnl:.4f} ({pnl_pct:.3f}%) >= {self.config.take_profit_pct}%"
                )
                await self._close_trade(trade, "take_profit", current_price)
                return

            # Check stop loss
            if self.config.stop_loss_pct > 0:
                sl_price = self.calculate_stop_loss_price(trade.average_price)
                trade.stop_loss_price = sl_price

                # Check based on exchange PnL percentage (negative)
                if pnl_pct <= -self.config.stop_loss_pct:
                    logger.warning(
                        f"[DCA] STOP LOSS triggered! xPnL: ${unrealized_pnl:.4f} ({pnl_pct:.3f}%) <= -{self.config.stop_loss_pct}%"
                    )
                    await self._close_trade(trade, "stop_loss", current_price)
                    return

            # Check if position closed externally
            if abs(position_size) < self._min_order_size:
                # Position was closed (externally or by exchange)
                pnl = self._calculate_pnl(trade, current_price)
                trade.realized_pnl = pnl
                trade.state = DCATradeState.COMPLETED
                trade.closed_at = time.time()
                trade.close_reason = "external_close"

                # Fetch fees from recent fills
                trade.fees_paid = await self._fetch_trade_fees(trade)

                logger.info(
                    f"[DCA] Trade COMPLETED (position closed externally). "
                    f"PnL: ${pnl:.2f} ({pnl/trade.total_cost*100:.2f}%), Fees: ${trade.fees_paid:.2f}"
                )

                self._finalize_trade(trade)

    async def _close_trade(self, trade: DCATrade, reason: str, close_price: float) -> None:
        """Close trade by placing market order."""
        # Cancel all open orders
        await self.api.cancel_all_orders(trade.market)

        # Get actual position size from exchange
        actual_size = trade.total_size
        try:
            pos_response = await self.api.get_positions()
            if pos_response.success and pos_response.data:
                positions = pos_response.data.get("data", [])
                for pos in positions:
                    if pos.get("market") == trade.market:
                        actual_size = abs(float(pos.get("size", 0)))
                        break
        except Exception as e:
            logger.warning(f"[DCA] Could not fetch position size: {e}, using tracked size")

        # If no position exists, skip closing
        if actual_size < self._min_order_size:
            logger.info(f"[DCA] No position to close (size: {actual_size:.4f})")
            trade.state = DCATradeState.COMPLETED
            trade.closed_at = time.time()
            trade.close_reason = reason
            # Fetch fees from recent fills
            trade.fees_paid = await self._fetch_trade_fees(trade)
            self._finalize_trade(trade)
            return

        # Place market order to close
        side = "SELL" if self.config.direction == DCADirection.LONG else "BUY"

        logger.info(f"[DCA] Closing trade: {side} {actual_size:.4f} @ market (tracked: {trade.total_size:.4f})")

        # Calculate aggressive price (2% worse to ensure fill)
        raw_price = close_price * (1.02 if side == "BUY" else 0.98)
        # Round to valid price precision (0.1 for ETH-USD)
        rounded_price = self._round_price(raw_price, down=(side == "SELL"))

        order_payload = self.order_builder.build_ioc_order(
            market=trade.market,
            side=side,
            size=Decimal(str(actual_size)),
            price=Decimal(str(rounded_price)),
            reduce_only=True,
        )

        response = await self.api.create_order(order_payload)

        if response.success:
            pnl = self._calculate_pnl(trade, close_price)
            trade.realized_pnl = pnl
            trade.state = DCATradeState.STOPPED if reason == "stop_loss" else DCATradeState.COMPLETED
            trade.closed_at = time.time()
            trade.close_reason = reason

            # Fetch fees from recent fills
            trade.fees_paid = await self._fetch_trade_fees(trade)

            logger.info(f"[DCA] Trade closed: {reason}, PnL: ${pnl:.2f}, Fees: ${trade.fees_paid:.2f}")
            self._finalize_trade(trade)
        else:
            logger.error(f"[DCA] Failed to close trade: {response.error}")

    async def _fetch_trade_fees(self, trade: DCATrade) -> float:
        """Fetch total fees paid for this trade from recent fills."""
        try:
            # Get recent fills for this market
            response = await self.api.get_fills(market=trade.market, limit=50)
            if not response.success:
                logger.warning(f"[DCA] Could not fetch fills for fees: {response.error}")
                return 0.0

            # Handle different response formats
            raw_data = response.data
            if isinstance(raw_data, dict):
                fills = raw_data.get("data", raw_data.get("trades", []))
            elif isinstance(raw_data, list):
                fills = raw_data
            else:
                fills = []

            if not fills:
                logger.info(f"[DCA] No fills found. Response: {str(raw_data)[:200]}")
                return 0.0

            # Log first fill to see structure
            if fills:
                logger.info(f"[DCA] Sample fill: {fills[0]}")

            # Sum all fees from recent fills (simpler approach - sum all)
            total_fees = 0.0
            trade_start = trade.started_at
            trade_end = trade.closed_at or time.time()

            for fill in fills:
                # Try multiple timestamp field names
                fill_time = 0
                for ts_field in ["timestamp", "time", "createdAt", "created_at", "executedAt"]:
                    if ts_field in fill:
                        fill_time = float(fill[ts_field])
                        break

                if fill_time > 1e12:  # milliseconds
                    fill_time = fill_time / 1000

                # Check if fill is within trade timeframe (with buffer)
                if fill_time == 0 or (trade_start - 120 <= fill_time <= trade_end + 120):
                    # Try multiple fee field names
                    fee = 0.0
                    for fee_field in ["fee", "payedFee", "tradingFee", "commission", "feeAmount"]:
                        if fee_field in fill and fill[fee_field]:
                            fee = float(fill[fee_field])
                            break
                    total_fees += abs(fee)

            logger.debug(f"[DCA] Total fees calculated: ${total_fees:.4f} from {len(fills)} fills")
            return total_fees

        except Exception as e:
            logger.warning(f"[DCA] Error fetching fees: {e}")
            return 0.0

    def _calculate_pnl(self, trade: DCATrade, exit_price: float) -> float:
        """Calculate PnL for trade."""
        if self.config.direction == DCADirection.LONG:
            # Bought at avg price, sold at exit price
            pnl = (exit_price - trade.average_price) * trade.total_size
        else:
            # Sold at avg price, bought back at exit price
            pnl = (trade.average_price - exit_price) * trade.total_size
        return pnl

    def _finalize_trade(self, trade: DCATrade) -> None:
        """Finalize trade and update stats."""
        self._trade_history.append(trade)
        self._total_trades += 1
        self._total_pnl += trade.realized_pnl
        self._total_fees += trade.fees_paid

        if trade.realized_pnl > 0:
            self._winning_trades += 1
            self._total_wins_pnl += trade.realized_pnl
        else:
            self._losing_trades += 1
            self._total_losses_pnl += abs(trade.realized_pnl)

        self._last_trade_closed_at = time.time()
        self._current_trade = None

        # Calculate stats
        win_rate = (self._winning_trades / self._total_trades * 100) if self._total_trades > 0 else 0
        avg_win = (self._total_wins_pnl / self._winning_trades) if self._winning_trades > 0 else 0
        avg_loss = (self._total_losses_pnl / self._losing_trades) if self._losing_trades > 0 else 0
        net_pnl = self._total_pnl - self._total_fees

        logger.info(
            f"[STATS] Trades: {self._total_trades} | "
            f"Win: {self._winning_trades} ({win_rate:.1f}%) | "
            f"Loss: {self._losing_trades} | "
            f"PnL: ${self._total_pnl:.2f} | "
            f"Fees: ${self._total_fees:.2f} | "
            f"Net: ${net_pnl:.2f}"
        )
        if self._winning_trades > 0 or self._losing_trades > 0:
            logger.info(
                f"[STATS] Avg Win: ${avg_win:.2f} | "
                f"Avg Loss: ${avg_loss:.2f}"
            )

    async def cancel_all_orders(self) -> None:
        """Cancel all orders for current trade."""
        if self._current_trade:
            await self.api.cancel_all_orders(self.config.market)
            logger.info("[DCA] All orders cancelled")

    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status."""
        trade = self._current_trade

        return {
            "has_active_trade": self.has_active_trade,
            "direction": self.config.direction.value,
            "total_trades": self._total_trades,
            "winning_trades": self._winning_trades,
            "win_rate": (self._winning_trades / self._total_trades * 100) if self._total_trades > 0 else 0,
            "total_pnl": self._total_pnl,
            "current_trade": {
                "id": trade.id if trade else None,
                "state": trade.state.value if trade else None,
                "average_price": trade.average_price if trade else 0,
                "total_size": trade.total_size if trade else 0,
                "filled_safety_orders": trade.filled_safety_orders if trade else 0,
                "take_profit_price": trade.take_profit_price if trade else 0,
                "stop_loss_price": trade.stop_loss_price if trade else 0,
            } if trade else None,
        }
