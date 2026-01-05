"""Test placing a simple order now"""
import asyncio
import os
from decimal import Decimal
from dotenv import load_dotenv
load_dotenv()

async def test():
    import aiohttp
    import json
    from src.signing import create_order_builder

    api_key = os.getenv('EXTENDED_API_KEY')
    private_key = os.getenv('EXTENDED_STARK_PRIVATE_KEY')
    vault_id = int(os.getenv('EXTENDED_VAULT_ID', '0'))

    builder = create_order_builder(private_key, vault_id, is_testnet=True)

    url = 'https://api.starknet.sepolia.extended.exchange/api/v1/user/order'
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    # Test with exact values the bot is using (with skew applied)
    # Bot log shows: BID 0.12 @ 160.24 | ASK 0.12 @ 160.73
    test_cases = [
        ("BUY", Decimal("0.12"), Decimal("160.24"), True),   # Bot's BID
        ("SELL", Decimal("0.12"), Decimal("160.73"), False), # Bot's ASK (postOnly=False for SELL)
        ("BUY", Decimal("0.12"), Decimal("155.00"), True),   # Known working
    ]

    async with aiohttp.ClientSession() as session:
        for side, size, price, post_only in test_cases:
            print(f"\n{'='*60}")
            print(f"Testing: {side} {size} @ {price}, postOnly={post_only}")
            print('='*60)

            order = builder.build_limit_order(
                market="AAVE-USD",
                side=side,
                size=size,
                price=price,
                post_only=post_only,
            )

            print(f"Order qty: {order['qty']}, price: {order['price']}")

            async with session.post(url, headers=headers, json=order) as resp:
                status = resp.status
                data = await resp.json()
                print(f"Response (status {status}): {json.dumps(data)}")

asyncio.run(test())
