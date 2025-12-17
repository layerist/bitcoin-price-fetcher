#!/usr/bin/env python3
"""
CoinMarketCap cryptocurrency price tracker.

Features:
- Resilient HTTP session with retries
- Exponential backoff with jitter
- Colorized console logging + rotating file logs
- Graceful shutdown on SIGINT / SIGTERM
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
    backoff_factor: int = 2
    max_backoff: int = 60
    request_timeout: int = 10

    jitter_mode: str = "random"  # "random" | "full"

    log_file: str = field(
        default_factory=lambda: f"crypto_price_{int(time.time())}.log"
    )
    max_log_size: int = 5 * 1024 * 1024  # 5 MB
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
        message = super().format(record)
        return f"{color}{message}{self.RESET}"


def setup_logging(config: Config, level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"

    # File logging
    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.max_log_size,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt))

    # Console logging
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColorFormatter(fmt))

    root.addHandler(file_handler)
    root.addHandler(console_handler)


# ─────────────────────────────────────────────────────────────
# Networking helpers
# ─────────────────────────────────────────────────────────────

def exponential_backoff(
    attempt: int,
    factor: int,
    max_backoff: int,
    jitter_mode: str,
) -> float:
    base = min(factor ** attempt, max_backoff)
    if jitter_mode == "full":
        return random.uniform(0, base)
    return min(base + random.random(), max_backoff)


def create_session(config: Config) -> requests.Session:
    session = requests.Session()

    retries = Retry(
        total=config.max_retries,
        backoff_factor=config.backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
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
) -> Optional[float]:

    headers = {"X-CMC_PRO_API_KEY": config.api_key}
    params = {
        "symbol": symbol.upper(),
        "convert": convert.upper(),
    }

    for attempt in range(1, config.max_retries + 1):
        try:
            start = time.monotonic()
            logging.debug(f"[Attempt {attempt}] Fetching {symbol.upper()}")

            response = session.get(
                config.api_url,
                headers=headers,
                params=params,
                timeout=config.request_timeout,
            )
            response.raise_for_status()

            payload = response.json()
            price = (
                payload.get("data", {})
                .get(symbol.upper(), {})
                .get("quote", {})
                .get(convert.upper(), {})
                .get("price")
            )

            if price is None:
                logging.error("Malformed API response: price missing")
                return None

            logging.debug(
                f"Request completed in {time.monotonic() - start:.2f}s"
            )
            return float(price)

        except HTTPError as exc:
            status = response.status_code if "response" in locals() else None
            if status == 429:
                logging.warning("Rate limit hit (429)")
            wait = exponential_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning(f"{exc} → retry in {wait:.1f}s")
            time.sleep(wait)

        except (Timeout, RequestException) as exc:
            wait = exponential_backoff(
                attempt,
                config.backoff_factor,
                config.max_backoff,
                config.jitter_mode,
            )
            logging.warning(f"Network error: {exc} → retry in {wait:.1f}s")
            time.sleep(wait)

        except ValueError:
            logging.error("Invalid JSON response from API")
            return None

        except Exception:
            logging.exception("Unexpected error during fetch")
            return None

    logging.error(f"❌ Max retries exceeded for {symbol.upper()}")
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
        f"🚀 Tracking {symbol.upper()} → {convert.upper()} every {interval}s"
    )

    session = create_session(config)
    running = True

    def stop_handler(*_: object) -> None:
        nonlocal running
        running = False
        logging.info("🛑 Shutdown signal received")

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    try:
        while running:
            price = fetch_price(session, config, symbol, convert)
            if price is not None:
                logging.info(
                    f"{symbol.upper()} → {convert.upper()}: ${price:,.2f}"
                )
            else:
                logging.warning("Failed to retrieve price")

            sleep_time = max(1, interval + random.uniform(-0.5, 0.5))
            time.sleep(sleep_time)

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
    parser.add_argument("command", nargs="?", default="track", choices=("track",))
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

    if args.command == "track":
        track_prices(config, args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Interrupted by user")
    except Exception:
        logging.exception("Fatal error")
        sys.exit(1)
