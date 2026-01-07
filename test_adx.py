"""
Test script to verify ADX calculation and API candle data.
"""
import asyncio
import aiohttp
from src.indicators import TechnicalIndicators

async def fetch_candles(market: str = "ETH-USD", interval: str = "15m", limit: int = 250):
    """Fetch candles from Extended Exchange API."""
    base_url = "https://api.starknet.extended.exchange/api/v1"
    url = f"{base_url}/info/candles/{market}/trades?interval={interval}&limit={limit}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                result = await response.json()
                if result.get("status") == "OK" and result.get("data"):
                    return result["data"]
    return []

async def main():
    print("=" * 60)
    print("Testing ADX Calculation")
    print("=" * 60)

    # Fetch candles
    print("\n1. Fetching candles from Extended Exchange API...")
    candles = await fetch_candles()

    if not candles:
        print("ERROR: No candles returned from API")
        return

    print(f"   Received {len(candles)} candles")

    # Show sample candle structure
    print("\n2. Sample candle structure (first candle):")
    sample = candles[0]
    for key, value in sample.items():
        print(f"   {key}: {value}")

    # Reverse candles (API returns newest first)
    reversed_candles = list(reversed(candles))

    # Extract OHLC data
    print("\n3. Extracting OHLC data...")
    ohlc = {
        "open": [float(c["o"]) for c in reversed_candles],
        "high": [float(c["h"]) for c in reversed_candles],
        "low": [float(c["l"]) for c in reversed_candles],
        "close": [float(c["c"]) for c in reversed_candles],
    }

    print(f"   Open prices: {len(ohlc['open'])} values")
    print(f"   High prices: {len(ohlc['high'])} values")
    print(f"   Low prices: {len(ohlc['low'])} values")
    print(f"   Close prices: {len(ohlc['close'])} values")

    # Show last 5 candles
    print("\n4. Last 5 candles (oldest to newest):")
    print("   Open      High      Low       Close")
    for i in range(-5, 0):
        print(f"   {ohlc['open'][i]:<9.2f} {ohlc['high'][i]:<9.2f} {ohlc['low'][i]:<9.2f} {ohlc['close'][i]:<9.2f}")

    # Calculate ADX
    print("\n5. Calculating ADX...")
    indicators = TechnicalIndicators()

    adx_result = indicators.calculate_adx(
        high=ohlc["high"],
        low=ohlc["low"],
        close=ohlc["close"],
        period=14
    )

    if adx_result is None:
        print("   ERROR: ADX calculation returned None (not enough data)")
        return

    adx, plus_di, minus_di = adx_result

    print(f"\n   ADX Results:")
    print(f"   -------------")
    print(f"   ADX:    {adx:.2f}")
    print(f"   +DI:    {plus_di:.2f}")
    print(f"   -DI:    {minus_di:.2f}")

    # Interpret ADX
    print(f"\n6. ADX Interpretation:")
    if adx < 20:
        print(f"   ADX {adx:.1f} < 20: WEAK TREND / RANGING MARKET")
        print(f"   -> Entries would be FILTERED (blocked)")
    elif adx < 25:
        print(f"   ADX {adx:.1f} (20-25): TREND FORMING")
        print(f"   -> Entries ALLOWED (trend developing)")
    elif adx < 50:
        print(f"   ADX {adx:.1f} (25-50): STRONG TREND")
        print(f"   -> Entries ALLOWED (good conditions)")
    else:
        print(f"   ADX {adx:.1f} > 50: VERY STRONG TREND")
        print(f"   -> Entries ALLOWED (strong momentum)")

    # Trend direction from DI
    print(f"\n7. Trend Direction (from DI):")
    if plus_di > minus_di:
        print(f"   +DI ({plus_di:.1f}) > -DI ({minus_di:.1f}): BULLISH trend")
    else:
        print(f"   -DI ({minus_di:.1f}) > +DI ({plus_di:.1f}): BEARISH trend")

    # Test ADX filter
    print("\n8. Testing ADX Filter (min_strength=20):")

    # Test with LONG signal
    print("\n   a) Testing with LONG signal:")
    is_strong, adx_val, desc = indicators.get_adx_filter(
        high=ohlc["high"],
        low=ohlc["low"],
        close=ohlc["close"],
        min_strength=20.0,
        period=14,
        signal_direction="LONG",
        require_di_confirmation=True
    )
    print(f"      {desc}")
    print(f"      Would allow LONG: {is_strong}")

    # Test with SHORT signal
    print("\n   b) Testing with SHORT signal:")
    is_strong, adx_val, desc = indicators.get_adx_filter(
        high=ohlc["high"],
        low=ohlc["low"],
        close=ohlc["close"],
        min_strength=20.0,
        period=14,
        signal_direction="SHORT",
        require_di_confirmation=True
    )
    print(f"      {desc}")
    print(f"      Would allow SHORT: {is_strong}")

    # Also test other indicators
    print("\n9. Other Indicators (for reference):")

    rsi = indicators.calculate_rsi(ohlc["close"])
    print(f"   RSI(14): {rsi:.2f}" if rsi else "   RSI: N/A")

    macd = indicators.calculate_macd(ohlc["close"])
    if macd:
        macd_line, signal_line, histogram = macd
        print(f"   MACD: Line={macd_line:.4f}, Signal={signal_line:.4f}, Hist={histogram:.4f}")

    bb = indicators.calculate_bollinger_bands(ohlc["close"])
    if bb:
        upper, middle, lower, pct_b = bb
        print(f"   Bollinger: Upper={upper:.2f}, Middle={middle:.2f}, Lower={lower:.2f}, %B={pct_b:.2f}")

    ema = indicators.calculate_ema(ohlc["close"], 200)
    if ema:
        current_price = ohlc["close"][-1]
        print(f"   EMA(200): {ema:.2f} (Price: {current_price:.2f}, {'above' if current_price > ema else 'below'} EMA)")

    print("\n" + "=" * 60)
    print("Test completed!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(main())
