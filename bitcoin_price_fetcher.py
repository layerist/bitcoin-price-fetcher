import os
import sys
import time
import random
import signal
import logging
import argparse
import requests
from dataclasses import dataclass
from typing import Optional
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


def load_config() -> Config:
    """Load API key and return Config object."""
    api_key = os.getenv("CMC_API_KEY")
    if not api_key:
        logging.critical("CMC_API_KEY environment variable is not set.")
        sys.exit("Error: Missing CMC_API_KEY environment variable.")
    return Config(api_key=api_key)


def setup_logging(log_file: str, level: str = "INFO") -> None:
    """Set up logging to console and file."""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def exponential_backoff(attempt: int, factor: int, max_backoff: int) -> float:
    """Return exponential backoff time with jitter."""
    return min((factor ** attempt) + random.random(), max_backoff)


def fetch_price(
    session: requests.Session,
    config: Config,
    symbol: str,
    convert: str
) -> Optional[float]:
    """Fetch the current price of a cryptocurrency."""
    headers = {"X-CMC_PRO_API_KEY": config.api_key}
    params = {"symbol": symbol.upper(), "convert": convert.upper()}

    for attempt in range(1, config.max_retries + 1):
        try:
            logging.info(f"[Attempt {attempt}] Fetching {symbol.upper()} in {convert.upper()}...")
            response = session.get(
                config.api_url,
                headers=headers,
                params=params,
                timeout=config.request_timeout
            )
            response.raise_for_status()

            data = response.json()
            price = (
                data.get("data", {})
                .get(symbol.upper(), {})
                .get("quote", {})
                .get(convert.upper(), {})
                .get("price")
            )

            if price is None:
                logging.error("Malformed response: price not found.")
                return None

            logging.debug(f"API response: {data}")
            return float(price)

        except (HTTPError, Timeout, RequestException) as e:
            wait_time = exponential_backoff(attempt, config.backoff_factor, config.max_backoff)
            logging.warning(f"Request failed: {e}. Retrying in {wait_time:.2f}s...")
            time.sleep(wait_time)

        except ValueError as e:
            logging.error(f"JSON decode error: {e}")
            return None

        except Exception:
            logging.exception("Unexpected error during price fetch.")
            return None

    logging.error(f"Max retries exceeded for {symbol.upper()} → {convert.upper()}")
    return None


def track_prices(config: Config, symbol: str, convert: str, interval: int) -> None:
    """Track and log cryptocurrency prices at regular intervals."""
    logging.info(f"Tracking {symbol.upper()} → {convert.upper()} every {interval}s.")

    stop = False

    def handle_sigterm(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle_sigterm)
    signal.signal(signal.SIGTERM, handle_sigterm)

    with requests.Session() as session:
        while not stop:
            price = fetch_price(session, config, symbol, convert)
            if price is not None:
                message = f"{symbol.upper()} → {convert.upper()}: ${price:,.2f}"
                logging.info(message)
                print(message)
            else:
                logging.warning(f"Failed to fetch price for {symbol.upper()} → {convert.upper()}")
            time.sleep(interval)

    logging.info("Tracking stopped.")


def parse_arguments(config: Config) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Real-time cryptocurrency price tracker.")
    parser.add_argument("--symbol", default=config.default_symbol, help="Cryptocurrency symbol (e.g., BTC).")
    parser.add_argument("--convert", default=config.default_convert, help="Conversion currency (e.g., USD).")
    parser.add_argument("--interval", type=int, default=config.default_interval, help="Update interval in seconds.")
    parser.add_argument("--log-level", default="INFO", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("Interval must be a positive integer.")

    return args


def main() -> None:
    config = load_config()
    args = parse_arguments(config)
    setup_logging(config.log_file, args.log_level)
    track_prices(config, args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    main()
