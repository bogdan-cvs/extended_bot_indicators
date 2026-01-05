import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

async def check():
    import aiohttp
    api_key = os.getenv('EXTENDED_API_KEY')
    headers = {'X-Api-Key': api_key, 'Content-Type': 'application/json'}

    async with aiohttp.ClientSession() as session:
        url = 'https://api.starknet.sepolia.extended.exchange/api/v1/user/positions'
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            print(f"Raw response: {data}")
            print(f"Type of data: {type(data)}")
            if isinstance(data, dict):
                print(f"Keys: {data.keys()}")
                if 'data' in data:
                    print(f"Type of data['data']: {type(data['data'])}")
                    print(f"data['data']: {data['data']}")

asyncio.run(check())
