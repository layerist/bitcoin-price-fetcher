#!/usr/bin/env python3
"""
CoinMarketCap cryptocurrency price tracker.

Improvements:
- Clear separation of concerns (config / logging / networking / runtime)
- Safer backoff math + centralized retry sleep
- Persistent headers on session
- Decimal for price precision
- Drift-corrected scheduling
- Cleaner error handling and logging
- Type hints tightened
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
from decimal import Decimal
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
    base = min(factor ** attempt, max_backoff)
    if jitter_mode == "full":
        return random.uniform(0.0, base)
    return min(base + random.random(), max_backoff)


def create_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "X-CMC_PRO_API_KEY": config.api_key,
        "Accept": "application/json",
        "User-Agent": "cmc-price-tracker/1.0",
    })

    retries = Retry(
        total=config.max_retries,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        backoff_factor=0,  # handled manually
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ─────────────────────────────────────────────────────────────
# API logic
# ─────────────────────────────────────────────────────────────

def fetch_price(
    session: requests.Session,
    config: Config,
    symbol: str,
    convert: str,
) -> Optional[Decimal]:

    params = {
        "symbol": symbol.upper(),
        "convert": convert.upper(),
    }

    for attempt in range(1, config.max_retries + 1):
        try:
            t0 = time.monotonic()
            logging.debug("Fetching %s (%s)", symbol.upper(), attempt)

            response = session.get(
                config.api_url,
                params=params,
                timeout=config.request_timeout,
            )
            response.raise_for_status()

            data = response.json()
            price = (
                data["data"][symbol.upper()]
                ["quote"][convert.upper()]
                ["price"]
            )

            logging.debug(
                "Request finished in %.2fs", time.monotonic() - t0
            )
            return Decimal(str(price))

        except HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                logging.warning("Rate limit exceeded (429)")
            wait = compute_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning("%s → retry in %.1fs", exc, wait)
            time.sleep(wait)

        except (Timeout, RequestException) as exc:
            wait = compute_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning("Network error: %s → retry in %.1fs", exc, wait)
            time.sleep(wait)

        except (KeyError, ValueError):
            logging.error("Malformed API response")
            return None

        except Exception:
            logging.exception("Unexpected error during fetch")
            return None

    logging.error("Max retries exceeded for %s", symbol.upper())
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
        symbol.upper(),
        convert.upper(),
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
                logging.info(
                    "%s → %s: $%s",
                    symbol.upper(),
                    convert.upper(),
                    f"{price:,.2f}",
                )
            else:
                logging.warning("Price fetch failed")

            next_tick += interval
            sleep_for = max(1.0, next_tick - time.monotonic())
            sleep_for += random.uniform(-0.5, 0.5)
            time.sleep(max(1.0, sleep_for))

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

    track_prices(config, args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception:
        logging.exception("Fatal error")
        sys.exit(1)
