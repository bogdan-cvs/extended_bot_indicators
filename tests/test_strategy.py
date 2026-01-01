"""
Unit tests for market making strategy components.
Tests spread calculation, inventory skew, sizing, and pause logic.
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, AsyncMock
from dataclasses import dataclass
from typing import Optional


# ============================================================================
# Test Fixtures and Mocks
# ============================================================================

@dataclass
class MockStrategyConfig:
    """Mock strategy configuration for testing."""
    spread_min_bps: float = 20.0
    spread_max_bps: float = 150.0
    volatility_spread_multiplier: float = 2.0
    inventory_skew_factor: float = 0.5
    order_notional_usd: float = 10.0
    max_open_orders_per_side: int = 1
    refresh_sec: float = 5.0
    mid_move_requote_bps: float = 15.0
    pause_move_bps: float = 60.0
    pause_window_sec: float = 15.0
    cooldown_after_pause_sec: float = 45.0
    post_only: bool = True


@dataclass
class MockRiskConfig:
    """Mock risk configuration for testing."""
    max_inventory_usd: float = 50.0
    max_position_notional: float = 100.0
    drawdown_limit_usd: float = 20.0
    session_drawdown_pct: float = 10.0


@dataclass
class MockMarketInfo:
    """Mock market information."""
    tick_size: Decimal = Decimal("0.01")
    step_size: Decimal = Decimal("0.0001")
    min_order_size: Decimal = Decimal("0.001")
    min_notional: Decimal = Decimal("1.0")


# ============================================================================
# Spread Calculation Tests
# ============================================================================

class TestSpreadCalculation:
    """Tests for dynamic spread calculation."""
    
    def test_base_spread_no_volatility(self):
        """Test spread equals minimum when volatility is zero."""
        config = MockStrategyConfig(spread_min_bps=20.0, spread_max_bps=150.0)
        volatility_bps = 0.0
        
        spread = calculate_dynamic_spread(
            base_spread_bps=config.spread_min_bps,
            volatility_bps=volatility_bps,
            multiplier=config.volatility_spread_multiplier,
            max_spread_bps=config.spread_max_bps
        )
        
        assert spread == config.spread_min_bps
    
    def test_spread_increases_with_volatility(self):
        """Test spread increases proportionally with volatility."""
        config = MockStrategyConfig(spread_min_bps=20.0, spread_max_bps=150.0)
        volatility_bps = 30.0
        
        spread = calculate_dynamic_spread(
            base_spread_bps=config.spread_min_bps,
            volatility_bps=volatility_bps,
            multiplier=config.volatility_spread_multiplier,
            max_spread_bps=config.spread_max_bps
        )
        
        expected = 20.0 + (30.0 * 2.0)  # base + vol * multiplier
        assert spread == expected
    
    def test_spread_capped_at_maximum(self):
        """Test spread is capped at maximum."""
        config = MockStrategyConfig(spread_min_bps=20.0, spread_max_bps=150.0)
        volatility_bps = 100.0  # Would give 220 bps without cap
        
        spread = calculate_dynamic_spread(
            base_spread_bps=config.spread_min_bps,
            volatility_bps=volatility_bps,
            multiplier=config.volatility_spread_multiplier,
            max_spread_bps=config.spread_max_bps
        )
        
        assert spread == config.spread_max_bps
    
    def test_spread_never_negative(self):
        """Test spread never goes negative even with bad inputs."""
        spread = calculate_dynamic_spread(
            base_spread_bps=-10.0,  # Invalid negative
            volatility_bps=-5.0,    # Invalid negative
            multiplier=2.0,
            max_spread_bps=150.0
        )
        
        assert spread >= 0


def calculate_dynamic_spread(
    base_spread_bps: float,
    volatility_bps: float,
    multiplier: float,
    max_spread_bps: float
) -> float:
    """Calculate dynamic spread based on volatility."""
    volatility_component = max(0.0, volatility_bps) * multiplier
    raw_spread = max(0.0, base_spread_bps) + volatility_component
    return min(raw_spread, max_spread_bps)


# ============================================================================
# Inventory Skew Tests
# ============================================================================

class TestInventorySkew:
    """Tests for inventory skew calculation."""
    
    def test_neutral_inventory_no_skew(self):
        """Test zero inventory produces zero skew."""
        skew = calculate_inventory_skew(
            current_inventory_usd=0.0,
            max_inventory_usd=50.0,
            skew_factor=0.5
        )
        
        assert skew == 0.0
    
    def test_long_inventory_negative_skew(self):
        """Test positive inventory produces negative skew (discourage more long)."""
        skew = calculate_inventory_skew(
            current_inventory_usd=25.0,  # 50% of max
            max_inventory_usd=50.0,
            skew_factor=0.5
        )
        
        # Should be negative to lower bid and raise ask
        assert skew < 0
        assert skew == pytest.approx(-0.25, rel=0.01)  # 0.5 * 0.5
    
    def test_short_inventory_positive_skew(self):
        """Test negative inventory produces positive skew (discourage more short)."""
        skew = calculate_inventory_skew(
            current_inventory_usd=-25.0,  # -50% of max
            max_inventory_usd=50.0,
            skew_factor=0.5
        )
        
        # Should be positive to raise bid and lower ask
        assert skew > 0
        assert skew == pytest.approx(0.25, rel=0.01)
    
    def test_skew_clamped_at_extremes(self):
        """Test skew is clamped between -1 and 1."""
        # Extreme long
        skew_long = calculate_inventory_skew(
            current_inventory_usd=100.0,  # 200% of max
            max_inventory_usd=50.0,
            skew_factor=0.5
        )
        assert skew_long >= -1.0
        
        # Extreme short
        skew_short = calculate_inventory_skew(
            current_inventory_usd=-100.0,
            max_inventory_usd=50.0,
            skew_factor=0.5
        )
        assert skew_short <= 1.0
    
    def test_skew_with_zero_max_inventory(self):
        """Test skew handles zero max inventory safely."""
        skew = calculate_inventory_skew(
            current_inventory_usd=10.0,
            max_inventory_usd=0.0,  # Edge case
            skew_factor=0.5
        )
        
        # Should clamp to extreme
        assert skew in [-1.0, 0.0, 1.0]


def calculate_inventory_skew(
    current_inventory_usd: float,
    max_inventory_usd: float,
    skew_factor: float
) -> float:
    """Calculate inventory skew factor for quote adjustment."""
    if max_inventory_usd <= 0:
        return -1.0 if current_inventory_usd > 0 else (1.0 if current_inventory_usd < 0 else 0.0)
    
    inventory_ratio = current_inventory_usd / max_inventory_usd
    raw_skew = -inventory_ratio * skew_factor
    return max(-1.0, min(1.0, raw_skew))


# ============================================================================
# Order Sizing Tests
# ============================================================================

class TestOrderSizing:
    """Tests for order size calculation."""
    
    def test_basic_sizing(self):
        """Test basic order size calculation."""
        market = MockMarketInfo()
        
        size = calculate_order_size(
            notional_usd=10.0,
            price=Decimal("2000.0"),
            step_size=market.step_size,
            min_size=market.min_order_size
        )
        
        expected = Decimal("0.005")  # 10 / 2000 = 0.005
        assert size == expected
    
    def test_size_rounded_to_step(self):
        """Test size is rounded to step size."""
        size = calculate_order_size(
            notional_usd=10.0,
            price=Decimal("3333.33"),
            step_size=Decimal("0.001"),
            min_size=Decimal("0.001")
        )
        
        # 10 / 3333.33 = 0.003... should round to 0.003
        assert size % Decimal("0.001") == 0
    
    def test_size_at_least_minimum(self):
        """Test size is at least minimum order size."""
        size = calculate_order_size(
            notional_usd=0.1,  # Very small notional
            price=Decimal("2000.0"),
            step_size=Decimal("0.0001"),
            min_size=Decimal("0.001")
        )
        
        assert size >= Decimal("0.001")
    
    def test_size_zero_for_too_small_notional(self):
        """Test returns zero if notional too small even for min size."""
        size = calculate_order_size(
            notional_usd=0.0001,  # Extremely small
            price=Decimal("2000.0"),
            step_size=Decimal("0.0001"),
            min_size=Decimal("0.001"),
            max_notional_pct=1.5  # Allow 150% of target
        )
        
        # If min_size * price > notional * max_pct, return 0
        min_notional = Decimal("0.001") * Decimal("2000.0")  # $2
        if float(min_notional) > 0.0001 * 1.5:
            assert size == Decimal("0")


def calculate_order_size(
    notional_usd: float,
    price: Decimal,
    step_size: Decimal,
    min_size: Decimal,
    max_notional_pct: float = 1.5
) -> Decimal:
    """Calculate order size from notional USD."""
    if price <= 0:
        return Decimal("0")
    
    raw_size = Decimal(str(notional_usd)) / price
    
    # Round down to step size
    steps = raw_size / step_size
    rounded_size = int(steps) * step_size
    
    # Apply minimum
    if rounded_size < min_size:
        # Check if min_size is acceptable
        min_notional = min_size * price
        if float(min_notional) <= notional_usd * max_notional_pct:
            return min_size
        return Decimal("0")
    
    return rounded_size


# ============================================================================
# Pause Logic Tests
# ============================================================================

class TestPauseLogic:
    """Tests for volatility-based pause logic."""
    
    def test_no_pause_stable_market(self):
        """Test no pause when market is stable."""
        config = MockStrategyConfig(pause_move_bps=60.0, pause_window_sec=15.0)
        
        should_pause = check_pause_condition(
            price_changes_bps=[5.0, 3.0, 4.0, 2.0],  # Small moves
            window_sec=15.0,
            threshold_bps=config.pause_move_bps
        )
        
        assert should_pause is False
    
    def test_pause_on_large_move(self):
        """Test pause triggered on large price move."""
        config = MockStrategyConfig(pause_move_bps=60.0, pause_window_sec=15.0)
        
        should_pause = check_pause_condition(
            price_changes_bps=[10.0, 15.0, 40.0],  # Total 65 bps
            window_sec=15.0,
            threshold_bps=config.pause_move_bps
        )
        
        assert should_pause is True
    
    def test_pause_on_single_spike(self):
        """Test pause on single large spike."""
        should_pause = check_pause_condition(
            price_changes_bps=[70.0],  # Single big move
            window_sec=15.0,
            threshold_bps=60.0
        )
        
        assert should_pause is True
    
    def test_no_pause_empty_history(self):
        """Test no pause with empty price history."""
        should_pause = check_pause_condition(
            price_changes_bps=[],
            window_sec=15.0,
            threshold_bps=60.0
        )
        
        assert should_pause is False
    
    def test_cooldown_respected(self):
        """Test cooldown period is respected after pause."""
        cooldown_sec = 45.0
        pause_ended_ago = 30.0  # Only 30 sec since pause ended
        
        can_resume = check_can_resume(
            seconds_since_pause_end=pause_ended_ago,
            cooldown_sec=cooldown_sec
        )
        
        assert can_resume is False
    
    def test_resume_after_cooldown(self):
        """Test can resume after cooldown expires."""
        cooldown_sec = 45.0
        pause_ended_ago = 50.0  # 50 sec since pause ended
        
        can_resume = check_can_resume(
            seconds_since_pause_end=pause_ended_ago,
            cooldown_sec=cooldown_sec
        )
        
        assert can_resume is True


def check_pause_condition(
    price_changes_bps: list[float],
    window_sec: float,
    threshold_bps: float
) -> bool:
    """Check if pause condition is met."""
    if not price_changes_bps:
        return False
    
    total_move = sum(abs(x) for x in price_changes_bps)
    return total_move >= threshold_bps


def check_can_resume(
    seconds_since_pause_end: float,
    cooldown_sec: float
) -> bool:
    """Check if cooldown has expired and bot can resume."""
    return seconds_since_pause_end >= cooldown_sec


# ============================================================================
# Kill Switch Tests
# ============================================================================

class TestKillSwitch:
    """Tests for kill switch logic."""
    
    def test_no_kill_within_limits(self):
        """Test no kill when within drawdown limits."""
        config = MockRiskConfig(drawdown_limit_usd=20.0, session_drawdown_pct=10.0)
        
        should_kill = check_kill_switch(
            current_pnl_usd=-15.0,  # $15 loss
            starting_balance_usd=200.0,
            drawdown_limit_usd=config.drawdown_limit_usd,
            session_drawdown_pct=config.session_drawdown_pct
        )
        
        assert should_kill is False
    
    def test_kill_on_usd_drawdown(self):
        """Test kill triggered on USD drawdown limit."""
        should_kill = check_kill_switch(
            current_pnl_usd=-25.0,  # $25 loss > $20 limit
            starting_balance_usd=200.0,
            drawdown_limit_usd=20.0,
            session_drawdown_pct=10.0
        )
        
        assert should_kill is True
    
    def test_kill_on_pct_drawdown(self):
        """Test kill triggered on percentage drawdown."""
        should_kill = check_kill_switch(
            current_pnl_usd=-22.0,  # 11% loss > 10% limit
            starting_balance_usd=200.0,
            drawdown_limit_usd=50.0,  # USD limit not hit
            session_drawdown_pct=10.0
        )
        
        assert should_kill is True
    
    def test_no_kill_on_profit(self):
        """Test no kill when in profit."""
        should_kill = check_kill_switch(
            current_pnl_usd=50.0,  # In profit
            starting_balance_usd=200.0,
            drawdown_limit_usd=20.0,
            session_drawdown_pct=10.0
        )
        
        assert should_kill is False


def check_kill_switch(
    current_pnl_usd: float,
    starting_balance_usd: float,
    drawdown_limit_usd: float,
    session_drawdown_pct: float
) -> bool:
    """Check if kill switch should be triggered."""
    if current_pnl_usd >= 0:
        return False
    
    loss = abs(current_pnl_usd)
    
    # Check USD limit
    if loss >= drawdown_limit_usd:
        return True
    
    # Check percentage limit
    if starting_balance_usd > 0:
        loss_pct = (loss / starting_balance_usd) * 100
        if loss_pct >= session_drawdown_pct:
            return True
    
    return False


# ============================================================================
# Anti-Self-Trade Tests
# ============================================================================

class TestAntiSelfTrade:
    """Tests for anti-self-trade logic."""
    
    def test_no_cross_normal_quotes(self):
        """Test normal quotes don't cross."""
        mid_price = Decimal("2000.0")
        spread_bps = 20.0
        
        bid, ask = calculate_quotes(mid_price, spread_bps)
        
        assert bid < ask
        assert not would_self_trade(bid, ask, [], [])
    
    def test_detect_crossing_quotes(self):
        """Test detection of crossing quotes."""
        bid = Decimal("2001.0")  # Bid above ask
        ask = Decimal("2000.0")
        
        crosses = bid >= ask
        assert crosses is True
    
    def test_detect_self_trade_with_existing_orders(self):
        """Test detection of potential self-trade with existing orders."""
        new_bid = Decimal("2005.0")
        existing_asks = [Decimal("2003.0"), Decimal("2004.0")]
        
        would_trade = would_self_trade(
            new_bid=new_bid,
            new_ask=None,
            existing_bids=[],
            existing_asks=existing_asks
        )
        
        assert would_trade is True
    
    def test_safe_quotes_no_self_trade(self):
        """Test safe quotes don't trigger self-trade."""
        new_bid = Decimal("1999.0")
        new_ask = Decimal("2001.0")
        existing_bids = [Decimal("1998.0")]
        existing_asks = [Decimal("2002.0")]
        
        would_trade = would_self_trade(
            new_bid=new_bid,
            new_ask=new_ask,
            existing_bids=existing_bids,
            existing_asks=existing_asks
        )
        
        assert would_trade is False


