import os
import sys
import time
import random
import logging
import argparse
import requests
from typing import Optional, Dict, Any
from requests.exceptions import HTTPError, Timeout, RequestException

# Configuration
CONFIG = {
    "api_key": os.getenv("CMC_API_KEY", "your_api_key"),
    "api_url": "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
    "default_symbol": "BTC",
    "default_convert": "USD",
    "default_interval": 60,
    "max_retries": 5,
    "backoff_factor": 2,
    "max_backoff": 60,
    "log_file": f"crypto_price_{int(time.time())}.log",
    "request_timeout": 10,
}

HEADERS = {"X-CMC_PRO_API_KEY": CONFIG["api_key"]}


def configure_logging(log_level: str = "INFO") -> None:
    """Configure logging to both console and file."""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = logging.FileHandler(CONFIG["log_file"])
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def exponential_backoff(attempt: int) -> float:
    """Calculate exponential backoff with jitter."""
    return min(CONFIG["backoff_factor"] ** attempt + random.uniform(0, 1), CONFIG["max_backoff"])


def fetch_price(symbol: str, convert: str) -> Optional[float]:
    """Fetch the current price of a cryptocurrency from CoinMarketCap."""
    params = {"symbol": symbol.upper(), "convert": convert.upper()}

    for attempt in range(1, CONFIG["max_retries"] + 1):
        try:
            logging.info(f"Requesting {symbol.upper()} in {convert.upper()} (attempt {attempt})...")
            response = requests.get(
                CONFIG["api_url"],
                headers=HEADERS,
                params=params,
                timeout=CONFIG["request_timeout"]
            )
            response.raise_for_status()

            data: Dict[str, Any] = response.json()
            price = data["data"][symbol.upper()]["quote"][convert.upper()]["price"]
            logging.debug(f"Full API response: {data}")
            return float(price)

        except KeyError as e:
            logging.error(f"Malformed API response: {e}")
            return None

        except (HTTPError, Timeout, RequestException) as e:
            wait = exponential_backoff(attempt)
            logging.warning(f"Request failed: {e}. Retrying in {wait:.2f}s...")
            time.sleep(wait)

        except Exception as e:
            logging.exception("Unexpected error while fetching price")
            return None

    logging.error(f"Max retries exceeded for {symbol.upper()} → {convert.upper()}")
    return None


def track_prices(symbol: str, convert: str, interval: int) -> None:
    """Track and log cryptocurrency prices at regular intervals."""
    logging.info(f"Starting price tracker for {symbol.upper()} → {convert.upper()} every {interval}s.")

    try:
        while True:
            price = fetch_price(symbol, convert)
            if price is not None:
                message = f"{symbol.upper()} → {convert.upper()}: ${price:,.2f}"
                logging.info(message)
                print(message)
            else:
                logging.warning(f"Failed to fetch price for {symbol.upper()} → {convert.upper()}")
            time.sleep(interval)

    except KeyboardInterrupt:
        logging.info("Tracker interrupted by user.")
        print("\nTracker stopped.")

    except Exception:
        logging.critical("Fatal error during price tracking", exc_info=True)
        print("A critical error occurred. Exiting...")


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Real-time cryptocurrency price tracker.")
    parser.add_argument("--symbol", default=CONFIG["default_symbol"], help="Cryptocurrency symbol (e.g., BTC).")
    parser.add_argument("--convert", default=CONFIG["default_convert"], help="Conversion currency (e.g., USD).")
    parser.add_argument("--interval", type=int, default=CONFIG["default_interval"], help="Update interval in seconds.")
    parser.add_argument("--log-level", default="INFO", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("Interval must be greater than 0.")

    return args


def main() -> None:
    args = parse_arguments()
    configure_logging(args.log_level)
    track_prices(args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    main()
