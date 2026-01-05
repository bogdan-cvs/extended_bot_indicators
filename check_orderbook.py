import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

async def check():
    import aiohttp
    api_key = os.getenv('EXTENDED_API_KEY')
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        url = 'https://api.starknet.sepolia.extended.exchange/api/v1/markets/AAVE-USD/orderbook'
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            print(f"Orderbook: {data}")

asyncio.run(check())
