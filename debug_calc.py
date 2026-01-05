"""Debug what the bot is calculating for order sizes and prices"""
import asyncio
import os
from decimal import Decimal
from dotenv import load_dotenv
load_dotenv()

async def debug():
    import aiohttp
    import json
    api_key = os.getenv('EXTENDED_API_KEY')
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        # Get orderbook
        url = 'https://api.starknet.sepolia.extended.exchange/api/v1/info/markets/AAVE-USD/orderbook?depth=10'
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            print("Orderbook:")
            print(json.dumps(data, indent=2))

        # Simulate bot calculations
        # From config
        order_notional = 20.0  # $20
        spread_bps = 100  # 100 bps = 1%
        tick_size = 0.0001  # default from strategy
        size_precision = 6  # default from strategy

        # Assume mid price around 160
        mid_price = 160.0
        half_spread = (spread_bps / 10000) * mid_price / 2
        print(f"\nmid_price: {mid_price}")
        print(f"half_spread: {half_spread}")

        # Simulate with skew = -0.80 (short position)
        skew = -0.80
        skew_offset = skew * half_spread * 0.5
        print(f"skew: {skew}")
        print(f"skew_offset: {skew_offset}")

        bid_price = mid_price - half_spread - skew_offset
        ask_price = mid_price + half_spread - skew_offset
        print(f"raw bid: {bid_price}")
        print(f"raw ask: {ask_price}")

        # Round to tick
        def round_to_tick(price, down=True):
            ticks = price / tick_size
            if down:
                ticks = int(ticks)
            else:
                ticks = int(ticks) + 1 if ticks != int(ticks) else int(ticks)
            return ticks * tick_size

        bid_rounded = round_to_tick(bid_price, down=True)
        ask_rounded = round_to_tick(ask_price, down=False)
        print(f"bid_rounded: {bid_rounded}")
        print(f"ask_rounded: {ask_rounded}")

        # Calculate sizes
        bid_size = order_notional / bid_rounded
        ask_size = order_notional / ask_rounded
        bid_size_rounded = round(bid_size, size_precision)
        ask_size_rounded = round(ask_size, size_precision)
        print(f"bid_size: {bid_size_rounded}")
        print(f"ask_size: {ask_size_rounded}")

        print("\n=== PROBLEM: asset precision is 2, not 6 ===")
        print(f"bid_size with 2 decimals: {round(bid_size, 2)}")
        print(f"ask_size with 2 decimals: {round(ask_size, 2)}")

asyncio.run(debug())
