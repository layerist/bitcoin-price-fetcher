import os
import sys
import time
import random
import signal
import logging
import argparse
import requests
from dataclasses import dataclass
from typing import Optional, Dict, Any
from logging.handlers import RotatingFileHandler
from requests.exceptions import HTTPError, Timeout, RequestException


@dataclass
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
    log_file: str = f"crypto_price_{int(time.time())}.log"
    max_log_size: int = 5 * 1024 * 1024  # 5 MB
    backup_count: int = 3  # keep last 3 logs


def load_config() -> Config:
    """Load API key and return Config object."""
    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        sys.exit("❌ Missing required environment variable: CMC_API_KEY")
    return Config(api_key=api_key)


def setup_logging(log_file: str, level: str = "INFO") -> None:
    """Set up logging with rotation and console output."""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")

    # File logger with rotation
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=Config.max_log_size,
        backupCount=Config.backup_count,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console logger
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def exponential_backoff(attempt: int, factor: int, max_backoff: int) -> float:
    """Return exponential backoff time with jitter."""
    wait = min((factor ** attempt) + random.uniform(0, 1), max_backoff)
    return wait


def fetch_price(
    session: requests.Session,
    config: Config,
    symbol: str,
    convert: str
) -> Optional[float]:
    """Fetch the current cryptocurrency price."""
    headers = {"X-CMC_PRO_API_KEY": config.api_key}
    params = {"symbol": symbol.upper(), "convert": convert.upper()}

    for attempt in range(1, config.max_retries + 1):
        try:
            logging.debug(f"[Attempt {attempt}] Fetching {symbol.upper()} → {convert.upper()}...")
            response = session.get(
                config.api_url,
                headers=headers,
                params=params,
                timeout=config.request_timeout
            )
            response.raise_for_status()
            data: Dict[str, Any] = response.json()

            # Extract price safely
            price = (
                data.get("data", {})
                .get(symbol.upper(), {})
                .get("quote", {})
                .get(convert.upper(), {})
                .get("price")
            )

            if price is None:
                logging.error("Malformed API response: 'price' field missing.")
                return None

            return float(price)

        except (HTTPError, Timeout, RequestException) as e:
            wait_time = exponential_backoff(attempt, config.backoff_factor, config.max_backoff)
            logging.warning(f"Request failed ({e}). Retrying in {wait_time:.2f}s...")
            time.sleep(wait_time)

        except ValueError as e:
            logging.error(f"Invalid JSON received: {e}")
            return None

        except Exception as e:
            logging.exception(f"Unexpected error while fetching price: {e}")
            return None

    logging.error(f"Exceeded max retries for {symbol.upper()} → {convert.upper()}.")
    return None


def track_prices(config: Config, symbol: str, convert: str, interval: int) -> None:
    """Continuously track and log cryptocurrency prices."""
    logging.info(f"🚀 Tracking {symbol.upper()} → {convert.upper()} every {interval}s.")
    stop = False

    def handle_signal(*_):
        nonlocal stop
        stop = True
        logging.info("🛑 Stopping price tracker...")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    with requests.Session() as session:
        while not stop:
            price = fetch_price(session, config, symbol, convert)
            if price is not None:
                logging.info(f"{symbol.upper()} → {convert.upper()}: ${price:,.2f}")
            else:
                logging.warning(f"Failed to fetch {symbol.upper()} price.")
            time.sleep(interval)


def parse_arguments(config: Config) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="A simple cryptocurrency price tracker using the CoinMarketCap API."
    )

    parser.add_argument(
        "command",
        nargs="?",
        choices=["track"],
        default="track",
        help="Command to run (default: track)."
    )

    parser.add_argument("--symbol", default=config.default_symbol, help="Cryptocurrency symbol (e.g., BTC).")
    parser.add_argument("--convert", default=config.default_convert, help="Conversion currency (e.g., USD).")
    parser.add_argument("--interval", type=int, default=config.default_interval, help="Update interval in seconds.")
    parser.add_argument("--log-level", default="INFO", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("--interval must be a positive integer.")

    return args


def main() -> None:
    config = load_config()
    args = parse_arguments(config)
    setup_logging(config.log_file, args.log_level)

    if args.command == "track":
        track_prices(config, args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Interrupted by user. Exiting...")
    except Exception as e:
        logging.exception(f"Fatal error: {e}")
        sys.exit(1)
