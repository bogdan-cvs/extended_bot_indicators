"""
Configuration module for Extended MM Bot.
Supports multiple environments (testnet/mainnet) and profiles.
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional
import yaml
from dotenv import load_dotenv


class Environment(Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


@dataclass
class EndpointConfig:
    """API endpoint configuration for each environment."""
    rest_base: str
    ws_stream: str


ENDPOINTS = {
    Environment.TESTNET: EndpointConfig(
        rest_base="https://api.starknet.sepolia.extended.exchange/api/v1",
        ws_stream="wss://starknet.sepolia.extended.exchange/stream.extended.exchange/v1"
    ),
    Environment.MAINNET: EndpointConfig(
        rest_base="https://api.starknet.extended.exchange/api/v1",
        ws_stream="wss://api.starknet.extended.exchange/stream.extended.exchange/v1"
    )
}


@dataclass
class StrategyConfig:
    """Market making strategy parameters."""
    market: str = "ETH-USDC"
    order_notional_usd: float = 5.0
    max_open_orders_per_side: int = 1
    spread_min_bps: float = 20.0
    spread_max_bps: float = 120.0
    refresh_sec: float = 5.0
    mid_move_requote_bps: float = 15.0
    pause_move_bps: float = 50.0
    pause_window_sec: float = 15.0
    cooldown_after_pause_sec: float = 45.0
    volatility_lookback_sec: float = 60.0
    volatility_spread_multiplier: float = 2.0


@dataclass
class RiskConfig:
    """Risk management parameters."""
    max_inventory_usd: float = 15.0
    max_position_notional: float = 25.0
    max_leverage: float = 1.0
    kill_switch_drawdown_usd: float = 8.0
    session_drawdown_pct: float = 5.0
    daily_drawdown_limit_usd: float = 10.0
    max_negative_fills_window: int = 5
    negative_fills_pause_threshold: int = 3
    inventory_skew_factor: float = 0.5


@dataclass
class OrderConfig:
    """Order execution parameters."""
    post_only: bool = True
    use_taker_for_risk_off: bool = False
    taker_offset_bps: float = 10.0
    order_expiration_sec: int = 3600
    cancel_before_replace: bool = True
    max_retries: int = 3
    retry_delay_sec: float = 1.0


@dataclass
class BotConfig:
    """Main bot configuration."""
    environment: Environment = Environment.TESTNET
    dry_run: bool = True
    log_level: str = "INFO"
    metrics_interval_sec: float = 60.0
    heartbeat_interval_sec: float = 30.0
    
    # Sub-configs
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    order: OrderConfig = field(default_factory=OrderConfig)
    
    # API credentials (loaded from env)
    api_key: str = ""
    stark_private_key: str = ""
    account_address: str = ""
    vault_id: str = ""
    
    @property
    def endpoints(self) -> EndpointConfig:
        return ENDPOINTS[self.environment]


def load_env_credentials() -> dict:
    """Load API credentials from environment variables."""
    load_dotenv()
    
    return {
        "api_key": os.getenv("EXTENDED_API_KEY", ""),
        "stark_private_key": os.getenv("EXTENDED_STARK_PRIVATE_KEY", ""),
        "account_address": os.getenv("EXTENDED_ACCOUNT_ADDRESS", ""),
        "vault_id": os.getenv("EXTENDED_VAULT_ID", ""),
    }


def load_profile(profile_name: str, config_dir: Path = None) -> dict:
    """Load a configuration profile from YAML file."""
    if config_dir is None:
        config_dir = Path(__file__).parent.parent / "configs"
    
    profile_path = config_dir / f"{profile_name}.yaml"
    
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")
    
    with open(profile_path, 'r') as f:
        return yaml.safe_load(f)


def create_config(
    env: str = "testnet",
    profile: str = None,
    config_path: str = None,
    overrides: dict = None
) -> BotConfig:
    """
    Create a BotConfig from environment, profile, and optional overrides.
    
    Priority (highest to lowest):
    1. CLI overrides
    2. Custom config file
    3. Named profile
    4. Default values
    """
    # Start with defaults
    config_dict = {}
    
    # Load profile if specified
    if profile:
        try:
            profile_config = load_profile(profile)
            config_dict.update(profile_config)
        except FileNotFoundError as e:
            print(f"Warning: {e}")
    
    # Load custom config file if specified
    if config_path:
        with open(config_path, 'r') as f:
            custom_config = yaml.safe_load(f)
            if custom_config:
                config_dict.update(custom_config)
    
    # Apply CLI overrides
    if overrides:
        for key, value in overrides.items():
            if value is not None:
                config_dict[key] = value
    
    # Set environment
    environment = Environment(env.lower())
    
    # Build strategy config
    strategy_dict = config_dict.get('strategy', {})
    strategy = StrategyConfig(**{
        k: v for k, v in strategy_dict.items() 
        if k in StrategyConfig.__dataclass_fields__
    })
    
    # Build risk config
    risk_dict = config_dict.get('risk', {})
    risk = RiskConfig(**{
        k: v for k, v in risk_dict.items() 
        if k in RiskConfig.__dataclass_fields__
    })
    
    # Build order config
    order_dict = config_dict.get('order', {})
    order = OrderConfig(**{
        k: v for k, v in order_dict.items() 
        if k in OrderConfig.__dataclass_fields__
    })
    
    # Load credentials from environment
    credentials = load_env_credentials()
    
    # Create final config
    config = BotConfig(
        environment=environment,
        dry_run=config_dict.get('dry_run', True),
        log_level=config_dict.get('log_level', 'INFO'),
        metrics_interval_sec=config_dict.get('metrics_interval_sec', 60.0),
        heartbeat_interval_sec=config_dict.get('heartbeat_interval_sec', 30.0),
        strategy=strategy,
        risk=risk,
        order=order,
        **credentials
    )
    
    return config


def validate_config(config: BotConfig) -> list[str]:
    """Validate configuration and return list of errors."""
    errors = []
    
    # Check credentials
    if not config.api_key:
        errors.append("EXTENDED_API_KEY not set in environment")
    if not config.stark_private_key:
        errors.append("STARK_PRIVATE_KEY not set in environment")
    
    # Check strategy parameters
    if config.strategy.spread_min_bps <= 0:
        errors.append("spread_min_bps must be positive")
    if config.strategy.spread_max_bps < config.strategy.spread_min_bps:
        errors.append("spread_max_bps must be >= spread_min_bps")
    if config.strategy.order_notional_usd <= 0:
        errors.append("order_notional_usd must be positive")
    
    # Check risk parameters
    if config.risk.max_inventory_usd <= 0:
        errors.append("max_inventory_usd must be positive")
    if config.risk.kill_switch_drawdown_usd <= 0:
        errors.append("kill_switch_drawdown_usd must be positive")
    if config.risk.max_leverage < 0:
        errors.append("max_leverage cannot be negative")
    
    # Consistency checks
    if config.strategy.order_notional_usd > config.risk.max_inventory_usd:
        errors.append("order_notional_usd should not exceed max_inventory_usd")
    
    return errors


def print_config_summary(config: BotConfig):
    """Print a summary of the configuration."""
    print("\n" + "="*60)
    print("EXTENDED MM BOT - CONFIGURATION SUMMARY")
    print("="*60)
    print(f"Environment:     {config.environment.value.upper()}")
    print(f"Dry Run:         {config.dry_run}")
    print(f"Market:          {config.strategy.market}")
    print("-"*60)
    print("STRATEGY:")
    print(f"  Order Size:    ${config.strategy.order_notional_usd:.2f}")
    print(f"  Spread:        {config.strategy.spread_min_bps:.1f} - {config.strategy.spread_max_bps:.1f} bps")
    print(f"  Refresh:       {config.strategy.refresh_sec}s")
    print(f"  Orders/Side:   {config.strategy.max_open_orders_per_side}")
    print("-"*60)
    print("RISK:")
    print(f"  Max Inventory: ${config.risk.max_inventory_usd:.2f}")
    print(f"  Max Position:  ${config.risk.max_position_notional:.2f}")
    print(f"  Kill Switch:   ${config.risk.kill_switch_drawdown_usd:.2f} drawdown")
    print(f"  Max Leverage:  {config.risk.max_leverage}x")
    print("-"*60)
    print("ORDER EXECUTION:")
    print(f"  Post Only:     {config.order.post_only}")
    print(f"  Taker Risk-Off: {config.order.use_taker_for_risk_off}")
    print("="*60 + "\n")
