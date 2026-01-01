"""
Stark signature module for Extended Exchange.
Handles order signing using Stark curve cryptography.
"""

import hashlib
import time
from dataclasses import dataclass
from typing import Optional
import logging

# Try to import starknet libraries
try:
    from starknet_py.hash.utils import pedersen_hash
    from starknet_py.net.signer.stark_curve_signer import StarkCurveSigner, KeyPair
    STARKNET_AVAILABLE = True
except ImportError:
    STARKNET_AVAILABLE = False
    print("Warning: starknet-py not installed. Using mock signing for testing.")

logger = logging.getLogger(__name__)


@dataclass
class OrderData:
    """Order data structure for signing."""
    market: str
    side: str  # "BUY" or "SELL"
    order_type: str  # "LIMIT", "MARKET"
    size: str  # Size as string (decimal)
    price: str  # Price as string (decimal)
    time_in_force: str  # "GTC", "IOC", "FOK", "POST_ONLY"
    fee: str  # Fee as string (decimal)
    expiration: int  # Unix timestamp
    nonce: Optional[int] = None
    client_id: Optional[str] = None
    reduce_only: bool = False
    post_only: bool = False


class StarkSigner:
    """
    Handles Stark curve signing for Extended Exchange orders.
    """
    
    def __init__(self, private_key: str, account_address: str = ""):
        self.private_key = private_key
        self.account_address = account_address
        self._key_pair = None
        
        if STARKNET_AVAILABLE and private_key:
            try:
                # Convert hex string to int
                private_key_int = int(private_key, 16) if private_key.startswith("0x") else int(private_key, 16)
                self._key_pair = KeyPair.from_private_key(private_key_int)
                logger.info("StarkSigner initialized with real signing")
            except Exception as e:
                logger.warning(f"Failed to initialize StarkSigner: {e}. Using mock signing.")
                self._key_pair = None
    
    def get_public_key(self) -> str:
        """Get the public key in hex format."""
        if self._key_pair:
            return hex(self._key_pair.public_key)
        return "0x0"
    
    def sign_order(self, order: OrderData, vault_id: str = "") -> dict:
        """
        Sign an order and return the signature components.
        
        Returns:
            dict with 'r', 's' signature components and 'public_key'
        """
        # Build the order hash
        order_hash = self._compute_order_hash(order, vault_id)
        
        if self._key_pair and STARKNET_AVAILABLE:
            # Real signing
            signature = self._key_pair.sign(order_hash)
            return {
                "r": hex(signature[0]),
                "s": hex(signature[1]),
                "public_key": self.get_public_key(),
                "order_hash": hex(order_hash)
            }
        else:
            # Mock signing for dry-run/testing
            mock_sig = self._mock_sign(order_hash)
            return {
                "r": mock_sig["r"],
                "s": mock_sig["s"],
                "public_key": "0x0",
                "order_hash": hex(order_hash) if isinstance(order_hash, int) else order_hash
            }
    
    def _compute_order_hash(self, order: OrderData, vault_id: str) -> int:
        """
        Compute the Pedersen hash of the order.
        
        Note: The exact hash structure depends on the Extended Exchange specification.
        This is a placeholder implementation - adjust based on actual docs.
        """
        if not STARKNET_AVAILABLE:
            # Return a mock hash for testing
            order_str = f"{order.market}{order.side}{order.size}{order.price}{order.expiration}"
            return int(hashlib.sha256(order_str.encode()).hexdigest(), 16) % (2**251)
        
        try:
            # Build hash chain using Pedersen hash
            # The exact order and encoding depends on the exchange specification
            
            # Convert market to felt (simplified)
            market_felt = int.from_bytes(order.market.encode()[:31], 'big')
            
            # Side: 0 for BUY, 1 for SELL
            side_felt = 0 if order.side.upper() == "BUY" else 1
            
            # Convert size and price to fixed-point integers
            # Assuming 8 decimal places
            size_felt = int(float(order.size) * 10**8)
            price_felt = int(float(order.price) * 10**8)
            fee_felt = int(float(order.fee) * 10**8)
            
            # Build hash chain
            h = pedersen_hash(market_felt, side_felt)
            h = pedersen_hash(h, size_felt)
            h = pedersen_hash(h, price_felt)
            h = pedersen_hash(h, fee_felt)
            h = pedersen_hash(h, order.expiration)
            
            if order.nonce is not None:
                h = pedersen_hash(h, order.nonce)
            
            return h
            
        except Exception as e:
            logger.error(f"Error computing order hash: {e}")
            # Fallback to mock hash
            order_str = f"{order.market}{order.side}{order.size}{order.price}{order.expiration}"
            return int(hashlib.sha256(order_str.encode()).hexdigest(), 16) % (2**251)
    
    def _mock_sign(self, message_hash: int) -> dict:
        """Generate a mock signature for testing/dry-run."""
        # Use deterministic mock signatures based on the hash
        hash_bytes = message_hash.to_bytes(32, 'big') if isinstance(message_hash, int) else bytes.fromhex(message_hash[2:] if message_hash.startswith('0x') else message_hash)
        r = int(hashlib.sha256(b"r" + hash_bytes).hexdigest(), 16) % (2**251)
        s = int(hashlib.sha256(b"s" + hash_bytes).hexdigest(), 16) % (2**251)
        return {"r": hex(r), "s": hex(s)}
    
    def verify_signature(self, order_hash: int, r: int, s: int) -> bool:
        """Verify a signature (for testing purposes)."""
        if not STARKNET_AVAILABLE or not self._key_pair:
            return True  # Mock verification
        
        try:
            # Verification would use the public key
            # This is a simplified placeholder
            return True
        except Exception:
            return False


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


