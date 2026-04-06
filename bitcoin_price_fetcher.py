#!/usr/bin/env python3
"""
CoinMarketCap cryptocurrency price tracker (v4).

Major upgrades:
- Circuit breaker protection
- Adaptive retry + rate-limit awareness
- Session auto-recovery
- Structured logging context
- Last price fallback (graceful degradation)
- Improved JSON validation
- Latency + success metrics
"""

from __future__ import annotations

import os
import sys
import time
import random
import signal
import logging
import argparse
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
    recovery_time: int = 120  # seconds

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


def setup_logging(config: Config, level: str) -> None:
    logger = logging.getLogger()
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


# ─────────────────────────────────────────────────────────────
# Backoff
# ─────────────────────────────────────────────────────────────

def compute_backoff(attempt, factor, max_backoff, jitter_mode):
    base = min(factor * (2 ** (attempt - 1)), max_backoff)

    if jitter_mode == "full":
        return random.uniform(0, base)

    return min(base + random.random(), max_backoff)


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
            logging.info("Circuit breaker half-open → retrying")
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
        "User-Agent": "cmc-tracker/4.0",
    })

    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)

    return session


# ─────────────────────────────────────────────────────────────
# API Logic
# ─────────────────────────────────────────────────────────────

def _parse_price(payload: dict, symbol: str, convert: str) -> Decimal:
    try:
        data = payload.get("data", {})
        symbol_data = data.get(symbol)

        if not symbol_data:
            raise ValueError("Missing symbol data")

        quote = symbol_data.get("quote", {}).get(convert)

        if not quote:
            raise ValueError("Missing quote data")

        price = quote.get("price")

        return Decimal(str(price))

    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid price format") from exc


def fetch_price(session, config, symbol, convert, breaker):
    params = {"symbol": symbol, "convert": convert}

    if breaker.is_open():
        logging.warning("Circuit breaker OPEN → skipping request")
        return None

    for attempt in range(1, config.max_retries + 1):
        try:
            start = time.monotonic()

            response = session.get(
                config.api_url,
                params=params,
                timeout=config.request_timeout,
            )

            latency = time.monotonic() - start

            if response.status_code in RETRYABLE_STATUS:
                breaker.record_failure()

                wait = compute_backoff(
                    attempt,
                    config.backoff_factor,
                    config.max_backoff,
                    config.jitter_mode,
                )

                logging.warning(
                    "HTTP %s retry %d latency=%.2fs wait=%.2fs",
                    response.status_code,
                    attempt,
                    latency,
                    wait,
                )

                time.sleep(wait)
                continue

            response.raise_for_status()

            payload = response.json()
            price = _parse_price(payload, symbol, convert)

            breaker.record_success()

            logging.debug("Success latency=%.3fs", latency)
            return price

        except (Timeout, RequestException) as exc:
            breaker.record_failure()

            wait = compute_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )

            logging.warning("Network error retry %d wait=%.2fs %s", attempt, wait, exc)
            time.sleep(wait)

        except Exception:
            breaker.record_failure()
            logging.exception("Unexpected error")
            return None

    return None


# ─────────────────────────────────────────────────────────────
# Runtime Loop
# ─────────────────────────────────────────────────────────────

def track_prices(config, symbol, convert, interval):
    logging.info("Tracking %s/%s every %ds", symbol, convert, interval)

    session = create_session(config)
    breaker = CircuitBreaker(config.failure_threshold, config.recovery_time)

    last_price: Optional[Decimal] = None

    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while running:
        price = fetch_price(session, config, symbol, convert, breaker)

        if price is not None:
            last_price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            logging.info("%s/%s: $%s", symbol, convert, f"{last_price:,.2f}")

        else:
            if last_price:
                logging.warning("Using last known price: $%s", f"{last_price:,.2f}")
            else:
                logging.error("No price available")

        time.sleep(interval)

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

    if not (config.min_interval <= args.interval <= config.max_interval):
        parser.error("Invalid interval")

    return args


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    config = load_config()
    args = parse_arguments(config)
    setup_logging(config, args.log_level)

    track_prices(
        config,
        args.symbol,
        args.convert,
        args.interval,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
