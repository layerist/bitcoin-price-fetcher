#!/usr/bin/env python3
"""
CoinMarketCap cryptocurrency price tracker.

Improvements:
- Stronger typing and stricter validation
- Explicit retry classification (retryable vs fatal)
- Monotonic, drift-free scheduler with hard alignment
- Safer Decimal quantization
- Cleaner shutdown handling
- Better separation of retry logic from parsing
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
from requests.adapters import HTTPAdapter, Retry
from requests.exceptions import HTTPError, Timeout, RequestException


# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Config:
    api_key: str
    api_url: str = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"

    default_symbol: str = "BTC"
    default_convert: str = "USD"
    default_interval: int = 60

    max_retries: int = 5
    backoff_factor: float = 2.0
    max_backoff: float = 60.0
    request_timeout: int = 10

    jitter_mode: str = "random"  # "random" | "full"

    log_file: str = field(
        default_factory=lambda: f"crypto_price_{int(time.time())}.log"
    )
    max_log_size: int = 5 * 1024 * 1024
    backup_count: int = 3


def load_config() -> Config:
    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        sys.exit("❌ Missing required environment variable: CMC_API_KEY")
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

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{super().format(record)}{self.RESET}"


def setup_logging(config: Config, level: str) -> None:
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"

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
# Networking helpers
# ─────────────────────────────────────────────────────────────

def compute_backoff(
    attempt: int,
    factor: float,
    max_backoff: float,
    jitter_mode: str,
) -> float:
    exp = min(factor ** attempt, max_backoff)
    if jitter_mode == "full":
        return random.uniform(0.0, exp)
    return min(exp + random.random(), max_backoff)


def create_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "X-CMC_PRO_API_KEY": config.api_key,
        "Accept": "application/json",
        "User-Agent": "cmc-price-tracker/1.1",
    })

    retries = Retry(
        total=config.max_retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        backoff_factor=0,  # manual backoff
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ─────────────────────────────────────────────────────────────
# API logic
# ─────────────────────────────────────────────────────────────

def _parse_price(
    payload: dict,
    symbol: str,
    convert: str,
) -> Decimal:
    try:
        raw = (
            payload["data"][symbol]
            ["quote"][convert]
            ["price"]
        )
        return Decimal(str(raw))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("Invalid API payload") from exc


def fetch_price(
    session: requests.Session,
    config: Config,
    symbol: str,
    convert: str,
) -> Optional[Decimal]:

    params = {
        "symbol": symbol,
        "convert": convert,
    }

    for attempt in range(1, config.max_retries + 1):
        try:
            started = time.monotonic()
            response = session.get(
                config.api_url,
                params=params,
                timeout=config.request_timeout,
            )

            if response.status_code == 429:
                raise HTTPError("Rate limited", response=response)

            response.raise_for_status()
            price = _parse_price(response.json(), symbol, convert)

            logging.debug(
                "Fetched %s in %.2fs",
                symbol,
                time.monotonic() - started,
            )
            return price

        except HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            retryable = status in (429, 500, 502, 503, 504)

            if not retryable:
                logging.error("HTTP %s: %s", status, exc)
                return None

            wait = compute_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning("HTTP %s → retry in %.1fs", status, wait)
            time.sleep(wait)

        except (Timeout, RequestException) as exc:
            wait = compute_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning("Network error → retry in %.1fs: %s", wait, exc)
            time.sleep(wait)

        except ValueError:
            logging.error("Malformed API response")
            return None

        except Exception:
            logging.exception("Unexpected fetch error")
            return None

    logging.error("Max retries exceeded for %s", symbol)
    return None


# ─────────────────────────────────────────────────────────────
# Runtime loop
# ─────────────────────────────────────────────────────────────

def track_prices(
    config: Config,
    symbol: str,
    convert: str,
    interval: int,
) -> None:

    logging.info(
        "Tracking %s → %s every %ds",
        symbol,
        convert,
        interval,
    )

    session = create_session(config)
    running = True
    next_tick = time.monotonic()

    def stop_handler(*_: object) -> None:
        nonlocal running
        running = False
        logging.info("Shutdown signal received")

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    try:
        while running:
            price = fetch_price(session, config, symbol, convert)

            if price is not None:
                price = price.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
                logging.info(
                    "%s → %s: $%s",
                    symbol,
                    convert,
                    f"{price:,.2f}",
                )
            else:
                logging.warning("Price fetch failed")

            next_tick += interval
            sleep_for = max(0.0, next_tick - time.monotonic())
            time.sleep(sleep_for)

    finally:
        session.close()
        logging.info("Session closed. Exiting.")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def parse_arguments(config: Config) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cryptocurrency price tracker (CoinMarketCap API)"
    )
    parser.add_argument("--symbol", default=config.default_symbol)
    parser.add_argument("--convert", default=config.default_convert)
    parser.add_argument("--interval", type=int, default=config.default_interval)
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")

    return args


def main() -> None:
    config = load_config()
    args = parse_arguments(config)
    setup_logging(config, args.log_level)

    track_prices(
        config,
        args.symbol.upper(),
        args.convert.upper(),
        args.interval,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception:
        logging.exception("Fatal error")
        sys.exit(1)
