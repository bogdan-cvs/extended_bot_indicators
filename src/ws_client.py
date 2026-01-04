"""
WebSocket client for Extended Exchange market data streaming.
Handles order book, trades, and ticker streams with automatic reconnection.
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
import logging
import websockets
from websockets.exceptions import ConnectionClosed

from .config import BotConfig

logger = logging.getLogger(__name__)


@dataclass
class OrderBookLevel:
    """Single order book level."""
    price: float
    size: float


@dataclass
class OrderBook:
    """Order book snapshot."""
    market: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0
    sequence: int = 0
    
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None
    
    @property
    def spread_bps(self) -> Optional[float]:
        if self.mid_price and self.spread:
            return (self.spread / self.mid_price) * 10000
        return None
    
    def get_bid_liquidity(self, depth: int = 5) -> float:
        """Get total bid liquidity up to depth levels."""
        return sum(level.size for level in self.bids[:depth])
    
    def get_ask_liquidity(self, depth: int = 5) -> float:
        """Get total ask liquidity up to depth levels."""
        return sum(level.size for level in self.asks[:depth])


@dataclass
class Trade:
    """Single trade."""
    market: str
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    timestamp: float
    trade_id: str = ""


@dataclass
class MarketState:
    """Current market state aggregating order book and trades."""
    market: str
    orderbook: OrderBook = field(default_factory=lambda: OrderBook(""))
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=100))
    last_update: float = 0.0
    ws_connected: bool = False
    
    @property
    def mid_price(self) -> Optional[float]:
        return self.orderbook.mid_price
    
    def get_volatility(self, window_sec: float = 60.0) -> float:
        """Calculate recent volatility from trades."""
        if len(self.recent_trades) < 2:
            return 0.0
        
        now = time.time()
        prices = [t.price for t in self.recent_trades 
                  if now - t.timestamp <= window_sec]
        
        if len(prices) < 2:
            return 0.0
        
        mean_price = sum(prices) / len(prices)
        if mean_price == 0:
            return 0.0
        
        variance = sum((p - mean_price) ** 2 for p in prices) / len(prices)
        std_dev = variance ** 0.5
        
        # Return volatility in bps
        return (std_dev / mean_price) * 10000
    
    def get_recent_trade_direction(self, n: int = 5) -> float:
        """Get recent trade direction bias (-1 to 1)."""
        if len(self.recent_trades) < 1:
            return 0.0
        
        recent = list(self.recent_trades)[-n:]
        buy_volume = sum(t.size for t in recent if t.side == "BUY")
        sell_volume = sum(t.size for t in recent if t.side == "SELL")
        total = buy_volume + sell_volume
        
        if total == 0:
            return 0.0
        
        return (buy_volume - sell_volume) / total


class ExtendedWSClient:
    """
    WebSocket client for Extended Exchange.
    
    Features:
    - Automatic reconnection with exponential backoff
    - Multiple stream subscriptions
    - Order book and trade handling
    - Callbacks for state changes
    """
    
    def __init__(
        self,
        config: BotConfig,
        on_orderbook: Callable[[OrderBook], None] = None,
        on_trade: Callable[[Trade], None] = None,
        on_connect: Callable[[], None] = None,
        on_disconnect: Callable[[], None] = None
    ):
        self.config = config
        self.ws_url = config.endpoints.ws_stream
        
        # Callbacks
        self._on_orderbook = on_orderbook
        self._on_trade = on_trade
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        
        # State
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._connected = False
        self._reconnect_count = 0
        self._max_reconnects = 10
        self._subscriptions: set[str] = set()
        
        # Market state
        self.market_state: dict[str, MarketState] = {}
        
        # Stats
        self._message_count = 0
        self._last_message_time = 0.0
    
    async def start(self):
        """Start the WebSocket client."""
        self._running = True
        logger.info(f"Starting WebSocket client: {self.ws_url}")
        asyncio.create_task(self._connection_loop())
    
    async def stop(self):
        """Stop the WebSocket client."""
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info("WebSocket client stopped")
    
    async def subscribe_orderbook(self, market: str):
        """Subscribe to order book updates for a market."""
        self._subscriptions.add(f"orderbook:{market}")
        if market not in self.market_state:
            self.market_state[market] = MarketState(market=market)
        
        if self._connected:
            await self._send_subscribe("orderbook", market)
    
    async def subscribe_trades(self, market: str):
        """Subscribe to trade updates for a market."""
        self._subscriptions.add(f"trades:{market}")
        if market not in self.market_state:
            self.market_state[market] = MarketState(market=market)
        
        if self._connected:
            await self._send_subscribe("trades", market)
    
    async def subscribe_ticker(self, market: str):
        """Subscribe to ticker updates for a market."""
        self._subscriptions.add(f"ticker:{market}")
        
        if self._connected:
            await self._send_subscribe("ticker", market)
    
    async def _send_subscribe(self, channel: str, market: str):
        """Send subscription message."""
        if not self._ws:
            return
        
        message = {
            "type": "subscribe",
            "channel": channel,
            "market": market
        }
        
        try:
            await self._ws.send(json.dumps(message))
            logger.debug(f"Subscribed to {channel}:{market}")
        except Exception as e:
            logger.error(f"Failed to subscribe to {channel}:{market}: {e}")
    
    async def _connection_loop(self):
        """Main connection loop with automatic reconnection."""
        while self._running:
            try:
                await self._connect()
                await self._message_loop()
            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
            
            if self._running:
                self._connected = False
                self.market_state = {k: MarketState(market=k) for k in self.market_state}
                
                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception as e:
                        logger.error(f"Disconnect callback error: {e}")
                
                # Exponential backoff
                wait_time = min(2 ** self._reconnect_count, 60)
                logger.info(f"Reconnecting in {wait_time}s...")
                await asyncio.sleep(wait_time)
                self._reconnect_count += 1
                
                if self._reconnect_count > self._max_reconnects:
                    logger.error("Max reconnection attempts reached")
                    break
    
    async def _connect(self):
        """Establish WebSocket connection to orderbook stream."""
        extra_headers = {
            "User-Agent": "ExtendedMMBot/1.0",
            "X-Api-Key": self.config.api_key
        }

        # Extended Exchange uses per-stream URLs, not subscribe messages
        # Get the first orderbook subscription market
        market = None
        for sub in self._subscriptions:
            if sub.startswith("orderbook:"):
                market = sub.split(":", 1)[1]
                break

        if not market:
            logger.warning("No orderbook subscription, cannot connect WebSocket")
            return

        # Build the orderbook stream URL
        # Format: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{market}
        ws_url = f"{self.ws_url}/orderbooks/{market}"
        logger.info(f"Connecting to WebSocket: {ws_url}")

        self._ws = await websockets.connect(
            ws_url,
            extra_headers=extra_headers,
            ping_interval=20,
            ping_timeout=10
        )

        self._connected = True
        self._reconnect_count = 0
        logger.info("WebSocket connected")

        # Update market state
        for state in self.market_state.values():
            state.ws_connected = True

        if self._on_connect:
            try:
                self._on_connect()
            except Exception as e:
                logger.error(f"Connect callback error: {e}")
    
    async def _message_loop(self):
        """Process incoming WebSocket messages."""
        async for message in self._ws:
            self._message_count += 1
            self._last_message_time = time.time()
            
            try:
                data = json.loads(message)
                await self._handle_message(data)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON message: {message[:100]}")
            except Exception as e:
                logger.error(f"Error handling message: {e}")
    
    async def _handle_message(self, data: dict):
        """Handle parsed WebSocket message."""
        msg_type = data.get("type", data.get("channel", ""))

        # Extended Exchange format: type=SNAPSHOT or DELTA, data contains m, b, a
        if msg_type == "SNAPSHOT":
            await self._handle_extended_orderbook_snapshot(data)
        elif msg_type == "DELTA":
            await self._handle_extended_orderbook_delta(data)
        elif msg_type == "orderbook" or msg_type == "orderbook_snapshot":
            await self._handle_orderbook(data)
        elif msg_type == "orderbook_update":
            await self._handle_orderbook_update(data)
        elif msg_type == "trade" or msg_type == "trades":
            await self._handle_trade(data)
        elif msg_type == "ticker":
            await self._handle_ticker(data)
        elif msg_type == "subscribed":
            logger.debug(f"Subscription confirmed: {data}")
        elif msg_type == "error":
            logger.error(f"WebSocket error message: {data}")
        elif msg_type == "pong" or msg_type == "heartbeat":
            pass  # Ignore heartbeats
        else:
            logger.debug(f"Unknown message type: {msg_type}")
    
    async def _handle_orderbook(self, data: dict):
        """Handle order book snapshot."""
        market = data.get("market", "")
        if not market or market not in self.market_state:
            return
        
        bids = [
            OrderBookLevel(price=float(level[0]), size=float(level[1]))
            for level in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(level[0]), size=float(level[1]))
            for level in data.get("asks", [])
        ]
        
        orderbook = OrderBook(
            market=market,
            bids=bids,
            asks=asks,
            timestamp=data.get("timestamp", time.time()),
            sequence=data.get("sequence", 0)
        )
        
        self.market_state[market].orderbook = orderbook
        self.market_state[market].last_update = time.time()
        
        if self._on_orderbook:
            try:
                self._on_orderbook(orderbook)
            except Exception as e:
                logger.error(f"Orderbook callback error: {e}")

    async def _handle_extended_orderbook_snapshot(self, data: dict):
        """Handle Extended Exchange orderbook SNAPSHOT message."""
        inner = data.get("data", {})
        market = inner.get("m", "")
        if not market or market not in self.market_state:
            # Try to find from subscriptions
            for sub in self._subscriptions:
                if sub.startswith("orderbook:"):
                    market = sub.split(":", 1)[1]
                    break

        if not market or market not in self.market_state:
            return

        # Parse bids: [{q: size, p: price}, ...]
        bids = []
        for level in inner.get("b", []):
            price = float(level.get("p", 0))
            size = float(level.get("q", 0))
            if price > 0 and size > 0:
                bids.append(OrderBookLevel(price=price, size=size))

        # Parse asks: [{q: size, p: price}, ...]
        asks = []
        for level in inner.get("a", []):
            price = float(level.get("p", 0))
            size = float(level.get("q", 0))
            if price > 0 and size > 0:
                asks.append(OrderBookLevel(price=price, size=size))

        # Sort: bids descending, asks ascending
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)

        orderbook = OrderBook(
            market=market,
            bids=bids,
            asks=asks,
            timestamp=data.get("ts", time.time() * 1000) / 1000,
            sequence=data.get("seq", 0)
        )

        self.market_state[market].orderbook = orderbook
        self.market_state[market].last_update = time.time()

        if self._on_orderbook:
            try:
                self._on_orderbook(orderbook)
            except Exception as e:
                logger.error(f"Orderbook callback error: {e}")

    async def _handle_extended_orderbook_delta(self, data: dict):
        """Handle Extended Exchange orderbook DELTA message."""
        inner = data.get("data", {})
        market = inner.get("m", "")
        if not market:
            # Try to find from subscriptions
            for sub in self._subscriptions:
                if sub.startswith("orderbook:"):
                    market = sub.split(":", 1)[1]
                    break

        if not market or market not in self.market_state:
            return

        state = self.market_state[market]

        # Update bids: delta q can be negative (reduce) or positive (add)
        for level in inner.get("b", []):
            price = float(level.get("p", 0))
            delta_size = float(level.get("q", 0))
            self._update_extended_book_side(state.orderbook.bids, price, delta_size, reverse=True)

        # Update asks
        for level in inner.get("a", []):
            price = float(level.get("p", 0))
            delta_size = float(level.get("q", 0))
            self._update_extended_book_side(state.orderbook.asks, price, delta_size, reverse=False)

        state.orderbook.timestamp = data.get("ts", time.time() * 1000) / 1000
        state.orderbook.sequence = data.get("seq", state.orderbook.sequence + 1)
        state.last_update = time.time()

        if self._on_orderbook:
            try:
                self._on_orderbook(state.orderbook)
            except Exception as e:
                logger.error(f"Orderbook callback error: {e}")

    def _update_extended_book_side(
        self,
        levels: list[OrderBookLevel],
        price: float,
        delta_size: float,
        reverse: bool
    ):
        """Update a side of the order book with delta size."""
        # Find existing level
        for i, level in enumerate(levels):
            if abs(level.price - price) < 0.0001:  # Float comparison
                new_size = level.size + delta_size
                if new_size <= 0:
                    levels.pop(i)
                else:
                    level.size = new_size
                return

        # New level (only add if positive delta)
        if delta_size > 0:
            levels.append(OrderBookLevel(price=price, size=delta_size))
            # Re-sort
            levels.sort(key=lambda x: x.price, reverse=reverse)

    async def _handle_orderbook_update(self, data: dict):
        """Handle incremental order book update."""
        market = data.get("market", "")
        if not market or market not in self.market_state:
            return
        
        state = self.market_state[market]
        
        # Update bids
        for level in data.get("bids", []):
            price, size = float(level[0]), float(level[1])
            self._update_book_side(state.orderbook.bids, price, size, reverse=True)
        
        # Update asks
        for level in data.get("asks", []):
            price, size = float(level[0]), float(level[1])
            self._update_book_side(state.orderbook.asks, price, size, reverse=False)
        
        state.orderbook.timestamp = data.get("timestamp", time.time())
        state.orderbook.sequence = data.get("sequence", state.orderbook.sequence + 1)
        state.last_update = time.time()
        
        if self._on_orderbook:
            try:
                self._on_orderbook(state.orderbook)
            except Exception as e:
                logger.error(f"Orderbook callback error: {e}")
    
    def _update_book_side(
        self,
        levels: list[OrderBookLevel],
        price: float,
        size: float,
        reverse: bool
    ):
        """Update a side of the order book."""
        # Find existing level
        for i, level in enumerate(levels):
            if level.price == price:
                if size == 0:
                    levels.pop(i)
                else:
                    levels[i] = OrderBookLevel(price=price, size=size)
                return
        
        # Add new level if size > 0
        if size > 0:
            levels.append(OrderBookLevel(price=price, size=size))
            levels.sort(key=lambda x: x.price, reverse=reverse)
    
    async def _handle_trade(self, data: dict):
        """Handle trade message."""
        market = data.get("market", "")
        if not market:
            return
        
        if market not in self.market_state:
            self.market_state[market] = MarketState(market=market)
        
        # Handle both single trade and trade array
        trades = data.get("trades", [data])
        if not isinstance(trades, list):
            trades = [trades]
        
        for trade_data in trades:
            trade = Trade(
                market=market,
                price=float(trade_data.get("price", 0)),
                size=float(trade_data.get("size", trade_data.get("amount", 0))),
                side=trade_data.get("side", "").upper(),
                timestamp=trade_data.get("timestamp", time.time()),
                trade_id=str(trade_data.get("id", trade_data.get("tradeId", "")))
            )
            
            self.market_state[market].recent_trades.append(trade)
            self.market_state[market].last_update = time.time()
            
            if self._on_trade:
                try:
                    self._on_trade(trade)
                except Exception as e:
                    logger.error(f"Trade callback error: {e}")
    
    async def _handle_ticker(self, data: dict):
        """Handle ticker update."""
        # Ticker updates are informational, main data comes from orderbook
        logger.debug(f"Ticker update: {data}")
    
    def get_market_state(self, market: str) -> Optional[MarketState]:
        """Get current market state."""
        return self.market_state.get(market)
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected
    
    def get_stats(self) -> dict:
        """Get WebSocket client statistics."""
        return {
            "connected": self._connected,
            "message_count": self._message_count,
            "last_message_time": self._last_message_time,
            "reconnect_count": self._reconnect_count,
            "subscriptions": list(self._subscriptions)
        }


async def test_ws_connection(config: BotConfig, market: str, timeout: float = 10.0) -> bool:
    """Test WebSocket connection and data reception."""
    received_data = {"orderbook": False, "trade": False}
    
    def on_orderbook(ob: OrderBook):
        received_data["orderbook"] = True
        logger.info(f"Received orderbook: mid={ob.mid_price:.4f}, spread={ob.spread_bps:.2f}bps")
    
    def on_trade(trade: Trade):
        received_data["trade"] = True
        logger.info(f"Received trade: {trade.side} {trade.size} @ {trade.price}")
    
    client = ExtendedWSClient(config, on_orderbook=on_orderbook, on_trade=on_trade)
    
    try:
        await client.start()
        await client.subscribe_orderbook(market)
        await client.subscribe_trades(market)
        
        # Wait for data
        start = time.time()
        while time.time() - start < timeout:
            if received_data["orderbook"]:
                logger.info("[OK] WebSocket orderbook streaming OK")
                return True
            await asyncio.sleep(0.5)
        
        logger.warning("No orderbook data received within timeout")
        return False
        
    finally:
        await client.stop()
