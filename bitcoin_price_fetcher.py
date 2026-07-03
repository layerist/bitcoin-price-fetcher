#!/usr/bin/env python3
"""
CoinMarketCap Cryptocurrency Tracker v8

Features:
- One API request for multiple symbols
- Atomic persistent JSON cache with stale-cache reporting
- Thread-safe circuit breaker with a single HALF_OPEN probe
- Monotonic clocks for durations and recovery windows
- Retry-After support (seconds and HTTP date)
- Retry only for transient failures; permanent API errors fail fast
- Adaptive polling with gradual recovery to the configured interval
- Session recreation after transport failures or request-count threshold
- Rolling success/latency metrics with p95
- Graceful shutdown and interruptible waits
- Optional proxy, cache path, log path, and CLI overrides
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping, Sequence

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
DEFAULT_CACHE_FILE = "cmc_price_cache.json"


@dataclass(slots=True)
class Config:
    api_key: str
    api_url: str = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
    default_symbols: tuple[str, ...] = ("BTC",)
    default_convert: str = "USD"
    default_interval: float = 60.0
    min_interval: float = 5.0
    max_interval: float = 3600.0
    interval_step: float = 5.0
    request_timeout: tuple[float, float] = (5.0, 15.0)
    max_retries: int = 5
    backoff_factor: float = 1.5
    max_backoff: float = 60.0
    failure_threshold: int = 5
    recovery_time: float = 120.0
    session_refresh_every: int = 500
    metrics_window: int = 100
    adaptive_interval: bool = True
    health_log_every: int = 10
    cache_file: Path = Path(DEFAULT_CACHE_FILE)
    cache_max_age: float = 86400.0
    log_file: Path = field(default_factory=lambda: Path("cmc_tracker.log"))
    max_log_size: int = 10 * 1024 * 1024
    backup_count: int = 5
    jitter_mode: str = "full"
    proxy_url: str | None = None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    api_key = os.getenv("CMC_API_KEY", "").strip()

    proxy = os.getenv("CMC_PROXY_URL", "").strip() or None
    return Config(
        api_key=api_key,
        cache_file=Path(os.getenv("CMC_CACHE_FILE", DEFAULT_CACHE_FILE)),
        log_file=Path(os.getenv("CMC_LOG_FILE", "cmc_tracker.log")),
        adaptive_interval=env_bool("CMC_ADAPTIVE_INTERVAL", True),
        proxy_url=proxy,
    )


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[96m",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[95m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
            return message
        return f"{self.COLORS.get(record.levelno, '')}{message}{self.RESET}"


def setup_logging(config: Config, level: str) -> logging.Logger:
    logger = logging.getLogger("cmc_tracker")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    file_formatter = logging.Formatter(fmt)
    console_formatter = ColorFormatter(fmt)

    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.max_log_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    ok: int
    failed: int
    success_rate: float
    avg_latency: float
    p95_latency: float


class Metrics:
    def __init__(self, window: int) -> None:
        self._lock = threading.Lock()
        self._ok = 0
        self._failed = 0
        self._latencies: deque[float] = deque(maxlen=max(1, window))

    def record(self, success: bool, latency: float) -> None:
        with self._lock:
            self._ok += int(success)
            self._failed += int(not success)
            self._latencies.append(max(0.0, latency))

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            values = sorted(self._latencies)
            total = self._ok + self._failed
            p95_index = max(0, min(len(values) - 1, int(len(values) * 0.95) - 1))
            return MetricsSnapshot(
                ok=self._ok,
                failed=self._failed,
                success_rate=(self._ok / total * 100.0) if total else 0.0,
                avg_latency=fmean(values) if values else 0.0,
                p95_latency=values[p95_index] if values else 0.0,
            )

    def summary(self) -> str:
        snap = self.snapshot()
        return (
            f"success={snap.success_rate:.1f}% avg={snap.avg_latency:.2f}s "
            f"p95={snap.p95_latency:.2f}s ok={snap.ok} fail={snap.failed}"
        )


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_time: float) -> None:
        if threshold < 1 or recovery_time <= 0:
            raise ValueError("Invalid circuit breaker configuration")
        self._threshold = threshold
        self._recovery_time = recovery_time
        self._lock = threading.Lock()
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._probe_in_flight = False

    def acquire_permission(self) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True, 0.0

            if self._state is CircuitState.OPEN:
                remaining = self._recovery_time - (now - self._opened_at)
                if remaining > 0:
                    return False, remaining
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False

            if self._probe_in_flight:
                return False, self._recovery_time

            self._probe_in_flight = True
            return True, 0.0

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    def record_failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self._threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now

    def snapshot(self) -> tuple[CircuitState, int]:
        with self._lock:
            return self._state, self._failures


@dataclass(frozen=True, slots=True)
class PricePoint:
    symbol: str
    convert: str
    price: Decimal
    fetched_at: str


class PriceCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, PricePoint]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                result: dict[str, PricePoint] = {}
                for key, item in raw.get("prices", {}).items():
                    result[key] = PricePoint(
                        symbol=str(item["symbol"]),
                        convert=str(item["convert"]),
                        price=Decimal(str(item["price"])),
                        fetched_at=str(item["fetched_at"]),
                    )
                return result
            except FileNotFoundError:
                return {}
            except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
                return {}

    def save(self, prices: Mapping[str, PricePoint]) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "prices": {
                key: {**asdict(point), "price": str(point.price)}
                for key, point in prices.items()
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(encoded, encoding="utf-8")
            os.replace(tmp, self.path)


class RetryableError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentAPIError(RuntimeError):
    pass


def create_session(config: Config) -> Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "X-CMC_PRO_API_KEY": config.api_key,
            "User-Agent": "cmc-tracker/8.0",
        }
    )
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
    session.mount("https://", adapter)
    if config.proxy_url:
        session.proxies.update({"http": config.proxy_url, "https": config.proxy_url})
    return session


def compute_backoff(attempt: int, config: Config) -> float:
    base = min(config.backoff_factor * (2 ** max(0, attempt - 1)), config.max_backoff)
    if config.jitter_mode == "full":
        return random.uniform(0.0, base)
    if config.jitter_mode == "equal":
        return base / 2.0 + random.uniform(0.0, base / 2.0)
    return base


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def parse_prices(payload: Mapping[str, Any], symbols: Sequence[str], convert: str) -> dict[str, Decimal]:
    status = payload.get("status")
    if not isinstance(status, Mapping):
        raise PermanentAPIError("API response has no valid status object")

    error_code = status.get("error_code", 0)
    if error_code not in (0, "0", None):
        raise PermanentAPIError(str(status.get("error_message") or f"CMC error {error_code}"))

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise PermanentAPIError("API response has no valid data object")

    prices: dict[str, Decimal] = {}
    missing: list[str] = []
    for symbol in symbols:
        try:
            raw_price = data[symbol]["quote"][convert]["price"]
            value = Decimal(str(raw_price))
            if not value.is_finite() or value < 0:
                raise InvalidOperation
            prices[symbol] = value
        except (KeyError, TypeError, InvalidOperation, ValueError):
            missing.append(symbol)

    if missing:
        raise PermanentAPIError(f"Missing or invalid price data for: {', '.join(missing)}")
    return prices


def decode_response(response: Response, symbols: Sequence[str], convert: str) -> dict[str, Decimal]:
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableError(f"HTTP {response.status_code}", retry_after)
    if not 200 <= response.status_code < 300:
        detail = response.text[:300].replace("\n", " ")
        raise PermanentAPIError(f"HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RetryableError("Invalid JSON response") from exc
    if not isinstance(payload, Mapping):
        raise PermanentAPIError("Unexpected JSON root type")
    return parse_prices(payload, symbols, convert)


def fetch_prices(
    session: Session,
    config: Config,
    symbols: Sequence[str],
    convert: str,
    breaker: CircuitBreaker,
    metrics: Metrics,
    logger: logging.Logger,
    stop_event: threading.Event,
) -> tuple[dict[str, Decimal] | None, float | None, bool]:
    allowed, retry_in = breaker.acquire_permission()
    if not allowed:
        logger.warning("Circuit breaker OPEN; next probe in %.1fs", retry_in)
        return None, retry_in, False

    params = {"symbol": ",".join(symbols), "convert": convert}
    recreate_session = False

    for attempt in range(1, config.max_retries + 1):
        started = time.perf_counter()
        try:
            response = session.get(config.api_url, params=params, timeout=config.request_timeout)
            latency = time.perf_counter() - started
            prices = decode_response(response, symbols, convert)
            metrics.record(True, latency)
            breaker.record_success()
            logger.debug(
                "HTTP %s in %.3fs | remaining=%s",
                response.status_code,
                latency,
                response.headers.get("X-RateLimit-Remaining", "?"),
            )
            return prices, None, recreate_session

        except PermanentAPIError as exc:
            latency = time.perf_counter() - started
            metrics.record(False, latency)
            breaker.record_failure()
            logger.error("Permanent API error: %s", exc)
            return None, None, recreate_session

        except RetryableError as exc:
            latency = time.perf_counter() - started
            metrics.record(False, latency)
            breaker.record_failure()
            wait = exc.retry_after if exc.retry_after is not None else compute_backoff(attempt, config)
            logger.warning("Attempt %d/%d failed: %s; retry in %.2fs", attempt, config.max_retries, exc, wait)

        except (Timeout, RequestsConnectionError, RequestException) as exc:
            latency = time.perf_counter() - started
            metrics.record(False, latency)
            breaker.record_failure()
            recreate_session = True
            wait = compute_backoff(attempt, config)
            logger.warning("Attempt %d/%d transport error: %s; retry in %.2fs", attempt, config.max_retries, exc, wait)

        if attempt < config.max_retries and stop_event.wait(wait):
            return None, None, recreate_session

    return None, None, recreate_session


def cache_key(symbol: str, convert: str) -> str:
    return f"{symbol}/{convert}"


def parse_iso_age(timestamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def format_price(price: Decimal) -> str:
    if price >= Decimal("1"):
        quant = Decimal("0.01")
    elif price >= Decimal("0.01"):
        quant = Decimal("0.000001")
    else:
        quant = Decimal("0.0000000001")
    return f"{price.quantize(quant, rounding=ROUND_HALF_UP):,f}"


def install_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(signum: int, _frame: object) -> None:
        logger.info("Shutdown requested by signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def track_prices(
    config: Config,
    symbols: Sequence[str],
    convert: str,
    target_interval: float,
    logger: logging.Logger,
) -> None:
    stop_event = threading.Event()
    install_signal_handlers(stop_event, logger)

    cache = PriceCache(config.cache_file)
    cached = cache.load()
    last_prices: dict[str, Decimal] = {
        symbol: cached[cache_key(symbol, convert)].price
        for symbol in symbols
        if cache_key(symbol, convert) in cached
    }

    session = create_session(config)
    breaker = CircuitBreaker(config.failure_threshold, config.recovery_time)
    metrics = Metrics(config.metrics_window)
    current_interval = target_interval
    cycle = 0
    session_requests = 0

    logger.info("Tracking %s in %s every %.1fs", ",".join(symbols), convert, target_interval)

    try:
        while not stop_event.is_set():
            cycle += 1
            if session_requests >= config.session_refresh_every:
                session.close()
                session = create_session(config)
                session_requests = 0
                logger.debug("HTTP session refreshed")

            prices, retry_after, recreate = fetch_prices(
                session, config, symbols, convert, breaker, metrics, logger, stop_event
            )
            session_requests += 1

            if recreate:
                session.close()
                session = create_session(config)
                session_requests = 0

            if prices:
                now_iso = datetime.now(timezone.utc).isoformat()
                for symbol, price in prices.items():
                    previous = last_prices.get(symbol)
                    delta = ""
                    if previous is not None and previous != 0:
                        pct = (price - previous) / previous * Decimal("100")
                        arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
                        delta = f" ({arrow} {pct:+.3f}%)"
                    logger.info("[%s/%s] %s %s%s", symbol, convert, format_price(price), convert, delta)
                    last_prices[symbol] = price
                    cached[cache_key(symbol, convert)] = PricePoint(symbol, convert, price, now_iso)

                try:
                    cache.save(cached)
                except OSError as exc:
                    logger.warning("Could not save cache: %s", exc)

                if config.adaptive_interval:
                    current_interval = max(target_interval, current_interval - config.interval_step)
                else:
                    current_interval = target_interval
            else:
                for symbol in symbols:
                    point = cached.get(cache_key(symbol, convert))
                    if point is None:
                        logger.error("[%s/%s] No current or cached price", symbol, convert)
                        continue
                    age = parse_iso_age(point.fetched_at)
                    stale = age is None or age > config.cache_max_age
                    age_text = "unknown" if age is None else f"{age:.0f}s"
                    logger.warning(
                        "[%s/%s] Cached price %s %s (age=%s%s)",
                        symbol,
                        convert,
                        format_price(point.price),
                        convert,
                        age_text,
                        ", stale" if stale else "",
                    )

                if config.adaptive_interval:
                    suggested = retry_after or (current_interval + config.interval_step)
                    current_interval = min(config.max_interval, max(config.min_interval, suggested))

            if cycle % config.health_log_every == 0:
                state, failures = breaker.snapshot()
                logger.info(
                    "Health | interval=%.1fs breaker=%s failures=%d | %s",
                    current_interval,
                    state.value,
                    failures,
                    metrics.summary(),
                )

            stop_event.wait(current_interval)
    finally:
        session.close()
        logger.info("Tracker stopped | %s", metrics.summary())


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("At least one symbol is required")
    if len(symbols) > 100:
        raise argparse.ArgumentTypeError("Too many symbols; maximum is 100")
    return symbols


def parse_arguments(config: Config) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reliable CoinMarketCap multi-symbol price tracker")
    parser.add_argument("--symbols", type=parse_symbols, default=config.default_symbols, help="Comma-separated symbols, e.g. BTC,ETH,SOL")
    parser.add_argument("--convert", default=config.default_convert, help="Quote currency, e.g. USD or EUR")
    parser.add_argument("--interval", type=float, default=config.default_interval)
    parser.add_argument("--cache-file", type=Path, default=config.cache_file)
    parser.add_argument("--log-file", type=Path, default=config.log_file)
    parser.add_argument("--no-adaptive", action="store_true", help="Disable adaptive interval")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()

    args.convert = args.convert.strip().upper()
    if not args.convert:
        parser.error("--convert cannot be empty")
    if not config.min_interval <= args.interval <= config.max_interval:
        parser.error(f"--interval must be between {config.min_interval} and {config.max_interval}")
    return args


def main() -> int:
    config = load_config()
    args = parse_arguments(config)
    if not config.api_key:
        print("Missing environment variable: CMC_API_KEY", file=sys.stderr)
        return 2
    config.cache_file = args.cache_file
    config.log_file = args.log_file
    config.adaptive_interval = not args.no_adaptive
    logger = setup_logging(config, args.log_level)

    try:
        track_prices(config, args.symbols, args.convert, args.interval, logger)
        return 0
    except Exception:
        logger.exception("Unhandled fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
