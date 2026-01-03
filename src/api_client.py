"""
REST API client for Extended Exchange.
Handles all HTTP requests with proper authentication and error handling.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional, Any
import logging
import aiohttp
from aiohttp import ClientTimeout

from .config import BotConfig, EndpointConfig

logger = logging.getLogger(__name__)


@dataclass
class APIResponse:
    """Wrapper for API responses."""
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None
    status_code: int = 0
    rate_limit_remaining: Optional[int] = None
    rate_limit_reset: Optional[int] = None


class RateLimitError(Exception):
    """Raised when rate limit is hit."""
    def __init__(self, retry_after: int = 1):
        self.retry_after = retry_after
        super().__init__(f"Rate limit hit. Retry after {retry_after}s")


class APIError(Exception):
    """General API error."""
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)


class ExtendedAPIClient:
    """
    Async REST API client for Extended Exchange.
    
    Features:
    - Automatic retries with exponential backoff
    - Rate limit handling
    - Request logging
    - Proper authentication headers
    """
    
    USER_AGENT = "ExtendedMMBot/1.0 (Conservative Market Maker)"
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.endpoints: EndpointConfig = config.endpoints
        self.api_key = config.api_key
        
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_count = 0
        self._rate_limit_hits = 0
        self._last_request_time = 0
        
    async def __aenter__(self):
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def start(self):
        """Initialize the HTTP session."""
        if self._session is None or self._session.closed:
            timeout = ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
            logger.info(f"API client started. Base URL: {self.endpoints.rest_base}")
    
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("API client closed")
    
    def _get_headers(self) -> dict:
        """Get common request headers."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
            "X-Api-Key": self.api_key,
        }
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict = None,
        json_data: dict = None,
        max_retries: int = 3
    ) -> APIResponse:
        """
        Make an authenticated API request with retry logic.
        """
        if self._session is None or self._session.closed:
            await self.start()
        
        url = f"{self.endpoints.rest_base}{endpoint}"
        headers = self._get_headers()
        
        for attempt in range(max_retries):
            try:
                self._request_count += 1
                self._last_request_time = time.time()
                
                logger.debug(f"API {method} {endpoint} (attempt {attempt + 1})")
                
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data
                ) as response:
                    # Parse rate limit headers if present
                    rate_limit_remaining = response.headers.get("X-RateLimit-Remaining")
                    rate_limit_reset = response.headers.get("X-RateLimit-Reset")
                    
                    # Handle rate limiting
                    if response.status == 429:
                        self._rate_limit_hits += 1
                        retry_after = int(response.headers.get("Retry-After", 1))
                        logger.warning(f"Rate limit hit. Waiting {retry_after}s...")
                        await asyncio.sleep(retry_after + 0.5)  # Add jitter
                        continue
                    
                    # Parse response body
                    try:
                        data = await response.json()
                    except Exception:
                        data = await response.text()
                    
                    # Check for errors
                    if response.status >= 400:
                        error_msg = data.get("error", str(data)) if isinstance(data, dict) else str(data)
                        logger.error(f"API error {response.status}: {error_msg}")
                        
                        if response.status >= 500 and attempt < max_retries - 1:
                            # Retry on server errors
                            wait_time = (2 ** attempt) + 0.5
                            await asyncio.sleep(wait_time)
                            continue
                        
                        return APIResponse(
                            success=False,
                            error=error_msg,
                            status_code=response.status,
                            rate_limit_remaining=int(rate_limit_remaining) if rate_limit_remaining else None,
                            rate_limit_reset=int(rate_limit_reset) if rate_limit_reset else None
                        )
                    
                    return APIResponse(
                        success=True,
                        data=data,
                        status_code=response.status,
                        rate_limit_remaining=int(rate_limit_remaining) if rate_limit_remaining else None,
                        rate_limit_reset=int(rate_limit_reset) if rate_limit_reset else None
                    )
                    
            except asyncio.TimeoutError:
                logger.warning(f"Request timeout on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return APIResponse(success=False, error="Request timeout")
                
            except aiohttp.ClientError as e:
                logger.error(f"Client error: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return APIResponse(success=False, error=str(e))
        
        return APIResponse(success=False, error="Max retries exceeded")
    
    # ==================== PUBLIC ENDPOINTS ====================
    
    async def get_markets(self) -> APIResponse:
        """Get list of available markets."""
        return await self._request("GET", "/info/markets")
    
    async def get_market(self, market: str) -> APIResponse:
        """Get market details including tick size and min size."""
        return await self._request("GET", "/info/markets", params={"market": market})
    
    async def get_orderbook(self, market: str, depth: int = 20) -> APIResponse:
        """Get current order book snapshot."""
        return await self._request("GET", f"/info/markets/{market}/orderbook", params={"depth": depth})
    
    async def get_trades(self, market: str, limit: int = 50) -> APIResponse:
        """Get recent trades."""
        return await self._request("GET", f"/info/markets/{market}/trades", params={"limit": limit})
    
    async def get_ticker(self, market: str) -> APIResponse:
        """Get market ticker (price, 24h volume, etc.)."""
        return await self._request("GET", f"/info/markets/{market}/stats")
    
    # ==================== ACCOUNT ENDPOINTS ====================
    
    async def get_balance(self) -> APIResponse:
        """Get account balances."""
        return await self._request("GET", "/user/balance")
    
    async def get_positions(self) -> APIResponse:
        """Get open positions."""
        return await self._request("GET", "/user/positions")
    
    async def get_position(self, market: str) -> APIResponse:
        """Get position for a specific market."""
        return await self._request("GET", f"/user/positions/{market}")
    
    async def get_open_orders(self, market: str = None) -> APIResponse:
        """Get open orders, optionally filtered by market."""
        params = {"market": market} if market else None
        return await self._request("GET", "/user/orders", params=params)
    
    async def get_order(self, order_id: str) -> APIResponse:
        """Get a specific order by ID."""
        return await self._request("GET", f"/user/orders/{order_id}")
    
    async def get_fills(self, market: str = None, limit: int = 50) -> APIResponse:
        """Get trade fills/executions."""
        params = {"limit": limit}
        if market:
            params["market"] = market
        return await self._request("GET", "/user/fills", params=params)
    
    async def get_fees(self, market: str) -> APIResponse:
        """Get fee rates for a market."""
        return await self._request("GET", "/user/fees", params={"market": market})
    
    # ==================== ORDER MANAGEMENT ====================
    
    async def create_order(self, order_payload: dict) -> APIResponse:
        """
        Create a new order.
        
        Args:
            order_payload: Signed order data from OrderBuilder
        """
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would create order: {order_payload.get('side')} "
                       f"{order_payload.get('size')} @ {order_payload.get('price')}")
            return APIResponse(
                success=True,
                data={
                    "orderId": f"dry_run_{int(time.time()*1000)}",
                    "clientId": order_payload.get("clientId"),
                    "status": "DRY_RUN"
                }
            )
        
        return await self._request("POST", "/user/order", json_data=order_payload)
    
    async def cancel_order(self, order_id: str) -> APIResponse:
        """Cancel a specific order."""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would cancel order: {order_id}")
            return APIResponse(success=True, data={"orderId": order_id, "status": "cancelled"})

        return await self._request("DELETE", f"/user/order/{order_id}")
    
    async def cancel_order_by_client_id(self, client_id: str) -> APIResponse:
        """Cancel an order by client ID."""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would cancel order by client ID: {client_id}")
            return APIResponse(success=True, data={"clientId": client_id, "status": "cancelled"})
        
        return await self._request("DELETE", f"/user/orders/client/{client_id}")
    
    async def cancel_all_orders(self, market: str = None) -> APIResponse:
        """Cancel all open orders, optionally for a specific market."""
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would cancel all orders" + (f" for {market}" if market else ""))
            return APIResponse(success=True, data={"cancelled": 0})

        # Use massCancel endpoint per SDK
        payload = {}
        if market:
            payload["markets"] = [market]
        else:
            payload["cancelAll"] = True

        return await self._request("POST", "/user/order/massCancel", json_data=payload)
    
    async def set_dead_man_switch(self, timeout_sec: int) -> APIResponse:
        """
        Set dead man's switch (auto-cancel if no heartbeat).
        
        Args:
            timeout_sec: Seconds until auto-cancel (0 to disable)
        """
        if self.config.dry_run:
            logger.info(f"[DRY RUN] Would set dead man's switch: {timeout_sec}s")
            return APIResponse(success=True, data={"timeout": timeout_sec})
        
        return await self._request("POST", "/user/dead-man-switch", json_data={"timeout": timeout_sec})
    
    async def heartbeat(self) -> APIResponse:
        """Send heartbeat to keep dead man's switch alive."""
        if self.config.dry_run:
            return APIResponse(success=True, data={"status": "ok"})
        
        return await self._request("POST", "/user/heartbeat")
    
    # ==================== UTILITY METHODS ====================
    
    async def check_connectivity(self) -> bool:
        """Check if API is reachable."""
        try:
            response = await self.get_markets()
            return response.success
        except Exception as e:
            logger.error(f"Connectivity check failed: {e}")
            return False
    
    async def get_market_info(self, market: str) -> dict:
        """Get comprehensive market information."""
        response = await self.get_market(market)
        if not response.success:
            raise APIError(f"Failed to get market info: {response.error}")

        # Response is {'status': 'OK', 'data': [market_info, ...]}
        # Extract the first market from the list
        data = response.data
        if isinstance(data, dict) and "data" in data:
            markets = data["data"]
            if markets and len(markets) > 0:
                return markets[0]
        elif isinstance(data, list) and len(data) > 0:
            return data[0]

        return data
    
    async def get_maker_fee(self, market: str) -> float:
        """Get maker fee rate for a market."""
        response = await self.get_fees(market)
        if not response.success:
            logger.warning(f"Failed to get fees: {response.error}. Using default 0.0002")
            return 0.0002  # Default 2 bps
        
        # Parse fee from response
        data = response.data
        if isinstance(data, dict):
            return float(data.get("makerFee", data.get("maker", 0.0002)))
        return 0.0002
    
    def get_stats(self) -> dict:
        """Get client statistics."""
        return {
            "total_requests": self._request_count,
            "rate_limit_hits": self._rate_limit_hits,
            "last_request_time": self._last_request_time
        }


async def test_api_connection(config: BotConfig) -> bool:
    """Test API connection and credentials."""
    async with ExtendedAPIClient(config) as client:
        logger.info("Testing API connection...")
        
        # Test public endpoint
        markets = await client.get_markets()
        if not markets.success:
            logger.error(f"Failed to get markets: {markets.error}")
            return False
        logger.info(f"[OK] Markets endpoint OK. Found {len(markets.data) if markets.data else 0} markets")
        
        # Test authenticated endpoint
        balance = await client.get_balance()
        if not balance.success:
            logger.error(f"Failed to get balance: {balance.error}")
            return False
        logger.info("[OK] Balance endpoint OK")
        
        # Test fees endpoint
        market = config.strategy.market
        fees = await client.get_fees(market)
        if fees.success:
            logger.info(f"[OK] Fees endpoint OK. Data: {fees.data}")
        else:
            logger.warning(f"Fees endpoint returned error: {fees.error}")
        
        return True