def calculate_quotes(mid_price: Decimal, spread_bps: float) -> tuple[Decimal, Decimal]:
    """Calculate bid and ask from mid price and spread."""
    half_spread = mid_price * Decimal(str(spread_bps / 10000 / 2))
    bid = mid_price - half_spread
    ask = mid_price + half_spread
    return bid, ask


def would_self_trade(
    new_bid: Optional[Decimal],
    new_ask: Optional[Decimal],
    existing_bids: list[Decimal],
    existing_asks: list[Decimal]
) -> bool:
    """Check if new orders would self-trade with existing orders."""
    # Check if new bid would trade against existing asks
    if new_bid is not None:
        for ask in existing_asks:
            if new_bid >= ask:
                return True
    
    # Check if new ask would trade against existing bids
    if new_ask is not None:
        for bid in existing_bids:
            if new_ask <= bid:
                return True
    
    return False


# ============================================================================
# Price Rounding Tests
# ============================================================================

class TestPriceRounding:
    """Tests for price tick rounding."""
    
    def test_round_to_tick(self):
        """Test price rounds to tick size."""
        price = Decimal("2000.123")
        tick_size = Decimal("0.01")
        
        rounded = round_to_tick(price, tick_size)
        
        assert rounded == Decimal("2000.12")
    
    def test_round_bid_down(self):
        """Test bid rounds down."""
        price = Decimal("2000.129")
        tick_size = Decimal("0.01")
        
        rounded = round_to_tick(price, tick_size, round_down=True)
        
        assert rounded == Decimal("2000.12")
    
    def test_round_ask_up(self):
        """Test ask rounds up."""
        price = Decimal("2000.121")
        tick_size = Decimal("0.01")
        
        rounded = round_to_tick(price, tick_size, round_down=False)
        
        assert rounded == Decimal("2000.13")


def round_to_tick(
    price: Decimal,
    tick_size: Decimal,
    round_down: bool = True
) -> Decimal:
    """Round price to tick size."""
    ticks = price / tick_size
    if round_down:
        rounded_ticks = int(ticks)
    else:
        import math
        rounded_ticks = math.ceil(float(ticks))
    return Decimal(str(rounded_ticks)) * tick_size


# ============================================================================
# Run Tests
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
