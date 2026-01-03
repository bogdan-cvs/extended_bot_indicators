"""
Signing module for Extended Exchange API.
Uses SNIP12 (EIP712 for Starknet) format for order signing.
"""

import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Tuple, Optional
import logging

from fast_stark_crypto import get_order_msg_hash, sign, get_public_key

logger = logging.getLogger(__name__)


@dataclass
class StarknetDomain:
    """Starknet domain for SNIP12 signing."""
    name: str = "Perpetuals"
    version: str = "v0"
    chain_id: str = "SN_SEPOLIA"  # SN_MAIN for mainnet
    revision: str = "1"


@dataclass
class MarketL2Config:
    """L2 configuration for a market."""
    synthetic_id: str  # hex string like "0x4254432d555344"
    synthetic_resolution: int  # e.g., 10000000000 for BTC
    collateral_id: str  # "0x1" for USDC
    collateral_resolution: int  # 1000000 (6 decimals)
    qty_precision: int = 2  # decimal places for quantity
    price_precision: int = 2  # decimal places for price


# Default market configs - these will be updated from API
MARKET_L2_CONFIGS = {
    "BTC-USD": MarketL2Config(
        synthetic_id="0x4254432d555344",
        synthetic_resolution=10000000000,
        collateral_id="0x1",
        collateral_resolution=1000000,
    ),
    "ETH-USD": MarketL2Config(
        synthetic_id="0x4554482d555344",
        synthetic_resolution=100000000,
        collateral_id="0x1",
        collateral_resolution=1000000,
    ),
    "AAVE-USD": MarketL2Config(
        synthetic_id="0x414156452d33000000000000000000",  # From API
        synthetic_resolution=1000,  # From API
        collateral_id="0x31857064564ed0ff978e687456963cba09c2c6985d8f9300a1de4962fafa054",  # From API
        collateral_resolution=1000000,  # From API
    ),
}


class StarkSigner:
    """Stark curve signer for Extended Exchange."""

    def __init__(self, private_key: str):
        """
        Initialize signer with private key.

        Args:
            private_key: Hex string private key (with or without 0x prefix)
        """
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        self._private_key = int(private_key, 16)
        self._public_key = get_public_key(self._private_key)

    @property
    def public_key(self) -> int:
        """Get public key as integer."""
        return self._public_key

    @property
    def public_key_hex(self) -> str:
        """Get public key as hex string."""
        return hex(self._public_key)

    def sign(self, msg_hash: int) -> Tuple[int, int]:
        """
        Sign a message hash.

        Args:
            msg_hash: Message hash as integer

        Returns:
            Tuple of (r, s) signature components
        """
        return sign(self._private_key, msg_hash)


