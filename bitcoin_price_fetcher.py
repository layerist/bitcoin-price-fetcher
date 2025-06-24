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
    "log_file": f"cryptocurrency_price_{int(time.time())}.log",
    "request_timeout": 10,
}

HEADERS = {"X-CMC_PRO_API_KEY": CONFIG["api_key"]}


def configure_logging(log_level: str = "INFO") -> None:
    """Set up logging to file and stdout."""
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    if logger.hasHandlers():
        logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # File handler
    file_handler = logging.FileHandler(CONFIG["log_file"])
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def fetch_price(symbol: str, convert: str) -> Optional[float]:
    """Fetch the current price of a cryptocurrency."""
    params = {"symbol": symbol.upper(), "convert": convert.upper()}

    for attempt in range(1, CONFIG["max_retries"] + 1):
        try:
            logging.info(f"Fetching {symbol.upper()} in {convert.upper()} (Attempt {attempt})...")
            response = requests.get(
                CONFIG["api_url"],
                headers=HEADERS,
                params=params,
                timeout=CONFIG["request_timeout"]
            )
            response.raise_for_status()

            data: Dict[str, Any] = response.json()
            price = data["data"][symbol.upper()]["quote"][convert.upper()]["price"]
            logging.debug(f"API response: {data}")
            return float(price)

        except KeyError as e:
            logging.error(f"Unexpected API structure: {e}")
            return None

        except (HTTPError, Timeout, RequestException) as e:
            wait_time = CONFIG["backoff_factor"] ** attempt + random.uniform(0, 1)
            logging.warning(f"Request failed: {e}. Retrying in {wait_time:.2f}s...")
            time.sleep(wait_time)

        except Exception as e:
            logging.critical("Unexpected error during API call", exc_info=True)
            return None

    logging.error(f"Exceeded maximum retry attempts ({CONFIG['max_retries']}).")
    return None


def track_prices(symbol: str, convert: str, interval: int) -> None:
    """Continuously track and log cryptocurrency price at fixed intervals."""
    logging.info(f"Tracking {symbol.upper()} to {convert.upper()} every {interval}s.")

    try:
        while True:
            price = fetch_price(symbol, convert)
            if price is not None:
                msg = f"{symbol.upper()} → {convert.upper()}: ${price:,.2f}"
                logging.info(msg)
                print(msg)
            else:
                logging.warning(f"Price fetch failed for {symbol.upper()} → {convert.upper()}.")
            time.sleep(interval)

    except KeyboardInterrupt:
        logging.info("Process interrupted by user.")
        print("\nStopped by user.")

    except Exception as e:
        logging.critical("Fatal error during tracking.", exc_info=True)
        print(f"Critical error: {e}")


def parse_arguments() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="Real-time cryptocurrency price tracker.")
    parser.add_argument("--symbol", default=CONFIG["default_symbol"], help="Symbol of cryptocurrency (e.g., BTC).")
    parser.add_argument("--convert", default=CONFIG["default_convert"], help="Fiat or crypto to convert into (e.g., USD).")
    parser.add_argument("--interval", type=int, default=CONFIG["default_interval"], help="Polling interval in seconds.")
    parser.add_argument("--log-level", default="INFO", help="Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL.")

    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("Interval must be a positive integer.")
    return args


def main() -> None:
    args = parse_arguments()
    configure_logging(args.log_level)
    track_prices(args.symbol, args.convert, args.interval)


if __name__ == "__main__":
    main()
