#!/usr/bin/env python3
"""
CoinMarketCap Cryptocurrency Tracker v10

Improvements over v8:
- Counts real HTTP attempts instead of polling cycles.
- Circuit breaker records one final outcome per polling operation, not every retry.
- Permanent configuration/authentication/API errors do not poison the circuit breaker.
- Supports partial symbol responses: valid prices are preserved and missing symbols are reported.
- Rich environment and CLI configuration with strict validation.
- Safer atomic cache writes with fsync and orphan temporary-file cleanup.
- Cache schema validation and optional pruning of old entries.
- Correct nearest-rank p95 calculation and separate operation/request metrics.
- Honors Retry-After while capping unreasonable delays.
- Recreates the HTTP session after an operation that encountered transport failures.
- Uses fixed-rate polling semantics to avoid adding request duration to every interval.
- Optional one-shot and JSON output modes for automation.
- Graceful shutdown with interruptible retries and sleeps.
- Keeps --json stdout machine-clean by routing logs to stderr.
- Stops immediately on permanent API/auth/input errors instead of hammering the API forever.
- Validates cache version/key/timestamps and fsyncs the cache directory after atomic replace.

Required environment variable:
    CMC_API_KEY
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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
from requests.exceptions import RequestException


APP_NAME = "cmc-tracker"
APP_VERSION = "10.0"
DEFAULT_API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_CACHE_FILE = "cmc_price_cache.json"
DEFAULT_LOG_FILE = "cmc_tracker.log"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
PERMANENT_AUTH_STATUS = frozenset({401, 403})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """Invalid local configuration."""


class RetryableError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentAPIError(RuntimeError):
    """A request cannot succeed by retrying with the same inputs."""


@dataclass(slots=True)
class Config:
    api_key: str
    api_url: str = DEFAULT_API_URL
    default_symbols: tuple[str, ...] = ("BTC",)
    default_convert: str = "USD"
    default_interval: float = 60.0
    min_interval: float = 5.0
    max_interval: float = 3600.0
    interval_step: float = 5.0
    connect_timeout: float = 5.0
    read_timeout: float = 15.0
    max_retries: int = 5
    backoff_factor: float = 1.5
    max_backoff: float = 60.0
    max_retry_after: float = 3600.0
    failure_threshold: int = 5
    recovery_time: float = 120.0
    session_refresh_every: int = 500
    metrics_window: int = 100
    adaptive_interval: bool = True
    health_log_every: int = 10
    cache_file: Path = Path(DEFAULT_CACHE_FILE)
    cache_max_age: float = 86400.0
    cache_prune_age: float = 30 * 86400.0
    log_file: Path = field(default_factory=lambda: Path(DEFAULT_LOG_FILE))
    max_log_size: int = 10 * 1024 * 1024
    backup_count: int = 5
    jitter_mode: str = "full"
    proxy_url: str | None = None
    verify_tls: bool = True

    @property
    def timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout

    def validate(self, *, require_api_key: bool = True) -> None:
        if require_api_key and not self.api_key:
            raise ConfigError("Missing environment variable: CMC_API_KEY")
        if not self.api_url.startswith(("https://", "http://")):
            raise ConfigError("CMC_API_URL must start with http:// or https://")
        positive_float_fields = {
            "default_interval": self.default_interval,
            "min_interval": self.min_interval,
            "max_interval": self.max_interval,
            "interval_step": self.interval_step,
            "connect_timeout": self.connect_timeout,
            "read_timeout": self.read_timeout,
            "backoff_factor": self.backoff_factor,
            "max_backoff": self.max_backoff,
            "max_retry_after": self.max_retry_after,
            "recovery_time": self.recovery_time,
            "cache_max_age": self.cache_max_age,
            "cache_prune_age": self.cache_prune_age,
        }
        for name, value in positive_float_fields.items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be a finite positive number")
        positive_int_fields = {
            "max_retries": self.max_retries,
            "failure_threshold": self.failure_threshold,
            "session_refresh_every": self.session_refresh_every,
            "metrics_window": self.metrics_window,
            "health_log_every": self.health_log_every,
            "max_log_size": self.max_log_size,
        }
        for name, value in positive_int_fields.items():
            if value < 1:
                raise ConfigError(f"{name} must be at least 1")
        if self.backup_count < 0:
            raise ConfigError("backup_count cannot be negative")
        if self.min_interval > self.max_interval:
            raise ConfigError("min_interval cannot exceed max_interval")
        if not self.min_interval <= self.default_interval <= self.max_interval:
            raise ConfigError("default_interval must be within min_interval and max_interval")
        if self.jitter_mode not in {"none", "full", "equal"}:
            raise ConfigError("jitter_mode must be one of: none, full, equal")


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be a boolean value")


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def load_config() -> Config:
    config = Config(
        api_key=os.getenv("CMC_API_KEY", "").strip(),
        api_url=os.getenv("CMC_API_URL", DEFAULT_API_URL).strip(),
        default_interval=env_float("CMC_INTERVAL", 60.0, 0.001),
        min_interval=env_float("CMC_MIN_INTERVAL", 5.0, 0.001),
        max_interval=env_float("CMC_MAX_INTERVAL", 3600.0, 0.001),
        interval_step=env_float("CMC_INTERVAL_STEP", 5.0, 0.001),
        connect_timeout=env_float("CMC_CONNECT_TIMEOUT", 5.0, 0.001),
        read_timeout=env_float("CMC_READ_TIMEOUT", 15.0, 0.001),
        max_retries=env_int("CMC_MAX_RETRIES", 5, 1),
        backoff_factor=env_float("CMC_BACKOFF_FACTOR", 1.5, 0.001),
        max_backoff=env_float("CMC_MAX_BACKOFF", 60.0, 0.001),
        max_retry_after=env_float("CMC_MAX_RETRY_AFTER", 3600.0, 0.001),
        failure_threshold=env_int("CMC_FAILURE_THRESHOLD", 5, 1),
        recovery_time=env_float("CMC_RECOVERY_TIME", 120.0, 0.001),
        session_refresh_every=env_int("CMC_SESSION_REFRESH_EVERY", 500, 1),
        metrics_window=env_int("CMC_METRICS_WINDOW", 100, 1),
        adaptive_interval=env_bool("CMC_ADAPTIVE_INTERVAL", True),
        health_log_every=env_int("CMC_HEALTH_LOG_EVERY", 10, 1),
        cache_file=Path(os.getenv("CMC_CACHE_FILE", DEFAULT_CACHE_FILE)).expanduser(),
        cache_max_age=env_float("CMC_CACHE_MAX_AGE", 86400.0, 0.001),
        cache_prune_age=env_float("CMC_CACHE_PRUNE_AGE", 30 * 86400.0, 0.001),
        log_file=Path(os.getenv("CMC_LOG_FILE", DEFAULT_LOG_FILE)).expanduser(),
        max_log_size=env_int("CMC_MAX_LOG_SIZE", 10 * 1024 * 1024, 1),
        backup_count=env_int("CMC_LOG_BACKUPS", 5, 0),
        jitter_mode=os.getenv("CMC_JITTER_MODE", "full").strip().lower(),
        proxy_url=os.getenv("CMC_PROXY_URL", "").strip() or None,
        verify_tls=env_bool("CMC_VERIFY_TLS", True),
    )
    config.validate(require_api_key=False)
    return config


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[96m",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[95m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, stream: object) -> None:
        super().__init__(fmt)
        self.stream = stream

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        if not is_tty or os.getenv("NO_COLOR"):
            return message
        return f"{self.COLORS.get(record.levelno, '')}{message}{self.RESET}"


def setup_logging(config: Config, level: str, *, console_to_stderr: bool = False) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.max_log_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt))

    console_stream = sys.stderr if console_to_stderr else sys.stdout
    console_handler = logging.StreamHandler(console_stream)
    console_handler.setFormatter(ColorFormatter(fmt, console_stream))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    operations_ok: int
    operations_failed: int
    requests: int
    success_rate: float
    avg_latency: float
    p95_latency: float


class Metrics:
    def __init__(self, window: int) -> None:
        self._lock = threading.Lock()
        self._operations_ok = 0
        self._operations_failed = 0
        self._requests = 0
        self._latencies: deque[float] = deque(maxlen=max(1, window))

    def record_request(self, latency: float) -> None:
        with self._lock:
            self._requests += 1
            self._latencies.append(max(0.0, latency))

    def record_operation(self, success: bool) -> None:
        with self._lock:
            self._operations_ok += int(success)
            self._operations_failed += int(not success)

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            values = sorted(self._latencies)
            total = self._operations_ok + self._operations_failed
            p95_index = max(0, math.ceil(len(values) * 0.95) - 1) if values else 0
            return MetricsSnapshot(
                operations_ok=self._operations_ok,
                operations_failed=self._operations_failed,
                requests=self._requests,
                success_rate=(self._operations_ok / total * 100.0) if total else 0.0,
                avg_latency=fmean(values) if values else 0.0,
                p95_latency=values[p95_index] if values else 0.0,
            )

    def summary(self) -> str:
        snap = self.snapshot()
        return (
            f"success={snap.success_rate:.1f}% avg={snap.avg_latency:.2f}s "
            f"p95={snap.p95_latency:.2f}s ops_ok={snap.operations_ok} "
            f"ops_fail={snap.operations_failed} requests={snap.requests}"
        )


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_time: float) -> None:
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

    def cancel_probe(self) -> None:
        with self._lock:
            self._probe_in_flight = False

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
    VERSION = 2

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()

    def _cleanup_orphan_temps(self) -> None:
        parent = self.path.parent
        pattern = f".{self.path.name}.*.tmp"
        try:
            for candidate in parent.glob(pattern):
                try:
                    if candidate.is_file():
                        candidate.unlink()
                except OSError:
                    self.logger.debug("Could not remove orphan cache temp %s", candidate)
        except OSError:
            pass

    def load(self) -> dict[str, PricePoint]:
        with self._lock:
            self._cleanup_orphan_temps()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, Mapping):
                    raise ValueError("cache root must be an object")
                version = raw.get("version")
                if version != self.VERSION:
                    raise ValueError(f"unsupported cache version: {version!r}")
                raw_prices = raw.get("prices", {})
                if not isinstance(raw_prices, Mapping):
                    raise ValueError("cache prices must be an object")
                result: dict[str, PricePoint] = {}
                for key, item in raw_prices.items():
                    if not isinstance(key, str) or not isinstance(item, Mapping):
                        continue
                    try:
                        price = Decimal(str(item["price"]))
                        if not price.is_finite() or price < 0:
                            continue
                        symbol = str(item["symbol"]).upper()
                        convert = str(item["convert"]).upper()
                        fetched_at = str(item["fetched_at"])
                        if key != cache_key(symbol, convert) or parse_iso_age(fetched_at) is None:
                            continue
                        result[key] = PricePoint(
                            symbol=symbol,
                            convert=convert,
                            price=price,
                            fetched_at=fetched_at,
                        )
                    except (KeyError, TypeError, ValueError, InvalidOperation):
                        continue
                return result
            except FileNotFoundError:
                return {}
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.logger.warning("Ignoring unreadable cache %s: %s", self.path, exc)
                return {}

    def save(self, prices: Mapping[str, PricePoint]) -> None:
        payload = {
            "version": self.VERSION,
            "updated_at": utc_now_iso(),
            "prices": {
                key: {**asdict(point), "price": str(point.price)}
                for key, point in sorted(prices.items())
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._cleanup_orphan_temps()
            try:
                with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
                try:
                    dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass


@dataclass(frozen=True, slots=True)
class FetchResult:
    prices: dict[str, Decimal]
    missing_symbols: tuple[str, ...] = ()
    retry_after: float | None = None
    permanent_error: str | None = None
    request_count: int = 0
    transport_failed: bool = False

    @property
    def success(self) -> bool:
        return bool(self.prices) and self.permanent_error is None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_session(config: Config) -> Session:
    session = requests.Session()
    session.trust_env = config.proxy_url is None
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "X-CMC_PRO_API_KEY": config.api_key,
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }
    )
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0, pool_block=True)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
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


def parse_retry_after(value: str | None, maximum: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), maximum)


def extract_api_error(payload: Mapping[str, Any]) -> str | None:
    status = payload.get("status")
    if not isinstance(status, Mapping):
        return "API response has no valid status object"
    error_code = status.get("error_code", 0)
    if error_code in (0, "0", None):
        return None
    return str(status.get("error_message") or f"CMC error {error_code}")


def parse_prices(
    payload: Mapping[str, Any], symbols: Sequence[str], convert: str
) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    error = extract_api_error(payload)
    if error:
        raise PermanentAPIError(error)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise PermanentAPIError("API response has no valid data object")

    prices: dict[str, Decimal] = {}
    missing: list[str] = []
    for symbol in symbols:
        try:
            item = data[symbol]
            if isinstance(item, list):
                if len(item) != 1:
                    raise KeyError(symbol)
                item = item[0]
            raw_price = item["quote"][convert]["price"]
            value = Decimal(str(raw_price))
            if not value.is_finite() or value < 0:
                raise InvalidOperation
            prices[symbol] = value
        except (KeyError, TypeError, InvalidOperation, ValueError):
            missing.append(symbol)
    if not prices:
        raise PermanentAPIError(f"No valid price data for: {', '.join(symbols)}")
    return prices, tuple(missing)


def decode_response(
    response: Response, symbols: Sequence[str], convert: str, config: Config
) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    retry_after = parse_retry_after(response.headers.get("Retry-After"), config.max_retry_after)
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableError(f"HTTP {response.status_code}", retry_after)
    if response.status_code in PERMANENT_AUTH_STATUS:
        raise PermanentAPIError(f"HTTP {response.status_code}: check CMC_API_KEY and API plan permissions")
    if not 200 <= response.status_code < 300:
        detail = response.text[:300].replace("\r", " ").replace("\n", " ")
        raise PermanentAPIError(f"HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RetryableError("Invalid JSON response") from exc
    if not isinstance(payload, Mapping):
        raise RetryableError("Unexpected JSON root type")
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
) -> FetchResult:
    allowed, retry_in = breaker.acquire_permission()
    if not allowed:
        logger.warning("Circuit breaker OPEN; next probe in %.1fs", retry_in)
        return FetchResult({}, retry_after=retry_in)

    params = {"symbol": ",".join(symbols), "convert": convert, "skip_invalid": "true"}
    requests_made = 0
    last_wait: float | None = None
    transport_failed = False

    for attempt in range(1, config.max_retries + 1):
        started = time.perf_counter()
        try:
            response = session.get(
                config.api_url,
                params=params,
                timeout=config.timeout,
                verify=config.verify_tls,
            )
            requests_made += 1
            latency = time.perf_counter() - started
            metrics.record_request(latency)
            prices, missing = decode_response(response, symbols, convert, config)
            metrics.record_operation(True)
            breaker.record_success()
            logger.debug(
                "HTTP %s in %.3fs | credit_count=%s | remaining=%s",
                response.status_code,
                latency,
                response.headers.get("X-RateLimit-Credit-Count", "?"),
                response.headers.get("X-RateLimit-Remaining", "?"),
            )
            return FetchResult(prices, missing, request_count=requests_made)

        except PermanentAPIError as exc:
            if requests_made == 0:
                requests_made = 1
                metrics.record_request(time.perf_counter() - started)
            metrics.record_operation(False)
            breaker.cancel_probe()
            logger.error("Permanent API error: %s", exc)
            return FetchResult({}, permanent_error=str(exc), request_count=requests_made)

        except RetryableError as exc:
            if requests_made == 0:
                requests_made = 1
                metrics.record_request(time.perf_counter() - started)
            last_wait = exc.retry_after if exc.retry_after is not None else compute_backoff(attempt, config)
            logger.warning(
                "Attempt %d/%d failed: %s; retry in %.2fs",
                attempt,
                config.max_retries,
                exc,
                last_wait,
            )

        except RequestException as exc:
            requests_made += 1
            metrics.record_request(time.perf_counter() - started)
            transport_failed = True
            last_wait = compute_backoff(attempt, config)
            logger.warning(
                "Attempt %d/%d transport error: %s; retry in %.2fs",
                attempt,
                config.max_retries,
                exc,
                last_wait,
            )

        if attempt < config.max_retries and stop_event.wait(last_wait or 0.0):
            breaker.cancel_probe()
            return FetchResult({}, request_count=requests_made, transport_failed=transport_failed)

    metrics.record_operation(False)
    breaker.record_failure()
    return FetchResult(
        {},
        retry_after=last_wait,
        request_count=requests_made,
        transport_failed=transport_failed,
    )


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


def prune_cache(cache: dict[str, PricePoint], max_age: float) -> int:
    removed = 0
    for key, point in list(cache.items()):
        age = parse_iso_age(point.fetched_at)
        if age is None or age > max_age:
            del cache[key]
            removed += 1
    return removed


def format_price(price: Decimal) -> str:
    absolute = abs(price)
    if absolute >= Decimal("1"):
        quant = Decimal("0.01")
    elif absolute >= Decimal("0.01"):
        quant = Decimal("0.000001")
    else:
        quant = Decimal("0.0000000001")
    try:
        return f"{price.quantize(quant, rounding=ROUND_HALF_UP):,f}"
    except InvalidOperation:
        return format(price, "f")


def install_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(signum: int, _frame: object) -> None:
        logger.info("Shutdown requested by signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def emit_prices(
    prices: Mapping[str, Decimal],
    previous_prices: Mapping[str, Decimal],
    convert: str,
    logger: logging.Logger,
    json_output: bool,
) -> None:
    timestamp = utc_now_iso()
    if json_output:
        output = {
            "timestamp": timestamp,
            "convert": convert,
            "prices": {symbol: str(price) for symbol, price in prices.items()},
        }
        print(json.dumps(output, ensure_ascii=False, sort_keys=True), flush=True)
        return

    for symbol, price in prices.items():
        previous = previous_prices.get(symbol)
        delta = ""
        if previous is not None and previous != 0:
            pct = (price - previous) / previous * Decimal("100")
            arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
            delta = f" ({arrow} {pct:+.3f}%)"
        logger.info("[%s/%s] %s %s%s", symbol, convert, format_price(price), convert, delta)


def log_cached_prices(
    symbols: Sequence[str],
    convert: str,
    cached: Mapping[str, PricePoint],
    config: Config,
    logger: logging.Logger,
) -> None:
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


def next_interval_after_failure(
    current: float, target: float, retry_after: float | None, config: Config
) -> float:
    suggested = retry_after if retry_after is not None else current + config.interval_step
    return min(config.max_interval, max(config.min_interval, target, suggested))


def track_prices(
    config: Config,
    symbols: Sequence[str],
    convert: str,
    target_interval: float,
    logger: logging.Logger,
    *,
    once: bool = False,
    json_output: bool = False,
) -> int:
    stop_event = threading.Event()
    install_signal_handlers(stop_event, logger)

    cache_store = PriceCache(config.cache_file, logger)
    cached = cache_store.load()
    removed = prune_cache(cached, config.cache_prune_age)
    if removed:
        logger.debug("Pruned %d expired cache entries", removed)

    last_prices = {
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
    exit_code = 0

    logger.info(
        "Tracking %s in %s every %.1fs%s",
        ",".join(symbols),
        convert,
        target_interval,
        " (one-shot)" if once else "",
    )

    try:
        while not stop_event.is_set():
            cycle_started = time.monotonic()
            cycle += 1

            if session_requests >= config.session_refresh_every:
                session.close()
                session = create_session(config)
                session_requests = 0
                logger.debug("HTTP session refreshed after request threshold")

            result = fetch_prices(
                session, config, symbols, convert, breaker, metrics, logger, stop_event
            )
            session_requests += result.request_count

            if result.transport_failed:
                session.close()
                session = create_session(config)
                session_requests = 0
                logger.debug("HTTP session recreated after transport failure")

            if result.prices:
                previous_snapshot = dict(last_prices)
                emit_prices(result.prices, previous_snapshot, convert, logger, json_output)
                now_iso = utc_now_iso()
                for symbol, price in result.prices.items():
                    last_prices[symbol] = price
                    cached[cache_key(symbol, convert)] = PricePoint(symbol, convert, price, now_iso)
                if result.missing_symbols:
                    logger.warning("No valid current price for: %s", ", ".join(result.missing_symbols))
                    log_cached_prices(result.missing_symbols, convert, cached, config, logger)
                try:
                    cache_store.save(cached)
                except OSError as exc:
                    logger.warning("Could not save cache: %s", exc)
                current_interval = (
                    max(target_interval, current_interval - config.interval_step)
                    if config.adaptive_interval
                    else target_interval
                )
            else:
                log_cached_prices(symbols, convert, cached, config, logger)
                if result.permanent_error:
                    exit_code = 2
                    logger.error("Stopping after permanent API error; retrying with unchanged inputs cannot recover")
                    break
                elif once:
                    exit_code = 1
                    break
                if config.adaptive_interval:
                    current_interval = next_interval_after_failure(
                        current_interval, target_interval, result.retry_after, config
                    )

            if cycle % config.health_log_every == 0:
                state, failures = breaker.snapshot()
                logger.info(
                    "Health | interval=%.1fs breaker=%s failures=%d | %s",
                    current_interval,
                    state.value,
                    failures,
                    metrics.summary(),
                )

            if once:
                break

            elapsed = time.monotonic() - cycle_started
            sleep_for = max(0.0, current_interval - elapsed)
            stop_event.wait(sleep_for)
    finally:
        session.close()
        logger.info("Tracker stopped | %s", metrics.summary())
    return exit_code


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in value.split(",") if part.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("At least one symbol is required")
    if len(symbols) > 100:
        raise argparse.ArgumentTypeError("Too many symbols; maximum is 100")
    invalid = [symbol for symbol in symbols if not symbol.replace("-", "").isalnum()]
    if invalid:
        raise argparse.ArgumentTypeError(f"Invalid symbols: {', '.join(invalid)}")
    return symbols


def parse_arguments(config: Config) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reliable CoinMarketCap multi-symbol price tracker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbols", type=parse_symbols, default=config.default_symbols)
    parser.add_argument("--convert", default=config.default_convert, help="Quote currency, e.g. USD or EUR")
    parser.add_argument("--interval", type=float, default=config.default_interval)
    parser.add_argument("--cache-file", type=Path, default=config.cache_file)
    parser.add_argument("--log-file", type=Path, default=config.log_file)
    parser.add_argument("--proxy", default=config.proxy_url, help="HTTP/SOCKS proxy URL")
    parser.add_argument("--no-adaptive", action="store_true", help="Disable adaptive interval")
    parser.add_argument("--once", action="store_true", help="Fetch once and exit")
    parser.add_argument("--json", action="store_true", help="Print successful prices as JSON")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = parser.parse_args()

    args.convert = args.convert.strip().upper()
    if not args.convert or not args.convert.replace("-", "").isalnum():
        parser.error("--convert must contain only letters, digits, or hyphens")
    if not math.isfinite(args.interval) or not config.min_interval <= args.interval <= config.max_interval:
        parser.error(f"--interval must be between {config.min_interval} and {config.max_interval}")
    return args


def main() -> int:
    try:
        config = load_config()
        args = parse_arguments(config)
        config.cache_file = args.cache_file.expanduser()
        config.log_file = args.log_file.expanduser()
        config.proxy_url = args.proxy.strip() if args.proxy else None
        config.adaptive_interval = not args.no_adaptive
        config.verify_tls = not args.insecure
        config.validate()
        logger = setup_logging(config, args.log_level, console_to_stderr=args.json)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Logging setup error: {exc}", file=sys.stderr)
        return 2

    if not config.verify_tls:
        logger.warning("TLS certificate verification is disabled")

    try:
        return track_prices(
            config,
            args.symbols,
            args.convert,
            args.interval,
            logger,
            once=args.once,
            json_output=args.json,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Unhandled fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
