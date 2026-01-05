import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

async def check():
    import aiohttp
    import json
    api_key = os.getenv('EXTENDED_API_KEY')
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        url = 'https://api.starknet.sepolia.extended.exchange/api/v1/info/markets?market=AAVE-USD'
        async with session.get(url, headers=headers) as resp:
            print(f"Status: {resp.status}")
            data = await resp.json()
            market = data['data'][0]

            # Print relevant fields
            print("\n=== Market Info ===")
            for key in ['name', 'assetPrecision', 'collateralAssetPrecision',
                        'tickSize', 'minOrderSize', 'stepSize', 'minSize',
                        'minNotional', 'maxLeverage', 'maintenanceMarginRate']:
                if key in market:
                    print(f"{key}: {market[key]}")

            # Look for any field with 'tick', 'size', 'step', 'min', 'precision'
            print("\n=== Relevant fields ===")
            for key, value in market.items():
                kl = key.lower()
                if any(x in kl for x in ['tick', 'size', 'step', 'min', 'max', 'precision', 'increment']):
                    print(f"{key}: {value}")

            # Print l2Config if present
            if 'l2Config' in market:
                print("\n=== L2 Config ===")
                print(json.dumps(market['l2Config'], indent=2))

asyncio.run(check())