class OrderBuilder:
    """
    Builds and signs orders for Extended Exchange API.
    Uses SNIP12 format compatible with the exchange.
    """

    def __init__(
        self,
        signer: StarkSigner,
        vault: int,
        domain: Optional[StarknetDomain] = None
    ):
        """
        Initialize order builder.

        Args:
            signer: StarkSigner instance
            vault: Vault/position ID for collateral
            domain: Starknet domain for signing (default testnet)
        """
        self.signer = signer
        self.vault = vault
        self.domain = domain or StarknetDomain()

    def build_limit_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        post_only: bool = True,
        reduce_only: bool = False,
        client_id: Optional[str] = None,
        expiration_sec: int = 3600,
        l2_config: Optional[MarketL2Config] = None,
    ) -> dict:
        """
        Build a signed limit order.

        Args:
            market: Market name (e.g., "BTC-USD")
            side: "BUY" or "SELL"
            size: Order size in base currency
            price: Limit price
            post_only: If True, order will be maker-only
            reduce_only: If True, order can only reduce position
            client_id: Optional client order ID
            expiration_sec: Order expiration in seconds from now
            l2_config: Market L2 config (fetched if not provided)

        Returns:
            Signed order payload ready for API submission
        """
        # Get L2 config
        if l2_config is None:
            l2_config = MARKET_L2_CONFIGS.get(market)
            if l2_config is None:
                raise ValueError(f"Unknown market: {market}. Please provide l2_config.")

        import math
        import random

        # Generate nonce - random 32-bit int like SDK
        nonce = random.randint(0, 2**32 - 1)

        # Calculate expiration timestamp
        expiration_ts = int(time.time()) + expiration_sec
        # Settlement expiration: add 14 days buffer like SDK, use Unix seconds
        settlement_expiration = math.ceil(expiration_ts + 14 * 24 * 3600)

        # Round size and price BEFORE computing hash
        # This is critical - the hash must be computed with the same values sent to API
        size_rounded = Decimal(str(round(float(size), l2_config.qty_precision)))
        price_rounded = Decimal(str(round(float(price), l2_config.price_precision)))

        # Per SDK:
        # BUY: synthetic rounding=ROUND_UP, collateral rounding=ROUND_UP (paying more)
        # SELL: synthetic rounding=ROUND_DOWN, collateral rounding=ROUND_DOWN (receiving less)
        is_buy = side.upper() == "BUY"
        rounding_context = ROUND_UP if is_buy else ROUND_DOWN

        # Calculate stark amounts using ROUNDED values and correct rounding
        synthetic_amount = self._to_stark_amount(size_rounded, l2_config.synthetic_resolution, rounding_context)
        collateral_amount = self._to_stark_amount(size_rounded * price_rounded, l2_config.collateral_resolution, rounding_context)

        # Per SDK: BUY = synthetic positive, collateral NEGATIVE
        #          SELL = synthetic NEGATIVE, collateral positive
        if is_buy:
            base_amount = synthetic_amount    # positive - receiving synthetic
            quote_amount = -collateral_amount  # negative - paying collateral
        else:
            base_amount = -synthetic_amount   # negative - giving synthetic
            quote_amount = collateral_amount   # positive - receiving collateral

        # Calculate fee using taker_fee_rate - always ROUND_UP per SDK
        fee_rate = Decimal("0.0005")
        fee_amount = self._to_stark_amount(size_rounded * price_rounded * fee_rate, l2_config.collateral_resolution, ROUND_UP)

        # Compute order hash using SNIP12
        order_hash = get_order_msg_hash(
            position_id=self.vault,
            base_asset_id=int(l2_config.synthetic_id, 16),
            base_amount=base_amount,
            quote_asset_id=int(l2_config.collateral_id, 16),
            quote_amount=quote_amount,
            fee_amount=fee_amount,
            fee_asset_id=int(l2_config.collateral_id, 16),
            expiration=settlement_expiration,
            salt=nonce,
            user_public_key=self.signer.public_key,
            domain_name=self.domain.name,
            domain_version=self.domain.version,
            domain_chain_id=self.domain.chain_id,
            domain_revision=self.domain.revision,
        )

        # Sign the hash
        r, s = self.signer.sign(order_hash)

        # Build order ID from hash if not provided
        order_id = client_id or str(order_hash)

        # Build the order payload using already-rounded values
        # NOTE: Extended testnet has a bug where SELL orders with postOnly=True
        # are silently rejected by settlement layer. Force postOnly=False for SELL.
        effective_post_only = post_only if is_buy else False

        order_payload = {
            "id": order_id,
            "market": market,
            "type": "LIMIT",
            "side": side.upper(),
            "qty": str(size_rounded),
            "price": str(price_rounded),
            "postOnly": effective_post_only,
            "reduceOnly": reduce_only,
            "timeInForce": "GTT",
            "expiryEpochMillis": expiration_ts * 1000,
            "fee": str(fee_rate),
            "nonce": str(nonce),
            "selfTradeProtectionLevel": "ACCOUNT",
            "settlement": {
                "signature": {
                    "r": hex(r),
                    "s": hex(s),
                },
                "starkKey": self.signer.public_key_hex,
                "collateralPosition": str(self.vault),
            },
        }

        if not is_buy and post_only:
            logger.debug(f"SELL order: forcing postOnly=False due to Extended testnet limitation")

        logger.debug(
            f"Built order: {side} {size} {market} @ {price}, "
            f"hash={hex(order_hash)}, nonce={nonce}, postOnly={effective_post_only}"
        )

        return order_payload

    def build_ioc_order(
        self,
        market: str,
        side: str,
        size: Decimal,
        price: Decimal,
        reduce_only: bool = False,
        client_id: Optional[str] = None,
        l2_config: Optional[MarketL2Config] = None,
    ) -> dict:
        """
        Build a signed IOC (Immediate-Or-Cancel) order.

        Args:
            market: Market name
            side: "BUY" or "SELL"
            size: Order size
            price: Limit price
            reduce_only: If True, order can only reduce position
            client_id: Optional client order ID
            l2_config: Market L2 config

        Returns:
            Signed order payload
        """
        order = self.build_limit_order(
            market=market,
            side=side,
            size=size,
            price=price,
            post_only=False,
            reduce_only=reduce_only,
            client_id=client_id,
            expiration_sec=60,  # Short expiration for IOC
            l2_config=l2_config,
        )
        order["timeInForce"] = "IOC"
        return order

    def _to_stark_amount(self, amount: Decimal, resolution: int, rounding=ROUND_DOWN) -> int:
        """Convert decimal amount to stark integer amount."""
        stark_amount = amount * Decimal(resolution)
        return int(stark_amount.quantize(Decimal("1"), rounding=rounding))


