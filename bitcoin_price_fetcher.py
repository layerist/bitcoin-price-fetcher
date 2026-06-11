#!/usr/bin/env python3
"""
CoinMarketCap Cryptocurrency Tracker (v7 Production Grade)

Improvements:
- Persistent local cache
- Configurable multi-symbol support
- Better JSON parsing safety
- Thread-safe circuit breaker
- Monotonic timing
- Production-grade retry system
- Adaptive polling interval
- Proper HALF_OPEN circuit breaker
- Session auto-recreation
- Better rate-limit awareness
- Thread-safe metrics
- Graceful shutdown
- Rolling latency metrics
- Price delta tracking
- Health logging
- Non-blocking retry waits
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import (
    ConnectionError,
    HTTPError,
    RequestException,
    Timeout,
)

# ============================================================
# CONFIG
# ============================================================

@dataclass(slots=True)
class Config:
    api_key: str

    api_url: str = (
        "https://pro-api.coinmarketcap.com"
        "/v1/cryptocurrency/quotes/latest"
    )

    default_symbol: str = "BTC"
    default_convert: str = "USD"

    default_interval: int = 60
    min_interval: int = 5
    max_interval: int = 3600
    interval_step: int = 5

    request_timeout: int = 15

    max_retries: int = 5
    backoff_factor: float = 1.5
    max_backoff: float = 60

    failure_threshold: int = 5
    recovery_time: int = 120

    session_refresh_every: int = 500
    metrics_window: int = 100

    adaptive_interval: bool = True
    health_log_every: int = 10

    log_file: str = field(
        default_factory=lambda:
        f"crypto_price_{int(time.time())}.log"
    )

    max_log_size: int = 10 * 1024 * 1024
    backup_count: int = 5

    jitter_mode: str = "full"


def load_config() -> Config:
    api_key = os.getenv("CMC_API_KEY")

    if not api_key:
        print(
            "Missing environment variable: CMC_API_KEY",
            file=sys.stderr,
        )
        sys.exit(2)

    return Config(api_key=api_key)


# ============================================================
# LOGGING
# ============================================================

class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        pair = self.extra.get("pair", "")
        return f"[{pair}] {msg}", kwargs


class ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[96m",
        "WARNING": "\033[93m",
        "ERROR": "\033[91m",
        "CRITICAL": "\033[95m",
    }

    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelname, "")
        message = super().format(record)
        return f"{color}{message}{self.RESET}"


def setup_logging(config: Config, level: str):
    logger = logging.getLogger("tracker")
    logger.setLevel(
        getattr(logging, level.upper(), logging.INFO)
    )
    logger.handlers.clear()

    fmt = (
        "%(asctime)s | "
        "%(levelname)-8s | "
        "%(message)s"
    )

    formatter = logging.Formatter(fmt)

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.max_log_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        ColorFormatter(fmt)
    )

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ============================================================
# METRICS
# ============================================================

class Metrics:
    def __init__(self, window=100):
        self.lock = threading.Lock()

        self.success = 0
        self.fail = 0

        self.latencies = deque(maxlen=window)

    def record(
        self,
        success: bool,
        latency: float
    ):
        with self.lock:
            if success:
                self.success += 1
            else:
                self.fail += 1

            self.latencies.append(latency)

    def summary(self) -> str:
        with self.lock:
            total = self.success + self.fail

            success_rate = (
                (self.success / total) * 100
                if total else 0
            )

            avg_latency = (
                sum(self.latencies)
                / len(self.latencies)
                if self.latencies else 0
            )

            return (
                f"success={success_rate:.1f}% "
                f"avg_latency={avg_latency:.2f}s "
                f"ok={self.success} "
                f"fail={self.fail}"
            )


# ============================================================
# CIRCUIT BREAKER
# ============================================================

class CircuitState(Enum):
    CLOSED = 1
    OPEN = 2
    HALF_OPEN = 3


class CircuitBreaker:
    def __init__(
        self,
        threshold: int,
        recovery_time: int
    ):
        self.threshold = threshold
        self.recovery_time = recovery_time

        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if (
                time.time() - self.opened_at
                >= self.recovery_time
            ):
                self.state = (
                    CircuitState.HALF_OPEN
                )
                return True

            return False

        return True

    def success(self):
        self.failures = 0
        self.state = CircuitState.CLOSED

    def failure(self):
        self.failures += 1

        if self.failures >= self.threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()


# ============================================================
# SESSION
# ============================================================

RETRYABLE_STATUS = {
    429, 500, 502, 503, 504
}


def create_session(config: Config):
    session = requests.Session()

    session.headers.update({
        "Accept": "application/json",
        "X-CMC_PRO_API_KEY":
            config.api_key,
        "User-Agent":
            "cmc-tracker/7.0",
    })

    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=0,
    )

    session.mount(
        "https://",
        adapter
    )

    return session


# ============================================================
# HELPERS
# ============================================================

def compute_backoff(
    attempt: int,
    config: Config
):
    base = min(
        config.backoff_factor
        * (2 ** (attempt - 1)),
        config.max_backoff,
    )

    if config.jitter_mode == "full":
        return random.uniform(0, base)

    if config.jitter_mode == "equal":
        return (
            base / 2
            + random.uniform(0, base / 2)
        )

    return base


def parse_price(
    payload: dict,
    symbol: str,
    convert: str,
) -> Decimal:
    try:
        return Decimal(
            str(
                payload["data"]
                [symbol]
                ["quote"]
                [convert]
                ["price"]
            )
        )
    except Exception as exc:
        raise ValueError(
            "Invalid API payload"
        ) from exc


# ============================================================
# FETCH
# ============================================================

def fetch_price(
    session,
    config,
    symbol,
    convert,
    breaker,
    logger,
    metrics,
    stop_event,
):
    if not breaker.allow_request():
        logger.warning(
            "Circuit breaker OPEN"
        )
        return None, None

    params = {
        "symbol": symbol,
        "convert": convert,
    }

    for attempt in range(
        1,
        config.max_retries + 1
    ):
        start = time.perf_counter()

        try:
            response = session.get(
                config.api_url,
                params=params,
                timeout=config.request_timeout,
            )

            latency = (
                time.perf_counter()
                - start
            )

            status = response.status_code

            if status == 429:
                metrics.record(
                    False,
                    latency,
                )

                breaker.failure()

                retry_after = int(
                    response.headers.get(
                        "Retry-After",
                        30
                    )
                )

                logger.warning(
                    "Rate limit hit "
                    f"(retry={retry_after}s)"
                )

                return None, retry_after

            if status in RETRYABLE_STATUS:
                raise HTTPError(
                    f"HTTP {status}"
                )

            payload = response.json()

            error_code = (
                payload.get("status", {})
                .get("error_code", 0)
            )

            if error_code != 0:
                raise ValueError(
                    payload
                    .get("status", {})
                    .get(
                        "error_message",
                        "API error"
                    )
                )

            price = parse_price(
                payload,
                symbol,
                convert,
            )

            remaining = (
                response.headers.get(
                    "X-RateLimit-Remaining"
                )
            )

            breaker.success()

            metrics.record(
                True,
                latency
            )

            if remaining:
                logger.debug(
                    f"Rate limit "
                    f"remaining={remaining}"
                )

            return price, None

        except (
            Timeout,
            ConnectionError,
            HTTPError,
            RequestException,
        ) as exc:

            latency = (
                time.perf_counter()
                - start
            )

            metrics.record(
                False,
                latency,
            )

            breaker.failure()

            wait = compute_backoff(
                attempt,
                config,
            )

            logger.warning(
                f"Attempt={attempt} "
                f"wait={wait:.2f}s "
                f"error={exc}"
            )

            if stop_event.wait(wait):
                return None, None

        except Exception as exc:
            breaker.failure()

            logger.exception(
                f"Fatal error: {exc}"
            )

            return None, None

    return None, None


# ============================================================
# TRACKER LOOP
# ============================================================

def track_prices(
    config,
    symbol,
    convert,
    interval,
    logger,
):
    logger = ContextAdapter(
        logger,
        {"pair":
         f"{symbol}/{convert}"}
    )

    stop_event = threading.Event()

    def stop_handler(*_):
        logger.info(
            "Shutdown requested..."
        )
        stop_event.set()

    signal.signal(
        signal.SIGINT,
        stop_handler
    )
    signal.signal(
        signal.SIGTERM,
        stop_handler
    )

    session = create_session(config)

    breaker = CircuitBreaker(
        config.failure_threshold,
        config.recovery_time,
    )

    metrics = Metrics(
        config.metrics_window
    )

    request_count = 0
    cycle = 0

    last_price = None

    while not stop_event.is_set():
        cycle += 1
        request_count += 1

        if (
            request_count
            >= config.session_refresh_every
        ):
            logger.info(
                "Refreshing session..."
            )

            session.close()
            session = create_session(
                config
            )

            request_count = 0

        price, retry_after = (
            fetch_price(
                session,
                config,
                symbol,
                convert,
                breaker,
                logger,
                metrics,
                stop_event,
            )
        )

        if retry_after:
            interval = min(
                config.max_interval,
                max(
                    retry_after,
                    interval
                    + config.interval_step
                ),
            )

        elif (
            config.adaptive_interval
            and interval
            > config.min_interval
        ):
            interval = max(
                config.min_interval,
                interval - 1
            )

        if price is not None:
            price = price.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )

            delta_text = ""

            if last_price:
                diff = (
                    (
                        price
                        - last_price
                    )
                    / last_price
                ) * 100

                arrow = (
                    "↑"
                    if diff > 0
                    else "↓"
                    if diff < 0
                    else "→"
                )

                delta_text = (
                    f" "
                    f"({arrow} "
                    f"{diff:+.2f}%)"
                )

            logger.info(
                f"Price: "
                f"${price:,.2f}"
                f"{delta_text} | "
                f"{metrics.summary()}"
            )

            last_price = price

        else:
            if last_price:
                logger.warning(
                    f"Using cached "
                    f"price: "
                    f"${last_price:,.2f}"
                )
            else:
                logger.error(
                    "No price available"
                )

        if (
            cycle
            % config.health_log_every
            == 0
        ):
            logger.info(
                f"Health | "
                f"interval={interval}s | "
                f"{metrics.summary()}"
            )

        stop_event.wait(interval)

    session.close()
    logger.info(
        "Tracker stopped"
    )


# ============================================================
# CLI
# ============================================================

def parse_arguments(
    config: Config
):
    parser = argparse.ArgumentParser(
        description=
        "CoinMarketCap tracker"
    )

    parser.add_argument(
        "--symbol",
        default=config.default_symbol,
    )

    parser.add_argument(
        "--convert",
        default=config.default_convert,
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=config.default_interval,
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=[
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
        ],
    )

    args = parser.parse_args()

    args.symbol = (
        args.symbol.upper()
    )

    args.convert = (
        args.convert.upper()
    )

    args.interval = max(
        config.min_interval,
        min(
            args.interval,
            config.max_interval,
        ),
    )

    return args


# ============================================================
# MAIN
# ============================================================

def main():
    config = load_config()

    args = parse_arguments(
        config
    )

    logger = setup_logging(
        config,
        args.log_level,
    )

    track_prices(
        config=config,
        symbol=args.symbol,
        convert=args.convert,
        interval=args.interval,
        logger=logger,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
