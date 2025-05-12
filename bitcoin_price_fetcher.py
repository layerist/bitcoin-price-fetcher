import requests
import time
import logging
import argparse
import sys
import random
from typing import Optional
from requests.exceptions import HTTPError, RequestException

# Configuration
CONFIG = {
    "api_key": "your_api_key",
    "api_url": "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest",
    "default_symbol": "BTC",
    "default_convert": "USD",
    "default_interval": 60,
    "max_retries": 5,
    "backoff_factor": 2,
    "log_file": "cryptocurrency_price.log",
    "request_timeout": 10,
}

HEADERS = {"X-CMC_PRO_API_KEY": CONFIG["api_key"]}


def configure_logging(log_level: str = "INFO") -> None:
    """Configure logging for both file and console outputs."""
    log = logging.getLogger()
    log.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Avoid duplicate handlers
    if log.hasHandlers():
        log.handlers.clear()

    file_handler = logging.FileHandler(CONFIG["log_file"])
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    log.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    log.addHandler(console_handler)


def fetch_price(symbol: str, convert: str) -> Optional[float]:
    """Fetch the current price for the given cryptocurrency symbol."""
    params = {"symbol": symbol.upper(), "convert": convert.upper()}

    for attempt in range(1, CONFIG["max_retries"] + 1):
        try:
            logging.info(f"Requesting {symbol.upper()} in {convert.upper()} (Attempt {attempt})...")
            response = requests.get(
                CONFIG["api_url"],
                headers=HEADERS,
                params=params,
                timeout=CONFIG["request_timeout"],
            )
            response.raise_for_status()

            data = response.json()
            price = data["data"][symbol.upper()]["quote"][convert.upper()]["price"]
            logging.debug(f"Response data: {data}")
            return price

        except (KeyError, TypeError) as e:
            logging.error(f"Invalid response structure: {e}")
            return None

        except (HTTPError, RequestException) as e:
            wait_time = CONFIG["backoff_factor"] ** attempt + random.uniform(0, 1)
            logging.warning(f"Request failed: {e}. Retrying in {wait_time:.2f}s...")
            time.sleep(wait_time)

        except Exception as e:
            logging.critical("Unhandled exception while fetching price", exc_info=True)
            return None

    logging.error(f"Failed to fetch price after {CONFIG['max_retries']} attempts.")
    return None


def track_prices(symbol: str, convert: str, interval: int) -> None:
    """Track and log the cryptocurrency price at regular intervals."""
    logging.info(f"Tracking {symbol.upper()} in {convert.upper()} every {interval} seconds.")

    try:
        while True:
            price = fetch_price(symbol, convert)
            if price is not None:
                message = f"{symbol.upper()} in {convert.upper()}: ${price:.2f}"
                logging.info(message)
                print(message)
            else:
                print(f"Could not retrieve price for {symbol.upper()} in {convert.upper()}.")

            time.sleep(interval)

    except KeyboardInterrupt:
        logging.info("Tracking stopped by user.")
        print("\nTracking stopped.")

    except Exception as e:
        logging.critical("Unexpected error occurred during tracking.", exc_info=True)
        print(f"Error: {e}")


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Track cryptocurrency prices in real-time.")
    parser.add_argument("--symbol", type=str, default=CONFIG["default_symbol"], help="Cryptocurrency symbol (e.g., BTC)")
    parser.add_argument("--convert", type=str, default=CONFIG["default_convert"], help="Currency to convert to (e.g., USD)")
    parser.add_argument("--interval", type=int, default=CONFIG["default_interval"], help="Seconds between updates")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL")

    args = parser.parse_args()

    if args.interval <= 0:
        parser.error("Interval must be a positive integer.")

    return args


def main():
    args = parse_arguments()
    configure_logging(args.log_level)
    track_prices(args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    main()