def create_order_builder(
    private_key: str,
    vault: int,
    is_testnet: bool = True
) -> OrderBuilder:
    """
    Create an OrderBuilder instance.

    Args:
        private_key: Stark private key (hex string)
        vault: Vault/position ID
        is_testnet: True for testnet, False for mainnet

    Returns:
        Configured OrderBuilder instance
    """
    signer = StarkSigner(private_key)
    domain = StarknetDomain(
        chain_id="SN_SEPOLIA" if is_testnet else "SN_MAIN"
    )
    return OrderBuilder(signer, vault, domain)


def update_market_l2_config(market: str, config: dict):
    """
    Update L2 config for a market from API response.

    Args:
        market: Market name
        config: L2 config dict from API (l2Config from /info/markets endpoint)
    """
    # API uses syntheticId/collateralId (not syntheticAssetId/collateralAssetId)
    synthetic_id = config.get("syntheticId", config.get("syntheticAssetId", config.get("synthetic_asset_id")))
    collateral_id = config.get("collateralId", config.get("collateralAssetId", config.get("collateral_asset_id")))

    # Get existing config as fallback
    existing = MARKET_L2_CONFIGS.get(market)

    MARKET_L2_CONFIGS[market] = MarketL2Config(
        synthetic_id=synthetic_id or (existing.synthetic_id if existing else "0x0"),
        synthetic_resolution=int(config.get("syntheticResolution", config.get("synthetic_resolution", 100000000))),
        collateral_id=collateral_id or (existing.collateral_id if existing else "0x1"),
        collateral_resolution=int(config.get("collateralResolution", config.get("collateral_resolution", 1000000))),
    )
    logger.info(f"Updated L2 config for {market}: {MARKET_L2_CONFIGS[market]}")


# Legacy compatibility - keep for order_manager.py imports
def generate_nonce() -> int:
    """Generate a unique nonce for an order."""
    return int(time.time() * 1000000)


def generate_client_id(prefix: str = "mm") -> str:
    """Generate a unique client order ID."""
    timestamp = int(time.time() * 1000)
    return f"{prefix}_{timestamp}_{generate_nonce() % 10000:04d}"


def get_expiration(seconds_from_now: int = 3600) -> int:
    """Get expiration timestamp (Unix seconds)."""
    return int(time.time()) + seconds_from_now
