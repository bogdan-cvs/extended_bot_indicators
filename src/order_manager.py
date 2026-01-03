"""
Order management module for Extended MM Bot.
Handles order lifecycle, tracking, and execution.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import logging

from decimal import Decimal
from .config import BotConfig
from .api_client import ExtendedAPIClient, APIResponse
from .signing import StarkSigner, OrderBuilder, create_order_builder, generate_client_id

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Order status states."""
    PENDING = "pending"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class ManagedOrder:
    """Internal order representation."""
    client_id: str
    order_id: str = ""
    market: str = ""
    side: OrderSide = OrderSide.BUY
    price: float = 0.0
    size: float = 0.0
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = 0.0
    updated_at: float = 0.0
    is_post_only: bool = True
    error: str = ""
    
    @property
    def remaining_size(self) -> float:
        return self.size - self.filled_size
    
    @property
    def notional_usd(self) -> float:
        return self.price * self.size
    
    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED)


@dataclass
class QuotePair:
    """Bid/ask quote pair."""
    bid: Optional[ManagedOrder] = None
    ask: Optional[ManagedOrder] = None
    
    @property
    def has_bid(self) -> bool:
        return self.bid is not None and self.bid.is_active
    
    @property
    def has_ask(self) -> bool:
        return self.ask is not None and self.ask.is_active
    
    @property
    def is_complete(self) -> bool:
        return self.has_bid and self.has_ask


