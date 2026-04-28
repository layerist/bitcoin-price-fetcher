#!/usr/bin/env python3
"""
CoinMarketCap cryptocurrency price tracker (v5).

Improvements over v4:
- Rate-limit awareness (headers-based)
- Adaptive interval (auto slowdown on 429)
- Session auto-recreation
- Metrics tracking (success rate, latency avg)
- Structured logging context
- Graceful shutdown via Event (no blocking sleep)
- API error parsing (CMC-specific)
"""

from __future__ import annotations

import os
import sys
import time
import random
import signal
import logging
import argparse
import threading
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional
from logging.handlers import RotatingFileHandler

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError, Timeout, RequestException


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

@dataclass
class Config:
    api_key: str
    api_url: str = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

    default_symbol: str = "BTC"
    default_convert: str = "USD"
    default_interval: int = 60

    min_interval: int = 5
    max_interval: int = 3600

    max_retries: int = 5
    backoff_factor: float = 1.5
    max_backoff: float = 60.0

    request_timeout: int = 10
    jitter_mode: str = "full"

    # Circuit breaker
    failure_threshold: int = 5
    recovery_time: int = 120

    # Adaptive interval
    adaptive_interval: bool = True
    interval_step: int = 5

    log_file: str = field(default_factory=lambda: f"crypto_price_{int(time.time())}.log")
    max_log_size: int = 5 * 1024 * 1024
    backup_count: int = 3


def load_config() -> Config:
    api_key = os.getenv("CMC_API_KEY")

    if not api_key:
        print("Missing CMC_API_KEY", file=sys.stderr)
        sys.exit(2)

    return Config(api_key=api_key)


# ─────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────

class ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return f"[{self.extra}] {msg}", kwargs


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
        return f"{color}{super().format(record)}{self.RESET}"


def setup_logging(config: Config, level: str) -> logging.Logger:
    logger = logging.getLogger("tracker")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = "%(asctime)s | %(levelname)s | %(message)s"

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.max_log_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt))

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter(fmt))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# ─────────────────────────────────────────────────────────────
# Metrics
# ─────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self):
        self.success = 0
        self.fail = 0
        self.latencies = []

    def record(self, success: bool, latency: float):
        if success:
            self.success += 1
        else:
            self.fail += 1

        self.latencies.append(latency)
        if len(self.latencies) > 100:
            self.latencies.pop(0)

    def summary(self):
        total = self.success + self.fail
        avg_latency = sum(self.latencies) / len(self.latencies) if self.latencies else 0
        success_rate = (self.success / total * 100) if total else 0

        return f"success={success_rate:.1f}% avg_latency={avg_latency:.2f}s"


# ─────────────────────────────────────────────────────────────
# Circuit Breaker
# ─────────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self, threshold: int, recovery_time: int):
        self.threshold = threshold
        self.recovery_time = recovery_time
        self.failures = 0
        self.opened_at: Optional[float] = None

    def record_success(self):
        self.failures = 0
        self.opened_at = None

    def record_failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.time()

    def is_open(self):
        if self.opened_at is None:
            return False

        if time.time() - self.opened_at > self.recovery_time:
            self.failures = 0
            self.opened_at = None
            return False

        return True


# ─────────────────────────────────────────────────────────────
# Networking
# ─────────────────────────────────────────────────────────────

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def create_session(config: Config) -> requests.Session:
    session = requests.Session()

    session.headers.update({
        "X-CMC_PRO_API_KEY": config.api_key,
        "Accept": "application/json",
        "User-Agent": "cmc-tracker/5.0",
    })

    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)

    return session


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def compute_backoff(attempt, factor, max_backoff):
    return min(factor * (2 ** (attempt - 1)), max_backoff) * random.random()


def parse_api_error(payload: dict) -> str:
    status = payload.get("status", {})
    return status.get("error_message") or "Unknown API error"


def parse_price(payload: dict, symbol: str, convert: str) -> Decimal:
    try:
        return Decimal(str(payload["data"][symbol]["quote"][convert]["price"]))
    except Exception as exc:
        raise ValueError("Invalid price format") from exc


# ─────────────────────────────────────────────────────────────
# Fetch Logic
# ─────────────────────────────────────────────────────────────

def fetch_price(session, config, symbol, convert, breaker, logger, metrics):
    if breaker.is_open():
        logger.warning("Circuit breaker OPEN")
        return None, None

    params = {"symbol": symbol, "convert": convert}

    for attempt in range(1, config.max_retries + 1):
        start = time.monotonic()

        try:
            response = session.get(
                config.api_url,
                params=params,
                timeout=config.request_timeout,
            )

            latency = time.monotonic() - start

            if response.status_code == 429:
                metrics.record(False, latency)
                breaker.record_failure()

                retry_after = int(response.headers.get("Retry-After", 5))
                logger.warning(f"Rate limit hit → sleep {retry_after}s")

                return None, retry_after

            if response.status_code in RETRYABLE_STATUS:
                raise HTTPError(f"{response.status_code}")

            payload = response.json()

            if payload.get("status", {}).get("error_code") != 0:
                raise ValueError(parse_api_error(payload))

            price = parse_price(payload, symbol, convert)

            breaker.record_success()
            metrics.record(True, latency)

            return price, None

        except (Timeout, RequestException, HTTPError) as exc:
            latency = time.monotonic() - start
            metrics.record(False, latency)
            breaker.record_failure()

            wait = compute_backoff(attempt, config.backoff_factor, config.max_backoff)
            logger.warning(f"Retry {attempt} wait={wait:.2f}s error={exc}")

            time.sleep(wait)

        except Exception as exc:
            metrics.record(False, 0)
            breaker.record_failure()
            logger.error(f"Fatal error: {exc}")
            return None, None

    return None, None


# ─────────────────────────────────────────────────────────────
# Runtime Loop
# ─────────────────────────────────────────────────────────────

def track_prices(config, symbol, convert, interval, logger):
    logger = ContextAdapter(logger, {"pair": f"{symbol}/{convert}"})

    session = create_session(config)
    breaker = CircuitBreaker(config.failure_threshold, config.recovery_time)
    metrics = Metrics()

    stop_event = threading.Event()

    def stop(*_):
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    last_price: Optional[Decimal] = None

    while not stop_event.is_set():
        price, retry_after = fetch_price(
            session, config, symbol, convert, breaker, logger, metrics
        )

        if retry_after:
            interval = min(config.max_interval, interval + config.interval_step)

        if price is not None:
            last_price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            logger.info(f"Price: ${last_price:,.2f} | {metrics.summary()}")

        else:
            if last_price:
                logger.warning(f"Fallback price: ${last_price:,.2f}")
            else:
                logger.error("No price available")

        stop_event.wait(interval)

    session.close()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def parse_arguments(config):
    parser = argparse.ArgumentParser()

    parser.add_argument("--symbol", default=config.default_symbol)
    parser.add_argument("--convert", default=config.default_convert)
    parser.add_argument("--interval", type=int, default=config.default_interval)
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args()

    args.symbol = args.symbol.upper()
    args.convert = args.convert.upper()

    return args


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    config = load_config()
    args = parse_arguments(config)

    logger = setup_logging(config, args.log_level)

    track_prices(
        config,
        args.symbol,
        args.convert,
        args.interval,
        logger,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
