"""Close all positions"""
import asyncio
import os
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from dotenv import load_dotenv
import time
import math
import random
load_dotenv()

from fast_stark_crypto import get_order_msg_hash, sign, get_public_key

async def close_position():
    import aiohttp

    api_key = os.getenv('EXTENDED_API_KEY')
    private_key = os.getenv('EXTENDED_STARK_PRIVATE_KEY')
    vault_id = int(os.getenv('EXTENDED_VAULT_ID', '0'))

    if private_key.startswith("0x"):
        private_key = private_key[2:]
    priv_key_int = int(private_key, 16)
    pub_key = get_public_key(priv_key_int)

    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        # Get current position
        pos_url = 'https://api.starknet.sepolia.extended.exchange/api/v1/user/positions'
        async with session.get(pos_url, headers=headers) as resp:
            data = await resp.json()
            positions = data.get('data', [])

        if not positions:
            print("No positions to close")
            return

        for pos in positions:
            market = pos.get('market')
            side = pos.get('side')
            size = Decimal(pos.get('size', '0'))
            mark_price = Decimal(pos.get('markPrice', '160'))

            print(f"Position: {side} {size} {market} @ mark {mark_price}")

            # To close: if SHORT, we need to BUY; if LONG, we need to SELL
            close_side = "BUY" if side == "SHORT" else "SELL"

            # Use a price that will definitely fill (very aggressive)
            if close_side == "BUY":
                close_price = mark_price * Decimal("1.05")  # 5% above mark
            else:
                close_price = mark_price * Decimal("0.95")  # 5% below mark

            close_price = Decimal(str(round(float(close_price), 2)))
            size = Decimal(str(round(float(size), 2)))

            print(f"Closing with {close_side} {size} @ {close_price}")

            # Build order
            synthetic_id = "0x414156452d33000000000000000000"
            collateral_id = "0x31857064564ed0ff978e687456963cba09c2c6985d8f9300a1de4962fafa054"

            nonce = random.randint(0, 2**32 - 1)
            expiration_ts = int(time.time()) + 300  # 5 min expiry
            settlement_expiration = math.ceil(expiration_ts + 14 * 24 * 3600)

            is_buy = close_side == "BUY"
            rounding = ROUND_UP if is_buy else ROUND_DOWN

            synthetic_amount = int((size * Decimal(1000)).quantize(Decimal("1"), rounding=rounding))
            collateral_amount = int((size * close_price * Decimal(1000000)).quantize(Decimal("1"), rounding=rounding))
            fee_amount = int((size * close_price * Decimal("0.0005") * Decimal(1000000)).quantize(Decimal("1"), rounding=ROUND_UP))

            if is_buy:
                base_amount = synthetic_amount
                quote_amount = -collateral_amount
            else:
                base_amount = -synthetic_amount
                quote_amount = collateral_amount

            order_hash = get_order_msg_hash(
                position_id=vault_id,
                base_asset_id=int(synthetic_id, 16),
                base_amount=base_amount,
                quote_asset_id=int(collateral_id, 16),
                quote_amount=quote_amount,
                fee_amount=fee_amount,
                fee_asset_id=int(collateral_id, 16),
                expiration=settlement_expiration,
                salt=nonce,
                user_public_key=pub_key,
                domain_name="Perpetuals",
                domain_version="v0",
                domain_chain_id="SN_SEPOLIA",
                domain_revision="1",
            )

            r, s = sign(priv_key_int, order_hash)

            order_payload = {
                "id": str(order_hash),
                "market": market,
                "type": "LIMIT",
                "side": close_side,
                "qty": str(size),
                "price": str(close_price),
                "postOnly": False,
                "reduceOnly": True,  # Important: reduce only
                "timeInForce": "IOC",  # Immediate or cancel
                "expiryEpochMillis": expiration_ts * 1000,
                "fee": "0.0005",
                "nonce": str(nonce),
                "selfTradeProtectionLevel": "ACCOUNT",
                "settlement": {
                    "signature": {"r": hex(r), "s": hex(s)},
                    "starkKey": hex(pub_key),
                    "collateralPosition": str(vault_id),
                },
            }

            order_url = 'https://api.starknet.sepolia.extended.exchange/api/v1/user/order'
            async with session.post(order_url, headers=headers, json=order_payload) as resp:
                result = await resp.json()
                print(f"Close order result: {result}")

        # Wait and check position
        await asyncio.sleep(2)
        async with session.get(pos_url, headers=headers) as resp:
            data = await resp.json()
            positions = data.get('data', [])
            if positions:
                print(f"\nRemaining positions: {len(positions)}")
                for p in positions:
                    print(f"  {p.get('side')} {p.get('size')} {p.get('market')}")
            else:
                print("\nAll positions closed!")

asyncio.run(close_position())