class OrderManager:
    """
    Manages order lifecycle for market making.
    
    Features:
    - Order tracking and state management
    - Cancel-before-replace pattern
    - Mass cancellation
    - Idempotent order operations
    - Anti-self-trade checks
    """
    
    def __init__(
        self,
        config: BotConfig,
        api_client: ExtendedAPIClient,
        private_key: str
    ):
        self.config = config
        self.api = api_client
        from .config import Environment
        self.order_builder = create_order_builder(
            private_key=private_key,
            vault=int(config.vault_id) if config.vault_id else 0,
            is_testnet=(config.environment == Environment.TESTNET)
        )
        
        # Order tracking
        self._orders: dict[str, ManagedOrder] = {}  # client_id -> order
        self._order_id_map: dict[str, str] = {}  # order_id -> client_id
        self._current_quotes: dict[str, QuotePair] = {}  # market -> quotes
        
        # Maker fee (fetched from API)
        self._maker_fee: float = 0.0002  # Default 2 bps
        
        # Stats
        self._orders_created = 0
        self._orders_cancelled = 0
        self._orders_filled = 0
        self._orders_rejected = 0
    
    async def initialize(self):
        """Initialize order manager, fetch fees."""
        try:
            self._maker_fee = await self.api.get_maker_fee(self.config.strategy.market)
            logger.info(f"Maker fee: {self._maker_fee * 10000:.2f} bps")
        except Exception as e:
            logger.warning(f"Failed to fetch maker fee, using default: {e}")
    
    async def sync_open_orders(self):
        """Sync internal state with exchange open orders."""
        response = await self.api.get_open_orders(self.config.strategy.market)
        
        if not response.success:
            logger.error(f"Failed to sync orders: {response.error}")
            return
        
        exchange_orders = response.data if response.data else []

        # Handle case where data might be wrapped in a dict
        if isinstance(exchange_orders, dict):
            exchange_orders = exchange_orders.get("orders", exchange_orders.get("data", []))

        # Update internal tracking
        for order_data in exchange_orders:
            # Skip if not a dict (handle unexpected data format)
            if not isinstance(order_data, dict):
                logger.warning(f"Unexpected order data format: {type(order_data)}")
                continue
            order_id = order_data.get("orderId", order_data.get("id", ""))
            client_id = order_data.get("clientId", "")
            
            if client_id and client_id in self._orders:
                order = self._orders[client_id]
                order.order_id = order_id
                order.filled_size = float(order_data.get("filledSize", 0))
                order.status = self._parse_status(order_data.get("status", "OPEN"))
                order.updated_at = time.time()
        
        logger.info(f"Synced {len(exchange_orders)} open orders")
    
    async def place_quote(
        self,
        side: str,
        price: float,
        size: float,
        cancel_existing: bool = True
    ) -> Optional[ManagedOrder]:
        """
        Place a market making quote.
        
        Args:
            side: "BUY" or "SELL"
            price: Quote price
            size: Quote size
            cancel_existing: Whether to cancel existing quote on same side first
        """
        market = self.config.strategy.market
        
        # Cancel existing quote if requested
        if cancel_existing:
            await self._cancel_side(market, side)
        
        # Anti-self-trade check
        if not self._check_no_self_trade(side, price):
            logger.warning(f"Self-trade risk detected for {side} @ {price:.6f}")
            return None
        
        # Build signed order
        client_id = generate_client_id()
        order_payload = self.order_builder.build_limit_order(
            market=market,
            side=side,
            size=Decimal(str(size)),
            price=Decimal(str(price)),
            post_only=self.config.order.post_only,
            client_id=client_id,
            expiration_sec=self.config.order.order_expiration_sec
        )
        
        # Create internal tracking
        order = ManagedOrder(
            client_id=client_id,
            market=market,
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            price=price,
            size=size,
            status=OrderStatus.PENDING,
            created_at=time.time(),
            updated_at=time.time(),
            is_post_only=self.config.order.post_only
        )
        
        self._orders[client_id] = order

        # Place order - log full payload for debugging
        import json
        logger.info(f"Placing order: {side} {size} @ {price}")
        logger.debug(f"Order payload: {json.dumps({k: v for k, v in order_payload.items() if k != 'settlement'})}")
        logger.debug(f"Settlement starkKey: {order_payload.get('settlement', {}).get('starkKey', 'N/A')}")
        response = await self.api.create_order(order_payload)
        
        if response.success:
            order.order_id = response.data.get("orderId", response.data.get("id", ""))
            order.status = OrderStatus.OPEN
            order.updated_at = time.time()
            
            if order.order_id:
                self._order_id_map[order.order_id] = client_id
            
            self._orders_created += 1
            
            # Update current quotes
            if market not in self._current_quotes:
                self._current_quotes[market] = QuotePair()
            
            if side == "BUY":
                self._current_quotes[market].bid = order
            else:
                self._current_quotes[market].ask = order
            
            logger.info(
                f"Quote placed: {side} {size:.6f} @ {price:.6f} "
                f"(client_id: {client_id[:12]}...)"
            )
            return order
        else:
            order.status = OrderStatus.REJECTED
            order.error = response.error or "Unknown error"
            self._orders_rejected += 1
            
            logger.error(f"Failed to place quote: {response.error}")
            return None
    
    async def place_quotes(
        self,
        bid_price: float,
        bid_size: float,
        ask_price: float,
        ask_size: float,
        cancel_existing: bool = True
    ) -> QuotePair:
        """
        Place bid and ask quotes atomically.
        
        Args:
            bid_price: Bid price
            bid_size: Bid size
            ask_price: Ask price
            ask_size: Ask size
            cancel_existing: Cancel existing quotes first
        """
        market = self.config.strategy.market

        # Cancel existing quotes
        if cancel_existing:
            await self.cancel_all_quotes(market)

        # Place both quotes (skip if size/price is 0)
        bid = None
        ask = None

        if bid_size > 0 and bid_price > 0:
            bid = await self.place_quote("BUY", bid_price, bid_size, cancel_existing=False)
        else:
            logger.debug(f"Skipping BID order: size={bid_size}, price={bid_price}")

        if ask_size > 0 and ask_price > 0:
            ask = await self.place_quote("SELL", ask_price, ask_size, cancel_existing=False)
        else:
            logger.debug(f"Skipping ASK order: size={ask_size}, price={ask_price}")

        return QuotePair(bid=bid, ask=ask)
    
    async def cancel_order(self, client_id: str) -> bool:
        """Cancel an order by client ID."""
        if client_id not in self._orders:
            logger.warning(f"Order not found: {client_id}")
            return False
        
        order = self._orders[client_id]
        
        if not order.is_active:
            return True
        
        if order.order_id:
            response = await self.api.cancel_order(order.order_id)
        else:
            response = await self.api.cancel_order_by_client_id(client_id)
        
        if response.success:
            order.status = OrderStatus.CANCELLED
            order.updated_at = time.time()
            self._orders_cancelled += 1
            logger.debug(f"Order cancelled: {client_id[:12]}...")
            return True
        else:
            logger.warning(f"Failed to cancel order {client_id}: {response.error}")
            return False
    
    async def cancel_all_quotes(self, market: str = None) -> int:
        """Cancel all open quotes for a market."""
        market = market or self.config.strategy.market
        
        response = await self.api.cancel_all_orders(market)
        
        if response.success:
            # Update internal state
            for order in self._orders.values():
                if order.market == market and order.is_active:
                    order.status = OrderStatus.CANCELLED
                    order.updated_at = time.time()
            
            # Clear current quotes
            if market in self._current_quotes:
                self._current_quotes[market] = QuotePair()
            
            cancelled = response.data.get("cancelled", 0) if response.data else 0
            self._orders_cancelled += cancelled
            logger.info(f"Cancelled all orders for {market}: {cancelled}")
            return cancelled
        else:
            logger.error(f"Failed to cancel all orders: {response.error}")
            return 0
    
    async def flatten_position(self, current_mid: float, position_size: float) -> bool:
        """
        Flatten position using IOC order (risk-off).
        
        Args:
            current_mid: Current mid price
            position_size: Current position size (positive = long, negative = short)
        """
        if abs(position_size) < 1e-8:
            return True
        
        if not self.config.order.use_taker_for_risk_off:
            logger.warning("Taker for risk-off is disabled, cannot flatten")
            return False
        
        market = self.config.strategy.market
        
        # Determine side and price
        if position_size > 0:
            # Long position, need to sell
            side = "SELL"
            # Use aggressive price (below mid)
            offset_bps = self.config.order.taker_offset_bps
            price = current_mid * (1 - offset_bps / 10000)
        else:
            # Short position, need to buy
            side = "BUY"
            # Use aggressive price (above mid)
            offset_bps = self.config.order.taker_offset_bps
            price = current_mid * (1 + offset_bps / 10000)
        
        size = abs(position_size)

        # Build IOC order
        order_payload = self.order_builder.build_ioc_order(
            market=market,
            side=side,
            size=Decimal(str(size)),
            price=Decimal(str(price)),
            reduce_only=True
        )
        
        logger.warning(f"FLATTEN: {side} {size:.6f} @ {price:.6f} (IOC)")
        
        response = await self.api.create_order(order_payload)
        
        if response.success:
            logger.info("Flatten order placed successfully")
            return True
        else:
            logger.error(f"Failed to flatten: {response.error}")
            return False
    
    async def _cancel_side(self, market: str, side: str):
        """Cancel existing orders on a specific side."""
        if market not in self._current_quotes:
            return
        
        quotes = self._current_quotes[market]
        
        if side == "BUY" and quotes.bid and quotes.bid.is_active:
            await self.cancel_order(quotes.bid.client_id)
        elif side == "SELL" and quotes.ask and quotes.ask.is_active:
            await self.cancel_order(quotes.ask.client_id)
    
    def _check_no_self_trade(self, side: str, price: float) -> bool:
        """
        Check that new order won't immediately trade against our orders.
        
        Returns:
            True if safe, False if self-trade risk
        """
        market = self.config.strategy.market
        
        if market not in self._current_quotes:
            return True
        
        quotes = self._current_quotes[market]
        
        if side == "BUY":
            # Check if buy would cross our ask
            if quotes.ask and quotes.ask.is_active:
                if price >= quotes.ask.price:
                    return False
        else:
            # Check if sell would cross our bid
            if quotes.bid and quotes.bid.is_active:
                if price <= quotes.bid.price:
                    return False
        
        return True
    
    def _parse_status(self, status_str: str) -> OrderStatus:
        """Parse order status from API response."""
        status_map = {
            "PENDING": OrderStatus.PENDING,
            "OPEN": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.EXPIRED,
        }
        return status_map.get(status_str.upper(), OrderStatus.OPEN)
    
    def get_current_quotes(self, market: str = None) -> QuotePair:
        """Get current quotes for a market."""
        market = market or self.config.strategy.market
        return self._current_quotes.get(market, QuotePair())
    
    def get_active_orders(self, market: str = None) -> list[ManagedOrder]:
        """Get all active orders."""
        market = market or self.config.strategy.market
        return [
            o for o in self._orders.values() 
            if o.is_active and (market is None or o.market == market)
        ]
    
    def get_stats(self) -> dict:
        """Get order manager statistics."""
        return {
            "orders_created": self._orders_created,
            "orders_cancelled": self._orders_cancelled,
            "orders_filled": self._orders_filled,
            "orders_rejected": self._orders_rejected,
            "active_orders": len(self.get_active_orders()),
            "maker_fee_bps": self._maker_fee * 10000,
        }
    
    def update_order_from_fill(self, order_id: str, filled_size: float, remaining_size: float):
        """Update order state from fill notification."""
        if order_id not in self._order_id_map:
            return
        
        client_id = self._order_id_map[order_id]
        if client_id not in self._orders:
            return
        
        order = self._orders[client_id]
        order.filled_size = filled_size
        order.updated_at = time.time()
        
        if remaining_size <= 0:
            order.status = OrderStatus.FILLED
            self._orders_filled += 1
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        
        logger.info(
            f"Order fill: {order.side.value} {filled_size:.6f}/{order.size:.6f} @ {order.price:.6f}"
        )
