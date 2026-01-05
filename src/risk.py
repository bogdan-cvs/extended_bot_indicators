"""
Risk management module for Extended MM Bot.
Handles inventory control, kill switches, and safety mechanisms.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging

from .config import BotConfig, RiskConfig

logger = logging.getLogger(__name__)


class BotState(Enum):
    """Bot operational states."""
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    COOLDOWN = "cooldown"
    KILLED = "killed"
    STOPPED = "stopped"


class KillReason(Enum):
    """Reasons for kill switch activation."""
    DRAWDOWN_LIMIT = "drawdown_limit"
    SESSION_DRAWDOWN = "session_drawdown"
    MANUAL = "manual"
    ERROR_LIMIT = "error_limit"
    CONNECTIVITY_LOSS = "connectivity_loss"


@dataclass
class Position:
    """Current position information."""
    market: str
    size: float = 0.0  # Positive = long, negative = short
    entry_price: float = 0.0
    mark_price: float = 0.0
    notional_usd: float = 0.0
    unrealized_pnl: float = 0.0
    
    @property
    def is_long(self) -> bool:
        return self.size > 0
    
    @property
    def is_short(self) -> bool:
        return self.size < 0
    
    @property
    def abs_size(self) -> float:
        return abs(self.size)


@dataclass
class Fill:
    """Trade fill/execution record."""
    market: str
    side: str  # "BUY" or "SELL"
    price: float
    size: float
    fee: float
    timestamp: float
    order_id: str = ""
    is_maker: bool = True
    
    @property
    def notional(self) -> float:
        return self.price * self.size
    
    @property
    def pnl_contribution(self) -> float:
        """Estimate PnL contribution (simplified)."""
        # For a fill, we can't know exact PnL without tracking entry
        return -self.fee  # At minimum, we pay the fee


@dataclass
class RiskState:
    """Current risk state of the bot."""
    # Position tracking
    position: Position = field(default_factory=lambda: Position(""))
    inventory_usd: float = 0.0
    
    # PnL tracking
    session_start_balance: float = 0.0
    current_balance: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_fees_paid: float = 0.0
    
    # Fill tracking
    recent_fills: list[Fill] = field(default_factory=list)
    total_fills: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    
    # State tracking
    state: BotState = BotState.STARTING
    pause_start_time: float = 0.0
    pause_reason: str = ""
    kill_reason: Optional[KillReason] = None
    
    # Volatility tracking
    last_mid_prices: list[tuple[float, float]] = field(default_factory=list)  # (timestamp, price)
    
    @property
    def session_drawdown_usd(self) -> float:
        """Calculate session drawdown in USD."""
        if self.session_start_balance == 0:
            return 0.0
        return self.session_start_balance - self.current_balance - self.unrealized_pnl
    
    @property
    def session_drawdown_pct(self) -> float:
        """Calculate session drawdown as percentage."""
        if self.session_start_balance == 0:
            return 0.0
        return (self.session_drawdown_usd / self.session_start_balance) * 100
    
    @property
    def total_pnl(self) -> float:
        """Total PnL including unrealized."""
        return self.realized_pnl + self.unrealized_pnl


class RiskManager:
    """
    Risk management engine for the market making bot.
    
    Responsibilities:
    - Track inventory and position limits
    - Monitor drawdown and activate kill switch
    - Detect volatility spikes and trigger pauses
    - Calculate inventory skew for quote adjustment
    - Track fills and PnL
    """
    
    MAX_RECENT_FILLS = 50
    MAX_MID_PRICES = 100
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.risk_config: RiskConfig = config.risk
        self.state = RiskState()
        self.state.position = Position(market=config.strategy.market)
        
        # Error tracking
        self._error_count = 0
        self._max_errors = 10
        self._error_window_sec = 60
        self._error_timestamps: list[float] = []
    
    def initialize(self, starting_balance: float):
        """Initialize risk state with starting balance."""
        self.state.session_start_balance = starting_balance
        self.state.current_balance = starting_balance
        self.state.state = BotState.RUNNING
        logger.info(f"Risk manager initialized. Starting balance: ${starting_balance:.2f}")
    
    def update_position(self, position: Position):
        """Update current position from exchange."""
        self.state.position = position
        self.state.inventory_usd = abs(position.size * position.mark_price)
        self.state.unrealized_pnl = position.unrealized_pnl
    
    def update_balance(self, balance: float):
        """Update current balance."""
        self.state.current_balance = balance
    
    def record_fill(self, fill: Fill):
        """Record a trade fill."""
        self.state.recent_fills.append(fill)
        if len(self.state.recent_fills) > self.MAX_RECENT_FILLS:
            self.state.recent_fills.pop(0)
        
        self.state.total_fills += 1
        if fill.is_maker:
            self.state.maker_fills += 1
        else:
            self.state.taker_fills += 1
        
        self.state.total_fees_paid += fill.fee
        
        logger.info(
            f"Fill recorded: {fill.side} {fill.size:.6f} @ {fill.price:.4f} "
            f"(fee: ${fill.fee:.4f}, maker: {fill.is_maker})"
        )
    
    def update_mid_price(self, mid_price: float):
        """Track mid price for volatility detection."""
        now = time.time()
        self.state.last_mid_prices.append((now, mid_price))
        
        # Keep only recent prices
        cutoff = now - self.config.strategy.volatility_lookback_sec
        self.state.last_mid_prices = [
            (t, p) for t, p in self.state.last_mid_prices 
            if t >= cutoff
        ][-self.MAX_MID_PRICES:]
    
    def check_kill_switch(self) -> tuple[bool, Optional[KillReason]]:
        """
        Check if kill switch should be activated.
        
        Returns:
            (should_kill, reason)
        """
        if self.state.state == BotState.KILLED:
            return True, self.state.kill_reason
        
        # Check drawdown limit
        if self.state.session_drawdown_usd >= self.risk_config.kill_switch_drawdown_usd:
            logger.critical(
                f"KILL SWITCH: Drawdown limit reached. "
                f"Drawdown: ${self.state.session_drawdown_usd:.2f} >= "
                f"${self.risk_config.kill_switch_drawdown_usd:.2f}"
            )
            return True, KillReason.DRAWDOWN_LIMIT
        
        # Check session drawdown percentage
        if self.state.session_drawdown_pct >= self.risk_config.session_drawdown_pct:
            logger.critical(
                f"KILL SWITCH: Session drawdown percentage reached. "
                f"Drawdown: {self.state.session_drawdown_pct:.1f}% >= "
                f"{self.risk_config.session_drawdown_pct:.1f}%"
            )
            return True, KillReason.SESSION_DRAWDOWN
        
        # Check error rate
        now = time.time()
        self._error_timestamps = [
            t for t in self._error_timestamps 
            if now - t < self._error_window_sec
        ]
        if len(self._error_timestamps) >= self._max_errors:
            logger.critical("KILL SWITCH: Too many errors")
            return True, KillReason.ERROR_LIMIT
        
        return False, None

    def check_stop_loss(self) -> tuple[bool, dict]:
        """
        Check if stop-loss should be triggered.

        Returns:
            (should_stop_loss, close_order_params)
            close_order_params: {"side": "BUY"/"SELL", "size": float, "market": str}
        """
        # Stop-loss disabled
        if self.risk_config.stop_loss_pct <= 0:
            return False, {}

        position = self.state.position
        if position.size == 0:
            return False, {}

        # Calculate loss percentage based on unrealized PnL vs position notional
        position_notional = abs(position.size * position.entry_price)
        if position_notional == 0:
            return False, {}

        # Unrealized PnL is negative when losing
        unrealized_pnl = position.unrealized_pnl
        loss_pct = (-unrealized_pnl / position_notional) * 100 if unrealized_pnl < 0 else 0

        if loss_pct >= self.risk_config.stop_loss_pct:
            # Need to close position
            # If LONG (size > 0), close with SELL
            # If SHORT (size < 0), close with BUY
            close_side = "SELL" if position.size > 0 else "BUY"
            close_size = abs(position.size)

            logger.warning(
                f"STOP-LOSS TRIGGERED: Loss {loss_pct:.2f}% >= {self.risk_config.stop_loss_pct:.1f}%. "
                f"Closing {close_side} {close_size} {position.market}"
            )

            return True, {
                "side": close_side,
                "size": close_size,
                "market": position.market,
                "loss_pct": loss_pct,
            }

        return False, {}

    def check_take_profit(self) -> tuple[bool, dict]:
        """
        Check if take-profit should be triggered.

        Returns:
            (should_take_profit, close_order_params)
            close_order_params: {"side": "BUY"/"SELL", "size": float, "market": str}
        """
        # Take-profit disabled
        if self.risk_config.take_profit_pct <= 0:
            return False, {}

        position = self.state.position
        if position.size == 0:
            return False, {}

        # Calculate profit percentage based on unrealized PnL vs position notional
        position_notional = abs(position.size * position.entry_price)
        if position_notional == 0:
            return False, {}

        # Unrealized PnL is positive when profitable
        unrealized_pnl = position.unrealized_pnl
        profit_pct = (unrealized_pnl / position_notional) * 100 if unrealized_pnl > 0 else 0

        if profit_pct >= self.risk_config.take_profit_pct:
            # Need to close position to lock in profit
            # If LONG (size > 0), close with SELL
            # If SHORT (size < 0), close with BUY
            close_side = "SELL" if position.size > 0 else "BUY"
            close_size = abs(position.size)

            logger.warning(
                f"TAKE-PROFIT TRIGGERED: Profit {profit_pct:.2f}% >= {self.risk_config.take_profit_pct:.1f}%. "
                f"Closing {close_side} {close_size} {position.market}"
            )

            return True, {
                "side": close_side,
                "size": close_size,
                "market": position.market,
                "profit_pct": profit_pct,
            }

        return False, {}

    def activate_kill_switch(self, reason: KillReason):
        """Activate the kill switch."""
        self.state.state = BotState.KILLED
        self.state.kill_reason = reason
        logger.critical(f"KILL SWITCH ACTIVATED: {reason.value}")
    
    def check_pause_conditions(self, current_mid: float) -> tuple[bool, str]:
        """
        Check if trading should be paused.
        
        Returns:
            (should_pause, reason)
        """
        if self.state.state in (BotState.KILLED, BotState.STOPPED):
            return True, "Bot stopped"
        
        if self.state.state == BotState.PAUSED:
            return True, self.state.pause_reason
        
        if self.state.state == BotState.COOLDOWN:
            elapsed = time.time() - self.state.pause_start_time
            if elapsed < self.config.strategy.cooldown_after_pause_sec:
                return True, f"Cooldown ({elapsed:.0f}s remaining)"
            else:
                self.state.state = BotState.RUNNING
        
        # Check for volatility spike
        move_bps = self._calculate_recent_move_bps(
            self.config.strategy.pause_window_sec
        )
        
        if move_bps >= self.config.strategy.pause_move_bps:
            reason = f"Volatility spike: {move_bps:.1f} bps in {self.config.strategy.pause_window_sec}s"
            logger.warning(f"PAUSE: {reason}")
            return True, reason
        
        # Check for too many negative fills
        recent_negative = self._count_negative_fills()
        if recent_negative >= self.risk_config.negative_fills_pause_threshold:
            reason = f"Too many negative fills: {recent_negative}"
            logger.warning(f"PAUSE: {reason}")
            return True, reason
        
        return False, ""
    
    def enter_pause(self, reason: str):
        """Enter pause state."""
        self.state.state = BotState.PAUSED
        self.state.pause_start_time = time.time()
        self.state.pause_reason = reason
        logger.info(f"Entering PAUSE: {reason}")
    
    def exit_pause(self):
        """Exit pause and enter cooldown."""
        self.state.state = BotState.COOLDOWN
        self.state.pause_start_time = time.time()
        logger.info(f"Exiting PAUSE, entering COOLDOWN for {self.config.strategy.cooldown_after_pause_sec}s")
    
    def check_inventory_limits(self) -> tuple[bool, str]:
        """
        Check if inventory limits are exceeded.
        
        Returns:
            (within_limits, warning_message)
        """
        inventory = self.state.inventory_usd
        max_inventory = self.risk_config.max_inventory_usd
        
        if inventory > max_inventory:
            return False, f"Inventory ${inventory:.2f} exceeds limit ${max_inventory:.2f}"
        
        if inventory > max_inventory * 0.8:
            return True, f"Warning: Inventory ${inventory:.2f} approaching limit ${max_inventory:.2f}"
        
        return True, ""
    
    def calculate_inventory_skew(self) -> float:
        """
        Calculate inventory skew factor for quote adjustment.
        
        Returns:
            Skew factor: positive = reduce long exposure, negative = reduce short exposure
            Range: typically -1 to 1
        """
        if self.state.inventory_usd == 0:
            return 0.0
        
        max_inv = self.risk_config.max_inventory_usd
        position_size = self.state.position.size
        inventory_ratio = self.state.inventory_usd / max_inv
        
        # Direction: positive if long (want to sell more), negative if short (want to buy more)
        direction = 1 if position_size > 0 else -1
        
        # Scale by inventory ratio and skew factor
        skew = direction * inventory_ratio * self.risk_config.inventory_skew_factor
        
        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, skew))
    
    def should_requote(self, current_mid: float) -> bool:
        """Check if quotes should be refreshed due to price movement."""
        if len(self.state.last_mid_prices) < 2:
            return True
        
        _, last_mid = self.state.last_mid_prices[-1]
        if last_mid == 0:
            return True
        
        move_bps = abs(current_mid - last_mid) / last_mid * 10000
        return move_bps >= self.config.strategy.mid_move_requote_bps
    
    def get_volatility_bps(self) -> float:
        """Calculate recent volatility in bps."""
        if len(self.state.last_mid_prices) < 2:
            return 0.0
        
        prices = [p for _, p in self.state.last_mid_prices]
        mean_price = sum(prices) / len(prices)
        
        if mean_price == 0:
            return 0.0
        
        variance = sum((p - mean_price) ** 2 for p in prices) / len(prices)
        std_dev = variance ** 0.5
        
        return (std_dev / mean_price) * 10000
    
    def can_place_order(self, side: str, notional_usd: float) -> tuple[bool, str]:
        """
        Check if a new order can be placed.
        
        Args:
            side: "BUY" or "SELL"
            notional_usd: Order notional in USD
        
        Returns:
            (can_place, reason if cannot)
        """
        if self.state.state != BotState.RUNNING:
            return False, f"Bot is {self.state.state.value}"

        # Check if order would exceed inventory limits
        current_inv = self.state.inventory_usd
        position_size = self.state.position.size

        # Determine if this order would reduce position (close/reduce inventory)
        is_reducing = (side == "BUY" and position_size < 0) or (side == "SELL" and position_size > 0)

        # Estimate new inventory after fill
        if is_reducing:
            new_inv = max(0, current_inv - notional_usd)
        else:
            new_inv = current_inv + notional_usd

        # Always allow orders that reduce position (even if over limits)
        if is_reducing:
            return True, ""

        # DON'T add to a losing position
        # If we have unrealized loss and this order would INCREASE the position, block it
        if self.state.unrealized_pnl < 0 and position_size != 0:
            # We're in a losing position, don't add to it
            return False, f"Won't add to losing position (unrealized PnL: ${self.state.unrealized_pnl:.2f})"

        # Only block orders that would increase position beyond max_position_notional
        # (which is balance * leverage, allowing leveraged positions)
        if new_inv > self.risk_config.max_position_notional:
            return False, f"Would exceed max position: ${new_inv:.2f} > ${self.risk_config.max_position_notional:.2f}"

        return True, ""
    
    def record_error(self, error: str):
        """Record an error for rate limiting."""
        self._error_timestamps.append(time.time())
        self._error_count += 1
        logger.error(f"Error recorded ({len(self._error_timestamps)} in window): {error}")
    
    def _calculate_recent_move_bps(self, window_sec: float) -> float:
        """Calculate max price move in the given window."""
        if len(self.state.last_mid_prices) < 2:
            return 0.0
        
        now = time.time()
        recent_prices = [
            p for t, p in self.state.last_mid_prices 
            if now - t <= window_sec
        ]
        
        if len(recent_prices) < 2:
            return 0.0
        
        min_price = min(recent_prices)
        max_price = max(recent_prices)
        mid = (min_price + max_price) / 2
        
        if mid == 0:
            return 0.0
        
        return (max_price - min_price) / mid * 10000
    
    def _count_negative_fills(self) -> int:
        """Count recent fills with estimated negative PnL."""
        # Simplified: count taker fills in recent window
        now = time.time()
        window = self.risk_config.max_negative_fills_window
        
        recent = [
            f for f in self.state.recent_fills 
            if now - f.timestamp <= 60 and not f.is_maker
        ]
        
        return len(recent)
    
    def get_risk_summary(self) -> dict:
        """Get summary of current risk state."""
        return {
            "state": self.state.state.value,
            "position_size": self.state.position.size,
            "inventory_usd": self.state.inventory_usd,
            "inventory_pct": (self.state.inventory_usd / self.risk_config.max_inventory_usd * 100) if self.risk_config.max_inventory_usd > 0 else 0,
            "unrealized_pnl": self.state.unrealized_pnl,
            "realized_pnl": self.state.realized_pnl,
            "total_pnl": self.state.total_pnl,
            "session_drawdown_usd": self.state.session_drawdown_usd,
            "session_drawdown_pct": self.state.session_drawdown_pct,
            "total_fills": self.state.total_fills,
            "maker_ratio": (self.state.maker_fills / self.state.total_fills * 100) if self.state.total_fills > 0 else 0,
            "fees_paid": self.state.total_fees_paid,
            "volatility_bps": self.get_volatility_bps(),
            "inventory_skew": self.calculate_inventory_skew(),
        }