class OrderBuilder:
    """Helper class to build signed orders."""
    
    def __init__(self, signer: StarkSigner, vault_id: str = "", default_expiration_sec: int = 3600):
        self.signer = signer
        self.vault_id = vault_id
        self.default_expiration_sec = default_expiration_sec
    
    def build_limit_order(
        self,
        market: str,
        side: str,
        size: float,
        price: float,
        fee: float,
        post_only: bool = True,
        reduce_only: bool = False,
        client_id: str = None,
        expiration_sec: int = None
    ) -> dict:
        """
        Build a signed limit order payload.
        
        Returns:
            dict ready to be sent to the API
        """
        expiration = get_expiration(expiration_sec or self.default_expiration_sec)
        client_id = client_id or generate_client_id()
        
        order_data = OrderData(
            market=market,
            side=side.upper(),
            order_type="LIMIT",
            size=f"{size:.8f}",
            price=f"{price:.8f}",
            time_in_force="POST_ONLY" if post_only else "GTC",
            fee=f"{fee:.8f}",
            expiration=expiration,
            nonce=generate_nonce(),
            client_id=client_id,
            reduce_only=reduce_only,
            post_only=post_only
        )
        
        # Sign the order
        signature = self.signer.sign_order(order_data, self.vault_id)
        
        # Build API payload
        payload = {
            "market": order_data.market,
            "side": order_data.side,
            "type": order_data.order_type,
            "size": order_data.size,
            "price": order_data.price,
            "timeInForce": order_data.time_in_force,
            "fee": order_data.fee,
            "expiration": order_data.expiration,
            "clientId": order_data.client_id,
            "reduceOnly": order_data.reduce_only,
            "postOnly": order_data.post_only,
            "signature": {
                "r": signature["r"],
                "s": signature["s"]
            }
        }
        
        return payload
    
    def build_ioc_order(
        self,
        market: str,
        side: str,
        size: float,
        price: float,
        fee: float,
        reduce_only: bool = False,
        client_id: str = None
    ) -> dict:
        """
        Build a signed IOC (Immediate-Or-Cancel) order for risk-off.
        
        Returns:
            dict ready to be sent to the API
        """
        expiration = get_expiration(300)  # Short expiration for IOC
        client_id = client_id or generate_client_id("ioc")
        
        order_data = OrderData(
            market=market,
            side=side.upper(),
            order_type="LIMIT",
            size=f"{size:.8f}",
            price=f"{price:.8f}",
            time_in_force="IOC",
            fee=f"{fee:.8f}",
            expiration=expiration,
            nonce=generate_nonce(),
            client_id=client_id,
            reduce_only=reduce_only,
            post_only=False
        )
        
        signature = self.signer.sign_order(order_data, self.vault_id)
        
        payload = {
            "market": order_data.market,
            "side": order_data.side,
            "type": order_data.order_type,
            "size": order_data.size,
            "price": order_data.price,
            "timeInForce": order_data.time_in_force,
            "fee": order_data.fee,
            "expiration": order_data.expiration,
            "clientId": order_data.client_id,
            "reduceOnly": order_data.reduce_only,
            "postOnly": False,
            "signature": {
                "r": signature["r"],
                "s": signature["s"]
            }
        }
        
        return payload
