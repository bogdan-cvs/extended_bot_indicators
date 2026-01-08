"""
Metrics and logging module for Extended MM Bot.
Handles structured logging, periodic reporting, and performance tracking.
"""

import json
import logging
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Optional, Any
from pathlib import Path

from .config import BotConfig


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add extra fields if present
        if hasattr(record, "extra_data"):
            log_obj["data"] = record.extra_data
        
        # Add exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_obj)


def setup_logging(
    log_level: str = "INFO",
    json_format: bool = True,
    log_file: Optional[str] = None
):
    """
    Setup logging configuration.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        json_format: Use JSON formatting
        log_file: Optional log file path
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    if json_format:
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
        )
    root_logger.addHandler(console_handler)
    
    # File handler (optional) - uses same format as console
    if log_file:
        # Use line buffering (flush after each line) for real-time logging
        file_stream = open(log_file, 'a', encoding='utf-8', buffering=1)
        file_handler = logging.StreamHandler(file_stream)
        if json_format:
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )
            )
        root_logger.addHandler(file_handler)
    
    # Reduce noise from libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


@dataclass
class StrategyMetrics:
    """Metrics for strategy performance."""
    quotes_placed: int = 0
    quotes_cancelled: int = 0
    fills_count: int = 0
    maker_fills: int = 0
    taker_fills: int = 0
    requotes_mid_move: int = 0
    requotes_refresh: int = 0
    pauses_triggered: int = 0
    
    # Timing
    avg_quote_latency_ms: float = 0.0
    last_quote_time: float = 0.0
    
    # Quality
    uptime_sec: float = 0.0
    quote_uptime_pct: float = 0.0


@dataclass
class PnLMetrics:
    """Profit and loss metrics."""
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    fees_paid: float = 0.0
    gross_pnl: float = 0.0

    # Trade stats
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0

    # Drawdown
    session_high_water: float = 0.0
    current_drawdown: float = 0.0
    max_drawdown: float = 0.0


@dataclass
class InventoryMetrics:
    """Inventory and position metrics."""
    position_size: float = 0.0
    position_notional: float = 0.0
    inventory_usd: float = 0.0
    inventory_pct: float = 0.0
    skew_factor: float = 0.0
    account_balance: float = 0.0  # Account balance in USD


@dataclass
class MarketMetrics:
    """Market data metrics."""
    mid_price: float = 0.0
    spread_bps: float = 0.0
    volatility_bps: float = 0.0
    bid_liquidity: float = 0.0
    ask_liquidity: float = 0.0
    trade_imbalance: float = 0.0


@dataclass
class SystemMetrics:
    """System health metrics."""
    ws_connected: bool = False
    ws_messages: int = 0
    api_requests: int = 0
    api_errors: int = 0
    rate_limit_hits: int = 0
    last_heartbeat: float = 0.0


@dataclass
class BotMetrics:
    """Complete bot metrics snapshot."""
    timestamp: float = 0.0
    environment: str = ""
    market: str = ""
    state: str = ""
    dry_run: bool = False
    
    strategy: StrategyMetrics = field(default_factory=StrategyMetrics)
    pnl: PnLMetrics = field(default_factory=PnLMetrics)
    inventory: InventoryMetrics = field(default_factory=InventoryMetrics)
    market_data: MarketMetrics = field(default_factory=MarketMetrics)
    system: SystemMetrics = field(default_factory=SystemMetrics)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return asdict(self)
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)


class MetricsCollector:
    """
    Collects and aggregates metrics from all bot components.
    
    Features:
    - Periodic metrics snapshots
    - PnL tracking
    - Performance reporting
    - JSON logging
    """
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.logger = logging.getLogger("metrics")
        
        # Current metrics
        self.current = BotMetrics(
            environment=config.environment.value,
            market=config.strategy.market,
            dry_run=config.dry_run
        )
        
        # Timing
        self.start_time = time.time()
        self._last_report_time = 0.0
        
        # Tracking
        self._quote_latencies: list[float] = []
        self._pnl_history: list[tuple[float, float]] = []  # (timestamp, pnl)
    
    def update_state(self, state: str):
        """Update bot state."""
        self.current.state = state
        self.current.timestamp = time.time()
    
    def record_quote_placed(self, latency_ms: float = 0.0):
        """Record a quote placement."""
        self.current.strategy.quotes_placed += 1
        self.current.strategy.last_quote_time = time.time()
        
        if latency_ms > 0:
            self._quote_latencies.append(latency_ms)
            # Keep last 100
            self._quote_latencies = self._quote_latencies[-100:]
            self.current.strategy.avg_quote_latency_ms = (
                sum(self._quote_latencies) / len(self._quote_latencies)
            )
    
    def record_quote_cancelled(self):
        """Record a quote cancellation."""
        self.current.strategy.quotes_cancelled += 1
    
    def record_fill(self, is_maker: bool):
        """Record a fill."""
        self.current.strategy.fills_count += 1
        if is_maker:
            self.current.strategy.maker_fills += 1
        else:
            self.current.strategy.taker_fills += 1
    
    def record_requote(self, reason: str):
        """Record a requote event."""
        if reason == "mid_move":
            self.current.strategy.requotes_mid_move += 1
        elif reason == "refresh":
            self.current.strategy.requotes_refresh += 1
    
    def record_pause(self):
        """Record a pause event."""
        self.current.strategy.pauses_triggered += 1
    
    def update_pnl(
        self,
        realized: float,
        unrealized: float,
        fees: float,
        total_trades: int = 0,
        winning_trades: int = 0,
        win_rate: float = 0.0
    ):
        """Update PnL metrics."""
        self.current.pnl.realized_pnl = realized
        self.current.pnl.unrealized_pnl = unrealized
        self.current.pnl.total_pnl = realized + unrealized
        self.current.pnl.fees_paid = fees
        self.current.pnl.gross_pnl = realized + unrealized + fees

        # Trade stats
        self.current.pnl.total_trades = total_trades
        self.current.pnl.winning_trades = winning_trades
        self.current.pnl.losing_trades = total_trades - winning_trades
        self.current.pnl.win_rate = win_rate

        # Track high water mark and drawdown
        total = self.current.pnl.total_pnl
        if total > self.current.pnl.session_high_water:
            self.current.pnl.session_high_water = total

        self.current.pnl.current_drawdown = self.current.pnl.session_high_water - total
        if self.current.pnl.current_drawdown > self.current.pnl.max_drawdown:
            self.current.pnl.max_drawdown = self.current.pnl.current_drawdown

        # Record history
        self._pnl_history.append((time.time(), total))
        # Keep last hour
        cutoff = time.time() - 3600
        self._pnl_history = [(t, p) for t, p in self._pnl_history if t > cutoff]
    
    def update_inventory(
        self,
        position_size: float,
        position_notional: float,
        inventory_usd: float,
        max_inventory_usd: float,
        skew_factor: float,
        account_balance: float = 0.0
    ):
        """Update inventory metrics."""
        self.current.inventory.position_size = position_size
        self.current.inventory.position_notional = position_notional
        self.current.inventory.inventory_usd = inventory_usd
        self.current.inventory.inventory_pct = (
            (inventory_usd / max_inventory_usd * 100)
            if max_inventory_usd > 0 else 0
        )
        self.current.inventory.skew_factor = skew_factor
        self.current.inventory.account_balance = account_balance
    
    def update_market(
        self,
        mid_price: float,
        spread_bps: float,
        volatility_bps: float,
        bid_liquidity: float = 0.0,
        ask_liquidity: float = 0.0,
        trade_imbalance: float = 0.0
    ):
        """Update market metrics."""
        self.current.market_data.mid_price = mid_price
        self.current.market_data.spread_bps = spread_bps
        self.current.market_data.volatility_bps = volatility_bps
        self.current.market_data.bid_liquidity = bid_liquidity
        self.current.market_data.ask_liquidity = ask_liquidity
        self.current.market_data.trade_imbalance = trade_imbalance
    
    def update_system(
        self,
        ws_connected: bool,
        ws_messages: int,
        api_requests: int,
        api_errors: int,
        rate_limit_hits: int
    ):
        """Update system metrics."""
        self.current.system.ws_connected = ws_connected
        self.current.system.ws_messages = ws_messages
        self.current.system.api_requests = api_requests
        self.current.system.api_errors = api_errors
        self.current.system.rate_limit_hits = rate_limit_hits
        self.current.system.last_heartbeat = time.time()
    
    def update_uptime(self, quote_uptime_pct: float):
        """Update uptime metrics."""
        self.current.strategy.uptime_sec = time.time() - self.start_time
        self.current.strategy.quote_uptime_pct = quote_uptime_pct
    
    def get_snapshot(self) -> BotMetrics:
        """Get current metrics snapshot."""
        self.current.timestamp = time.time()
        return self.current
    
    def should_report(self, interval_sec: float = None) -> bool:
        """Check if it's time for a periodic report."""
        interval = interval_sec or self.config.metrics_interval_sec
        return time.time() - self._last_report_time >= interval
    
    def report(self):
        """Generate and log a metrics report."""
        self._last_report_time = time.time()
        snapshot = self.get_snapshot()
        
        # Log structured metrics
        self.logger.info(
            "Metrics report",
            extra={"extra_data": snapshot.to_dict()}
        )
        
        # Also print human-readable summary
        self._print_summary(snapshot)
    
    def _print_summary(self, metrics: BotMetrics):
        """Print human-readable metrics summary."""
        # Build report as a single string for logging
        state_icon = "[ON]" if metrics.state == "running" else "[PAUSE]" if metrics.state == "paused" else "[OFF]"
        pnl_icon = "[+]" if metrics.pnl.total_pnl >= 0 else "[-]"
        ws_icon = "[WS:ON]" if metrics.system.ws_connected else "[WS:OFF]"

        report_lines = [
            "",
            "=" * 60,
            f"[METRICS] REPORT - {datetime.now().strftime('%H:%M:%S')}",
            "=" * 60,
            f"{state_icon} State: {metrics.state.upper()} | Market: {metrics.market}",
            "",
            f"{pnl_icon} PnL:",
            f"   Realized:   ${metrics.pnl.realized_pnl:+.4f}",
            f"   Unrealized: ${metrics.pnl.unrealized_pnl:+.4f}",
            f"   Total:      ${metrics.pnl.total_pnl:+.4f}",
            f"   Fees:       ${metrics.pnl.fees_paid:.4f}",
            f"   Drawdown:   ${metrics.pnl.current_drawdown:.4f} (max: ${metrics.pnl.max_drawdown:.4f})",
            f"   Trades:     {metrics.pnl.total_trades} (W:{metrics.pnl.winning_trades} L:{metrics.pnl.losing_trades} | {metrics.pnl.win_rate:.1f}%)",
            "",
            "[INV] Inventory:",
            f"   Balance:    ${metrics.inventory.account_balance:.2f}",
            f"   Position:   {metrics.inventory.position_size:+.6f}",
            f"   Notional:   ${metrics.inventory.position_notional:.2f}",
            f"   Inventory:  ${metrics.inventory.inventory_usd:.2f} ({metrics.inventory.inventory_pct:.1f}%)",
            f"   Skew:       {metrics.inventory.skew_factor:+.3f}",
            "",
            "[MKT] Market:",
            f"   Mid Price:  ${metrics.market_data.mid_price:.4f}",
            f"   Spread:     {metrics.market_data.spread_bps:.2f} bps",
            f"   Volatility: {metrics.market_data.volatility_bps:.2f} bps",
            "",
            "[STR] Strategy:",
            f"   Quotes:     {metrics.strategy.quotes_placed} placed, {metrics.strategy.quotes_cancelled} cancelled",
            f"   Fills:      {metrics.strategy.fills_count} (maker: {metrics.strategy.maker_fills})",
            f"   Requotes:   {metrics.strategy.requotes_mid_move} (mid), {metrics.strategy.requotes_refresh} (refresh)",
            f"   Pauses:     {metrics.strategy.pauses_triggered}",
            "",
            f"{ws_icon} System:",
            f"   WS Messages: {metrics.system.ws_messages}",
            f"   API Requests: {metrics.system.api_requests}",
            f"   Errors: {metrics.system.api_errors}",
            f"   Uptime: {metrics.strategy.uptime_sec/60:.1f} min",
            "=" * 60,
            ""
        ]

        report = "\n".join(report_lines)

        # Print to console (for terminal display)
        print(report)

        # Also log it (for file logging)
        logger.info("Metrics report" + report)
    
    def get_pnl_summary(self) -> dict:
        """Get PnL summary for logging."""
        return {
            "realized": self.current.pnl.realized_pnl,
            "unrealized": self.current.pnl.unrealized_pnl,
            "total": self.current.pnl.total_pnl,
            "fees": self.current.pnl.fees_paid,
            "drawdown": self.current.pnl.current_drawdown,
        }


def create_metrics_logger(config: BotConfig) -> MetricsCollector:
    """Create and configure metrics collector."""
    setup_logging(
        log_level=config.log_level,
        json_format=True,
        log_file=f"logs/mm_bot_{config.environment.value}_{int(time.time())}.log"
    )
    
    # Create logs directory
    Path("logs").mkdir(exist_ok=True)
    
    return MetricsCollector(config)
