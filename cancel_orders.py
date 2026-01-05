import asyncio
from src.config import create_config, Environment
from src.api_client import ExtendedAPIClient

async def main():
    config = create_config("mainnet", "MAINNET_SAFE")
    async with ExtendedAPIClient(config) as client:
        print("Cancelling all orders...")
        r = await client.cancel_all_orders("ETH-USD")
        print(f"Cancel result: {r.data}")

        print("\nGetting positions...")
        p = await client.get_positions()
        print(f"Positions: {p.data}")

        print("\nGetting balance...")
        b = await client.get_balance()
        print(f"Balance: {b.data}")

if __name__ == "__main__":
    asyncio.run(main())
