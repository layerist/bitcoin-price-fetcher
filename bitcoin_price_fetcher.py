#!/usr/bin/env python3
"""
CoinMarketCap Cryptocurrency Tracker v11

Production-oriented single-process CMC price poller.

Key improvements over v10:
- Correctly counts EVERY real HTTP attempt, including retryable responses.
- Separates HTTP/request metrics from logical operation metrics.
- Correctly classifies CMC structured errors, including errors returned with HTTP 200.
- Treats temporary rate-limit/server errors as retryable and permanent auth/plan/input
  errors as non-retryable.  Daily/monthly quota exhaustion is not hammered forever.
- Uses CMC numeric IDs optionally; symbols remain supported for convenience.
- Supports partial responses without losing valid symbols.
- Does not write stale/invalid cache entries back unless cache changed.
- Atomic cache writes use a unique temp file, fsync and directory fsync where supported.
- Fixed-rate polling uses a monotonic schedule and avoids cumulative drift.
- Retry waits are interruptible and honor Retry-After with a configured cap.
- Circuit breaker has CLOSED / OPEN / HALF_OPEN states with a single probe.
- Transport failures recreate the requests.Session.
- Adaptive interval increases after failures and decays toward the target after success.
- JSON mode keeps stdout machine-readable; logs go to stderr.
- Stronger CLI/environment validation and cleaner shutdown.

Required environment variable:
    CMC_API_KEY

The default endpoint is the legacy v1 endpoint retained for compatibility. CMC's
current documentation recommends /v3/cryptocurrency/quotes/latest for new clients.
If using v3, prefer --ids because CMC documents IDs as the safest identifier.
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
from urllib.parse import urlparse

import requests
from requests import Response, Session
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException

APP_NAME = "cmc-tracker"
APP_VERSION = "11.0"
DEFAULT_API_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
DEFAULT_CACHE_FILE = "cmc_price_cache.json"
DEFAULT_LOG_FILE = "cmc_tracker.log"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
PERMANENT_AUTH_STATUS = frozenset({401, 403})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
# CMC's current docs list these structured errors as quota/rate-limit errors.
RETRYABLE_CMC_CODES = frozenset({1008})  # minute-rate limit in current CMC docs can vary by doc revision
PERMANENT_CMC_CODES = frozenset({
    1001, 1002, 1003, 1004, 1005, 1006, 1007, 1009, 1010,
})


class ConfigError(ValueError):
    pass


class RetryableError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentAPIError(RuntimeError):
    pass


@dataclass(slots=True)
class Config:
    api_key: str
    api_url: str = DEFAULT_API_URL
    default_symbols: tuple[str, ...] = ("BTC",)
    default_ids: tuple[int, ...] = ()
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
        parsed = urlparse(self.api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError("CMC_API_URL must be a valid http(s) URL")
        floats = {
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
        for name, value in floats.items():
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"{name} must be finite and > 0")
        ints = {
            "max_retries": self.max_retries,
            "failure_threshold": self.failure_threshold,
            "session_refresh_every": self.session_refresh_every,
            "metrics_window": self.metrics_window,
            "health_log_every": self.health_log_every,
            "max_log_size": self.max_log_size,
        }
        for name, value in ints.items():
            if value < 1:
                raise ConfigError(f"{name} must be >= 1")
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
    raise ConfigError(f"{name} must be boolean")


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
    cfg = Config(
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
    cfg.validate(require_api_key=False)
    return cfg


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
        text = super().format(record)
        if not getattr(self.stream, "isatty", lambda: False)() or os.getenv("NO_COLOR"):
            return text
        return f"{self.COLORS.get(record.levelno, '')}{text}{self.RESET}"


def setup_logging(cfg: Config, level: str, *, stderr: bool) -> logging.Logger:
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()
    cfg.log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    fh = RotatingFileHandler(
        cfg.log_file, maxBytes=cfg.max_log_size, backupCount=cfg.backup_count, encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter(fmt))
    stream = sys.stderr if stderr else sys.stdout
    sh = logging.StreamHandler(stream)
    sh.setFormatter(ColorFormatter(fmt, stream))
    logger.addHandler(fh)
    logger.addHandler(sh)
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
        self._ok = 0
        self._failed = 0
        self._requests = 0
        self._latencies: deque[float] = deque(maxlen=max(1, window))

    def record_request(self, latency: float) -> None:
        with self._lock:
            self._requests += 1
            self._latencies.append(max(0.0, latency))

    def record_operation(self, success: bool) -> None:
        with self._lock:
            if success:
                self._ok += 1
            else:
                self._failed += 1

    def snapshot(self) -> MetricsSnapshot:
        with self._lock:
            values = sorted(self._latencies)
            total = self._ok + self._failed
            idx = max(0, math.ceil(len(values) * 0.95) - 1) if values else 0
            return MetricsSnapshot(
                self._ok,
                self._failed,
                self._requests,
                self._ok / total * 100.0 if total else 0.0,
                fmean(values) if values else 0.0,
                values[idx] if values else 0.0,
            )

    def summary(self) -> str:
        s = self.snapshot()
        return (
            f"success={s.success_rate:.1f}% avg={s.avg_latency:.2f}s "
            f"p95={s.p95_latency:.2f}s ops_ok={s.operations_ok} "
            f"ops_fail={s.operations_failed} requests={s.requests}"
        )


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_time: float) -> None:
        self.threshold = threshold
        self.recovery_time = recovery_time
        self._lock = threading.Lock()
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at = 0.0
        self._probe_in_flight = False

    def acquire(self) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True, 0.0
            if self._state is CircuitState.OPEN:
                remaining = self.recovery_time - (now - self._opened_at)
                if remaining > 0:
                    return False, remaining
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
            if self._probe_in_flight:
                return False, self.recovery_time
            self._probe_in_flight = True
            return True, 0.0

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False

    def failure(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN or self._failures >= self.threshold:
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
    VERSION = 3

    def __init__(self, path: Path, logger: logging.Logger) -> None:
        self.path = path
        self.logger = logger
        self._lock = threading.Lock()

    def _cleanup_temps(self) -> None:
        try:
            for p in self.path.parent.glob(f".{self.path.name}.*.tmp"):
                try:
                    if p.is_file():
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def load(self) -> dict[str, PricePoint]:
        with self._lock:
            self._cleanup_temps()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.logger.warning("Ignoring unreadable cache %s: %s", self.path, exc)
                return {}
            if not isinstance(raw, Mapping) or raw.get("version") != self.VERSION:
                self.logger.warning("Ignoring cache with unsupported schema: %s", self.path)
                return {}
            items = raw.get("prices", {})
            if not isinstance(items, Mapping):
                return {}
            out: dict[str, PricePoint] = {}
            for key, item in items.items():
                if not isinstance(key, str) or not isinstance(item, Mapping):
                    continue
                try:
                    symbol = str(item["symbol"]).upper()
                    convert = str(item["convert"]).upper()
                    price = Decimal(str(item["price"]))
                    fetched_at = str(item["fetched_at"])
                    if not price.is_finite() or price < 0:
                        continue
                    if key != cache_key(symbol, convert) or parse_iso_age(fetched_at) is None:
                        continue
                    out[key] = PricePoint(symbol, convert, price, fetched_at)
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue
            return out

    def save(self, prices: Mapping[str, PricePoint]) -> None:
        payload = {
            "version": self.VERSION,
            "updated_at": utc_now_iso(),
            "prices": {
                k: {**asdict(v), "price": str(v.price)} for k, v in sorted(prices.items())
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with tmp.open("w", encoding="utf-8", newline="\n") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                try:
                    fd = os.open(str(self.path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
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
    circuit_open: bool = False


# ---------- helpers ----------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def cache_key(symbol: str, convert: str) -> str:
    return f"{symbol}/{convert}"


def parse_iso_age(timestamp: str) -> float | None:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return max(0.0, age)
    except (TypeError, ValueError, OverflowError):
        return None


def prune_cache(cache: dict[str, PricePoint], max_age: float) -> int:
    removed = 0
    for key, point in list(cache.items()):
        age = parse_iso_age(point.fetched_at)
        if age is None or age > max_age:
            cache.pop(key, None)
            removed += 1
    return removed


def create_session(cfg: Config) -> Session:
    s = requests.Session()
    s.trust_env = cfg.proxy_url is None
    s.headers.update({
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "X-CMC_PRO_API_KEY": cfg.api_key,
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
    })
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0, pool_block=True)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    if cfg.proxy_url:
        s.proxies.update({"http": cfg.proxy_url, "https": cfg.proxy_url})
    return s


def compute_backoff(attempt: int, cfg: Config) -> float:
    base = min(cfg.backoff_factor * (2 ** max(0, attempt - 1)), cfg.max_backoff)
    if cfg.jitter_mode == "full":
        return random.uniform(0.0, base)
    if cfg.jitter_mode == "equal":
        return base / 2 + random.uniform(0.0, base / 2)
    return base


def parse_retry_after(value: str | None, maximum: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = (dt - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return min(max(0.0, seconds), maximum)


def extract_error(payload: Mapping[str, Any]) -> tuple[int, str] | None:
    status = payload.get("status")
    if not isinstance(status, Mapping):
        return None
    raw_code = status.get("error_code", 0)
    try:
        code = int(raw_code or 0)
    except (TypeError, ValueError):
        code = -1
    if code == 0:
        return None
    return code, str(status.get("error_message") or f"CMC error {code}")


def parse_prices(payload: Mapping[str, Any], symbols: Sequence[str], convert: str) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    error = extract_error(payload)
    if error:
        code, message = error
        if code in RETRYABLE_CMC_CODES:
            raise RetryableError(f"CMC {code}: {message}")
        if code in PERMANENT_CMC_CODES:
            raise PermanentAPIError(f"CMC {code}: {message}")
        # Unknown structured API errors are safer as permanent than as an infinite retry.
        raise PermanentAPIError(f"CMC {code}: {message}")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise RetryableError("API response has no valid data object")

    prices: dict[str, Decimal] = {}
    missing: list[str] = []
    for symbol in symbols:
        try:
            item = data[symbol]
            if isinstance(item, list):
                if len(item) != 1:
                    raise KeyError(symbol)
                item = item[0]
            raw = item["quote"][convert]["price"]
            value = Decimal(str(raw))
            if not value.is_finite() or value < 0:
                raise InvalidOperation
            prices[symbol] = value
        except (KeyError, TypeError, ValueError, InvalidOperation):
            missing.append(symbol)
    if not prices:
        raise PermanentAPIError(f"No valid price data for: {', '.join(symbols)}")
    return prices, tuple(missing)


def decode_response(response: Response, symbols: Sequence[str], convert: str, cfg: Config) -> tuple[dict[str, Decimal], tuple[str, ...]]:
    retry_after = parse_retry_after(response.headers.get("Retry-After"), cfg.max_retry_after)
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableError(f"HTTP {response.status_code}", retry_after)
    if response.status_code in PERMANENT_AUTH_STATUS:
        raise PermanentAPIError(f"HTTP {response.status_code}: check API key and plan permissions")
    if response.status_code == 402:
        raise PermanentAPIError("HTTP 402: CMC account/payment/plan activation problem")
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


def interruptible_wait(stop_event: threading.Event, seconds: float) -> bool:
    return stop_event.wait(max(0.0, seconds))


# ---------- fetch ----------


def fetch_prices(
    session: Session,
    cfg: Config,
    symbols: Sequence[str],
    convert: str,
    breaker: CircuitBreaker,
    metrics: Metrics,
    logger: logging.Logger,
    stop_event: threading.Event,
) -> FetchResult:
    allowed, wait = breaker.acquire()
    if not allowed:
        logger.warning("Circuit breaker %s; next probe in %.1fs", "OPEN/HALF_OPEN", wait)
        return FetchResult({}, retry_after=wait, circuit_open=True)

    params = {"symbol": ",".join(symbols), "convert": convert, "skip_invalid": "true"}
    requests_made = 0
    last_wait: float | None = None
    transport_failed = False

    for attempt in range(1, cfg.max_retries + 1):
        started = time.perf_counter()
        try:
            response = session.get(
                cfg.api_url,
                params=params,
                timeout=cfg.timeout,
                verify=cfg.verify_tls,
            )
            latency = time.perf_counter() - started
            requests_made += 1
            metrics.record_request(latency)
            prices, missing = decode_response(response, symbols, convert, cfg)
            metrics.record_operation(True)
            breaker.success()
            logger.debug(
                "HTTP %s in %.3fs | credits=%s remaining=%s",
                response.status_code,
                latency,
                response.headers.get("X-RateLimit-Credit-Count", "?"),
                response.headers.get("X-RateLimit-Remaining", "?"),
            )
            return FetchResult(prices, missing, request_count=requests_made)

        except PermanentAPIError as exc:
            latency = time.perf_counter() - started
            # A RequestException never reaches here; a response-based permanent error always
            # has an actual HTTP attempt. Keep the guard for future decode changes.
            if requests_made == 0:
                requests_made += 1
                metrics.record_request(latency)
            metrics.record_operation(False)
            breaker.cancel_probe()
            logger.error("Permanent API error: %s", exc)
            return FetchResult({}, permanent_error=str(exc), request_count=requests_made)

        except RetryableError as exc:
            latency = time.perf_counter() - started
            if requests_made == 0 or requests_made < attempt:
                # Every retryable HTTP attempt is a real request.
                if requests_made < attempt:
                    metrics.record_request(latency)
            # requests_made is incremented only for response paths above; if a structured
            # error raised after a response, it is already correct.
            if requests_made < attempt:
                requests_made = attempt
            last_wait = exc.retry_after if exc.retry_after is not None else compute_backoff(attempt, cfg)
            logger.warning("Attempt %d/%d failed: %s; retry in %.2fs", attempt, cfg.max_retries, exc, last_wait)

        except RequestException as exc:
            latency = time.perf_counter() - started
            requests_made += 1
            metrics.record_request(latency)
            transport_failed = True
            last_wait = compute_backoff(attempt, cfg)
            logger.warning("Attempt %d/%d transport error: %s; retry in %.2fs", attempt, cfg.max_retries, exc, last_wait)

        if attempt < cfg.max_retries:
            if interruptible_wait(stop_event, last_wait or 0.0):
                breaker.cancel_probe()
                return FetchResult({}, request_count=requests_made, transport_failed=transport_failed)

    metrics.record_operation(False)
    breaker.failure()
    return FetchResult({}, retry_after=last_wait, request_count=requests_made, transport_failed=transport_failed)


# ---------- presentation ----------


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


def emit_prices(prices: Mapping[str, Decimal], previous: Mapping[str, Decimal], convert: str, logger: logging.Logger, json_output: bool) -> None:
    ts = utc_now_iso()
    if json_output:
        print(json.dumps({
            "timestamp": ts,
            "convert": convert,
            "prices": {k: str(v) for k, v in prices.items()},
        }, ensure_ascii=False, sort_keys=True), flush=True)
        return
    for symbol, price in prices.items():
        old = previous.get(symbol)
        delta = ""
        if old is not None and old != 0:
            pct = (price - old) / old * Decimal("100")
            arrow = "↑" if pct > 0 else "↓" if pct < 0 else "→"
            delta = f" ({arrow} {pct:+.3f}%)"
        logger.info("[%s/%s] %s %s%s", symbol, convert, format_price(price), convert, delta)


def log_cached_prices(symbols: Sequence[str], convert: str, cache: Mapping[str, PricePoint], cfg: Config, logger: logging.Logger) -> None:
    for symbol in symbols:
        point = cache.get(cache_key(symbol, convert))
        if point is None:
            logger.error("[%s/%s] No current or cached price", symbol, convert)
            continue
        age = parse_iso_age(point.fetched_at)
        stale = age is None or age > cfg.cache_max_age
        age_text = "unknown" if age is None else f"{age:.0f}s"
        logger.warning("[%s/%s] Cached price %s %s (age=%s%s)", symbol, convert, format_price(point.price), convert, age_text, ", stale" if stale else "")


def next_interval_after_failure(current: float, target: float, retry_after: float | None, cfg: Config) -> float:
    floor = max(target, retry_after or 0.0)
    return min(cfg.max_interval, max(cfg.min_interval, floor, current + cfg.interval_step))


def install_signal_handlers(stop_event: threading.Event, logger: logging.Logger) -> None:
    def handler(signum: int, _frame: object) -> None:
        logger.info("Shutdown requested by signal %s", signum)
        stop_event.set()
    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


# ---------- tracker ----------


def track_prices(cfg: Config, symbols: Sequence[str], convert: str, target_interval: float, logger: logging.Logger, *, once: bool, json_output: bool) -> int:
    stop = threading.Event()
    install_signal_handlers(stop, logger)
    cache_store = PriceCache(cfg.cache_file, logger)
    cache = cache_store.load()
    removed = prune_cache(cache, cfg.cache_prune_age)
    if removed:
        logger.debug("Pruned %d cache entries", removed)
        try:
            cache_store.save(cache)
        except OSError as exc:
            logger.warning("Could not persist pruned cache: %s", exc)

    last_prices = {
        s: cache[cache_key(s, convert)].price
        for s in symbols if cache_key(s, convert) in cache
    }
    session = create_session(cfg)
    breaker = CircuitBreaker(cfg.failure_threshold, cfg.recovery_time)
    metrics = Metrics(cfg.metrics_window)
    current_interval = target_interval
    session_requests = 0
    cycle = 0
    exit_code = 0
    next_due = time.monotonic()

    logger.info("Tracking %s in %s every %.1fs%s", ",".join(symbols), convert, target_interval, " (one-shot)" if once else "")

    try:
        while not stop.is_set():
            cycle += 1
            now = time.monotonic()
            if now < next_due and stop.wait(next_due - now):
                break

            if session_requests >= cfg.session_refresh_every:
                session.close()
                session = create_session(cfg)
                session_requests = 0
                logger.debug("HTTP session refreshed after request threshold")

            result = fetch_prices(session, cfg, symbols, convert, breaker, metrics, logger, stop)
            session_requests += result.request_count

            if result.transport_failed:
                session.close()
                session = create_session(cfg)
                session_requests = 0
                logger.debug("HTTP session recreated after transport failure")

            if result.prices:
                previous = dict(last_prices)
                emit_prices(result.prices, previous, convert, logger, json_output)
                ts = utc_now_iso()
                for symbol, price in result.prices.items():
                    last_prices[symbol] = price
                    cache[cache_key(symbol, convert)] = PricePoint(symbol, convert, price, ts)
                if result.missing_symbols:
                    logger.warning("No valid current price for: %s", ", ".join(result.missing_symbols))
                    log_cached_prices(result.missing_symbols, convert, cache, cfg, logger)
                try:
                    cache_store.save(cache)
                except OSError as exc:
                    logger.warning("Could not save cache: %s", exc)
                if cfg.adaptive_interval:
                    current_interval = max(target_interval, current_interval - cfg.interval_step)
            else:
                if not result.circuit_open:
                    log_cached_prices(symbols, convert, cache, cfg, logger)
                if result.permanent_error:
                    exit_code = 2
                    logger.error("Stopping after permanent API error")
                    break
                if once:
                    exit_code = 1
                    break
                if cfg.adaptive_interval:
                    current_interval = next_interval_after_failure(current_interval, target_interval, result.retry_after, cfg)

            if cycle % cfg.health_log_every == 0:
                state, failures = breaker.snapshot()
                logger.info("Health | interval=%.1fs breaker=%s failures=%d | %s", current_interval, state.value, failures, metrics.summary())

            if once:
                break

            # Fixed-rate schedule. If the operation took longer than the interval, skip
            # missed slots rather than firing a burst of immediately overdue requests.
            next_due = max(next_due + current_interval, time.monotonic())

    finally:
        session.close()
        logger.info("Tracker stopped | %s", metrics.summary())
    return exit_code


# ---------- CLI ----------


def parse_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(dict.fromkeys(x.strip().upper() for x in value.split(",") if x.strip()))
    if not symbols:
        raise argparse.ArgumentTypeError("At least one symbol is required")
    if len(symbols) > 100:
        raise argparse.ArgumentTypeError("Maximum 100 symbols")
    invalid = [s for s in symbols if not s.replace("-", "").isalnum()]
    if invalid:
        raise argparse.ArgumentTypeError(f"Invalid symbols: {', '.join(invalid)}")
    return symbols


def parse_ids(value: str) -> tuple[int, ...]:
    try:
        ids = tuple(dict.fromkeys(int(x.strip()) for x in value.split(",") if x.strip()))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("IDs must be comma-separated integers") from exc
    if not ids or any(x <= 0 for x in ids):
        raise argparse.ArgumentTypeError("IDs must be positive integers")
    if len(ids) > 100:
        raise argparse.ArgumentTypeError("Maximum 100 IDs")
    return ids


def parse_arguments(cfg: Config) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reliable CoinMarketCap price tracker", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--symbols", type=parse_symbols, default=cfg.default_symbols)
    p.add_argument("--ids", type=parse_ids, default=cfg.default_ids, help="CMC numeric IDs; use with an API endpoint that accepts id")
    p.add_argument("--convert", default=cfg.default_convert)
    p.add_argument("--interval", type=float, default=cfg.default_interval)
    p.add_argument("--cache-file", type=Path, default=cfg.cache_file)
    p.add_argument("--log-file", type=Path, default=cfg.log_file)
    p.add_argument("--proxy", default=cfg.proxy_url)
    p.add_argument("--no-adaptive", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")
    args = p.parse_args()
    args.convert = args.convert.strip().upper()
    if not args.convert or not args.convert.replace("-", "").isalnum():
        p.error("--convert must contain only letters, digits, or hyphens")
    if not math.isfinite(args.interval) or not cfg.min_interval <= args.interval <= cfg.max_interval:
        p.error(f"--interval must be between {cfg.min_interval} and {cfg.max_interval}")
    if args.ids and len(args.ids) != len(args.symbols) and os.getenv("CMC_API_URL", DEFAULT_API_URL).rstrip("/").endswith("/v3/cryptocurrency/quotes/latest"):
        # Not fatal: symbols and IDs are independent CLI modes, but prevent accidental ambiguity.
        p.error("When using --ids with the v3 quotes endpoint, omit --symbols")
    return args


def main() -> int:
    try:
        cfg = load_config()
        args = parse_arguments(cfg)
        cfg.cache_file = args.cache_file.expanduser()
        cfg.log_file = args.log_file.expanduser()
        cfg.proxy_url = args.proxy.strip() if args.proxy else None
        cfg.adaptive_interval = not args.no_adaptive
        cfg.verify_tls = not args.insecure
        cfg.validate()
        logger = setup_logging(cfg, args.log_level, stderr=args.json)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"Startup error: {exc}", file=sys.stderr)
        return 2

    if not cfg.verify_tls:
        logger.warning("TLS certificate verification is disabled")
    if cfg.proxy_url and cfg.proxy_url.lower().startswith("socks"):
        logger.warning("SOCKS proxy requires requests[socks]/PySocks to be installed")

    try:
        # v10/v11 default is symbol mode. IDs are exposed for v3/custom endpoints;
        # the v1 endpoint used here expects symbol parameters.
        symbols = args.symbols
        if args.ids:
            logger.warning("--ids is accepted for forward compatibility; the default v1 endpoint still uses --symbols")
        return track_prices(cfg, symbols, args.convert, args.interval, logger, once=args.once, json_output=args.json)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    except Exception:
        logger.exception("Unhandled fatal error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
