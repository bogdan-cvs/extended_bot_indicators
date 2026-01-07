# =============================================================================
# Technical Indicators Module
# =============================================================================
# RSI, MACD, Bollinger Bands, EMA indicators for DCA bot entry signals
# =============================================================================

import numpy as np
from dataclasses import dataclass
from typing import List, Optional, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class Signal(Enum):
    """Trading signal types"""
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class IndicatorResult:
    """Result from an indicator calculation"""
    name: str
    signal: Signal
    value: float
    details: str


@dataclass
class CombinedSignal:
    """Combined signal from multiple indicators"""
    final_signal: Signal
    confidence: float  # 0.0 to 1.0
    indicators: List[IndicatorResult]
    long_count: int
    short_count: int
    neutral_count: int


class TechnicalIndicators:
    """
    Technical indicators calculator for trading signals.

    Supports:
    - RSI (Relative Strength Index)
    - MACD (Moving Average Convergence Divergence)
    - Bollinger Bands
    - EMA (Exponential Moving Average)
    """

    def __init__(
        self,
        # RSI settings
        rsi_period: int = 14,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        # MACD settings
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        # Bollinger settings
        bb_period: int = 20,
        bb_std_dev: float = 2.0,
        # EMA settings
        ema_period: int = 200,
    ):
        # RSI
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought

        # MACD
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal

        # Bollinger
        self.bb_period = bb_period
        self.bb_std_dev = bb_std_dev

        # EMA
        self.ema_period = ema_period

        logger.info(f"Indicators initialized: RSI({rsi_period}), MACD({macd_fast}/{macd_slow}/{macd_signal}), BB({bb_period}), EMA({ema_period})")

    # =========================================================================
    # RSI - Relative Strength Index
    # =========================================================================
    def calculate_rsi(self, prices: List[float]) -> Optional[float]:
        """
        Calculate RSI (Relative Strength Index).

        RSI = 100 - (100 / (1 + RS))
        RS = Average Gain / Average Loss

        Args:
            prices: List of closing prices (oldest to newest)

        Returns:
            RSI value (0-100) or None if not enough data
        """
        if len(prices) < self.rsi_period + 1:
            return None

        prices_arr = np.array(prices)
        deltas = np.diff(prices_arr)

        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        # Use exponential moving average for smoothing
        avg_gain = self._ema(gains, self.rsi_period)[-1]
        avg_loss = self._ema(losses, self.rsi_period)[-1]

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return float(rsi)

    def get_rsi_signal(self, prices: List[float]) -> IndicatorResult:
        """Get trading signal from RSI."""
        rsi = self.calculate_rsi(prices)

        if rsi is None:
            return IndicatorResult(
                name="RSI",
                signal=Signal.NEUTRAL,
                value=0.0,
                details="Not enough data"
            )

        if rsi < self.rsi_oversold:
            signal = Signal.LONG
            details = f"Oversold ({rsi:.1f} < {self.rsi_oversold})"
        elif rsi > self.rsi_overbought:
            signal = Signal.SHORT
            details = f"Overbought ({rsi:.1f} > {self.rsi_overbought})"
        else:
            signal = Signal.NEUTRAL
            details = f"Neutral ({rsi:.1f})"

        return IndicatorResult(name="RSI", signal=signal, value=rsi, details=details)

    # =========================================================================
    # MACD - Moving Average Convergence Divergence
    # =========================================================================
    def calculate_macd(self, prices: List[float]) -> Optional[Tuple[float, float, float]]:
        """
        Calculate MACD (Moving Average Convergence Divergence).

        MACD Line = EMA(fast) - EMA(slow)
        Signal Line = EMA(MACD Line)
        Histogram = MACD Line - Signal Line

        Args:
            prices: List of closing prices (oldest to newest)

        Returns:
            Tuple of (macd_line, signal_line, histogram) or None if not enough data
        """
        min_periods = self.macd_slow + self.macd_signal
        if len(prices) < min_periods:
            return None

        prices_arr = np.array(prices)

        ema_fast = self._ema(prices_arr, self.macd_fast)
        ema_slow = self._ema(prices_arr, self.macd_slow)

        macd_line = ema_fast - ema_slow
        signal_line = self._ema(macd_line, self.macd_signal)
        histogram = macd_line - signal_line

        return (float(macd_line[-1]), float(signal_line[-1]), float(histogram[-1]))

    def get_macd_signal(self, prices: List[float]) -> IndicatorResult:
        """Get trading signal from MACD."""
        result = self.calculate_macd(prices)

        if result is None:
            return IndicatorResult(
                name="MACD",
                signal=Signal.NEUTRAL,
                value=0.0,
                details="Not enough data"
            )

        macd_line, signal_line, histogram = result

        # Check for crossover
        prev_result = self.calculate_macd(prices[:-1])
        if prev_result is not None:
            _, prev_signal, prev_histogram = prev_result

            # Bullish crossover: MACD crosses above signal
            if prev_histogram < 0 and histogram > 0:
                return IndicatorResult(
                    name="MACD",
                    signal=Signal.LONG,
                    value=histogram,
                    details=f"Bullish crossover (hist: {histogram:.4f})"
                )

            # Bearish crossover: MACD crosses below signal
            if prev_histogram > 0 and histogram < 0:
                return IndicatorResult(
                    name="MACD",
                    signal=Signal.SHORT,
                    value=histogram,
                    details=f"Bearish crossover (hist: {histogram:.4f})"
                )

        # No crossover, use histogram direction
        if histogram > 0:
            signal = Signal.LONG
            details = f"Bullish (hist: {histogram:.4f})"
        elif histogram < 0:
            signal = Signal.SHORT
            details = f"Bearish (hist: {histogram:.4f})"
        else:
            signal = Signal.NEUTRAL
            details = f"Neutral (hist: {histogram:.4f})"

        return IndicatorResult(name="MACD", signal=signal, value=histogram, details=details)

    # =========================================================================
    # Bollinger Bands
    # =========================================================================
    def calculate_bollinger_bands(self, prices: List[float]) -> Optional[Tuple[float, float, float, float]]:
        """
        Calculate Bollinger Bands.

        Middle Band = SMA(period)
        Upper Band = Middle + (std_dev * standard deviation)
        Lower Band = Middle - (std_dev * standard deviation)
        %B = (Price - Lower) / (Upper - Lower)

        Args:
            prices: List of closing prices (oldest to newest)

        Returns:
            Tuple of (upper, middle, lower, percent_b) or None if not enough data
        """
        if len(prices) < self.bb_period:
            return None

        prices_arr = np.array(prices[-self.bb_period:])

        middle = np.mean(prices_arr)
        std = np.std(prices_arr)

        upper = middle + (self.bb_std_dev * std)
        lower = middle - (self.bb_std_dev * std)

        current_price = prices[-1]

        # %B indicator: where is price relative to bands
        if upper != lower:
            percent_b = (current_price - lower) / (upper - lower)
        else:
            percent_b = 0.5

        return (float(upper), float(middle), float(lower), float(percent_b))

    def get_bollinger_signal(self, prices: List[float]) -> IndicatorResult:
        """Get trading signal from Bollinger Bands."""
        result = self.calculate_bollinger_bands(prices)

        if result is None:
            return IndicatorResult(
                name="Bollinger",
                signal=Signal.NEUTRAL,
                value=0.0,
                details="Not enough data"
            )

        upper, middle, lower, percent_b = result
        current_price = prices[-1]

        # Price at or below lower band = oversold (LONG signal)
        if current_price <= lower or percent_b <= 0.0:
            return IndicatorResult(
                name="Bollinger",
                signal=Signal.LONG,
                value=percent_b,
                details=f"Price at lower band (%B: {percent_b:.2f})"
            )

        # Price at or above upper band = overbought (SHORT signal)
        if current_price >= upper or percent_b >= 1.0:
            return IndicatorResult(
                name="Bollinger",
                signal=Signal.SHORT,
                value=percent_b,
                details=f"Price at upper band (%B: {percent_b:.2f})"
            )

        # Price in middle zone
        return IndicatorResult(
            name="Bollinger",
            signal=Signal.NEUTRAL,
            value=percent_b,
            details=f"Price in middle (%B: {percent_b:.2f})"
        )

    # =========================================================================
    # EMA - Exponential Moving Average
    # =========================================================================
    def calculate_ema(self, prices: List[float], period: Optional[int] = None) -> Optional[float]:
        """
        Calculate EMA (Exponential Moving Average).

        Args:
            prices: List of closing prices (oldest to newest)
            period: EMA period (default: self.ema_period)

        Returns:
            EMA value or None if not enough data
        """
        period = period or self.ema_period
        if len(prices) < period:
            return None

        prices_arr = np.array(prices)
        ema = self._ema(prices_arr, period)

        return float(ema[-1])

    def get_ema_signal(self, prices: List[float]) -> IndicatorResult:
        """Get trading signal from EMA (price vs EMA200)."""
        ema = self.calculate_ema(prices)

        if ema is None:
            return IndicatorResult(
                name="EMA",
                signal=Signal.NEUTRAL,
                value=0.0,
                details="Not enough data"
            )

        current_price = prices[-1]
        diff_pct = ((current_price - ema) / ema) * 100

        # Price above EMA = uptrend (LONG signal)
        if current_price > ema:
            return IndicatorResult(
                name="EMA",
                signal=Signal.LONG,
                value=diff_pct,
                details=f"Price above EMA{self.ema_period} ({diff_pct:+.2f}%)"
            )

        # Price below EMA = downtrend (SHORT signal)
        if current_price < ema:
            return IndicatorResult(
                name="EMA",
                signal=Signal.SHORT,
                value=diff_pct,
                details=f"Price below EMA{self.ema_period} ({diff_pct:+.2f}%)"
            )

        return IndicatorResult(
            name="EMA",
            signal=Signal.NEUTRAL,
            value=diff_pct,
            details=f"Price at EMA{self.ema_period}"
        )

    # =========================================================================
    # Combined Signal
    # =========================================================================
    def get_combined_signal(
        self,
        prices: List[float],
        use_rsi: bool = True,
        use_macd: bool = True,
        use_bollinger: bool = True,
        use_ema: bool = True,
        min_confirmations: int = 2,
    ) -> CombinedSignal:
        """
        Get combined signal from multiple indicators.

        Args:
            prices: List of closing prices (oldest to newest)
            use_rsi: Include RSI in analysis
            use_macd: Include MACD in analysis
            use_bollinger: Include Bollinger Bands in analysis
            use_ema: Include EMA in analysis
            min_confirmations: Minimum indicators that must agree for a signal

        Returns:
            CombinedSignal with final decision
        """
        indicators = []

        if use_rsi:
            indicators.append(self.get_rsi_signal(prices))

        if use_macd:
            indicators.append(self.get_macd_signal(prices))

        if use_bollinger:
            indicators.append(self.get_bollinger_signal(prices))

        if use_ema:
            indicators.append(self.get_ema_signal(prices))

        # Count signals
        long_count = sum(1 for i in indicators if i.signal == Signal.LONG)
        short_count = sum(1 for i in indicators if i.signal == Signal.SHORT)
        neutral_count = sum(1 for i in indicators if i.signal == Signal.NEUTRAL)

        total = len(indicators)

        # Determine final signal
        if long_count >= min_confirmations and long_count > short_count:
            final_signal = Signal.LONG
            confidence = long_count / total
        elif short_count >= min_confirmations and short_count > long_count:
            final_signal = Signal.SHORT
            confidence = short_count / total
        else:
            final_signal = Signal.NEUTRAL
            confidence = neutral_count / total if total > 0 else 0.0

        return CombinedSignal(
            final_signal=final_signal,
            confidence=confidence,
            indicators=indicators,
            long_count=long_count,
            short_count=short_count,
            neutral_count=neutral_count,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================
    def _ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Calculate Exponential Moving Average."""
        alpha = 2 / (period + 1)
        ema = np.zeros_like(data, dtype=float)
        ema[0] = data[0]

        for i in range(1, len(data)):
            ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]

        return ema

    def _sma(self, data: np.ndarray, period: int) -> np.ndarray:
        """Calculate Simple Moving Average."""
        sma = np.convolve(data, np.ones(period) / period, mode='valid')
        # Pad with NaN at the beginning
        return np.concatenate([np.full(period - 1, np.nan), sma])


# =============================================================================
# Convenience function for quick signal check
# =============================================================================
def get_entry_signal(
    prices: List[float],
    direction: str = "LONG",
    strategy: str = "permissive",  # "strict" or "permissive"
    rsi_oversold: float = 35.0,
    rsi_overbought: float = 65.0,
) -> Tuple[bool, str, CombinedSignal]:
    """
    Quick check if entry conditions are met.

    Args:
        prices: List of closing prices
        direction: "LONG" or "SHORT"
        strategy: "strict" (4/4 indicators) or "permissive" (2/4 indicators)
        rsi_oversold: RSI oversold threshold (default 35 for permissive)
        rsi_overbought: RSI overbought threshold (default 65 for permissive)

    Returns:
        Tuple of (should_enter, reason, combined_signal)
    """
    # Permissive uses relaxed RSI thresholds
    indicators = TechnicalIndicators(
        rsi_oversold=rsi_oversold,
        rsi_overbought=rsi_overbought,
    )

    min_confirmations = 4 if strategy == "strict" else 2

    signal = indicators.get_combined_signal(
        prices=prices,
        min_confirmations=min_confirmations,
    )

    target_signal = Signal.LONG if direction == "LONG" else Signal.SHORT

    if signal.final_signal == target_signal:
        reason = f"{signal.long_count if direction == 'LONG' else signal.short_count}/{len(signal.indicators)} indicators confirm {direction}"
        return True, reason, signal
    else:
        reason = f"Signal is {signal.final_signal.value}, need {direction}"
        return False, reason, signal
