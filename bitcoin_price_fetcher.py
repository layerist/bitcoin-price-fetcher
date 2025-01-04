import requests
import time
import logging
import argparse
import sys
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

# Headers for the API request
HEADERS = {
    "X-CMC_PRO_API_KEY": CONFIG["api_key"]
}

# Logging configuration
def configure_logging(log_level: str = "INFO") -> None:
    """
    Configures logging settings for the application.

    :param log_level: The logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        filename=CONFIG["log_file"],
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(formatter)
    logging.getLogger().addHandler(console_handler)

def fetch_cryptocurrency_price(symbol: str, convert: str) -> Optional[float]:
    """
    Fetch the latest price of a cryptocurrency from CoinMarketCap API.
    
    :param symbol: The cryptocurrency symbol (e.g., 'BTC').
    :param convert: The currency to convert to (e.g., 'USD').
    :return: The current price or None if the request fails.
    """
    params = {"symbol": symbol, "convert": convert}
    for attempt in range(1, CONFIG["max_retries"] + 1):
        try:
            logging.info(f"Fetching price for {symbol} in {convert} (Attempt {attempt}/{CONFIG['max_retries']}).")
            response = requests.get(
                CONFIG["api_url"],
                headers=HEADERS,
                params=params,
                timeout=CONFIG["request_timeout"]
            )
            response.raise_for_status()
            data = response.json()

            # Extract price
            price = data["data"][symbol.upper()]["quote"][convert.upper()]["price"]
            logging.debug(f"API response: {data}")
            return price
        except KeyError as e:
            logging.error(f"Response structure missing key: {e}. Aborting further attempts.")
            return None
        except (HTTPError, RequestException) as e:
            wait_time = CONFIG["backoff_factor"] ** attempt
            logging.warning(f"Attempt {attempt} failed: {e}. Retrying in {wait_time} seconds.")
            time.sleep(wait_time)
        except Exception as e:
            logging.critical(f"Unexpected error: {e}", exc_info=True)
            break

    logging.error(f"Failed to retrieve price for {symbol} after {CONFIG['max_retries']} attempts.")
    return None

def track_prices(symbol: str, convert: str, interval: int) -> None:
    """
    Tracks cryptocurrency prices at regular intervals.

    :param symbol: The cryptocurrency symbol to track.
    :param convert: The currency to convert the price to.
    :param interval: Time interval between updates, in seconds.
    """
    logging.info(f"Starting price tracking for {symbol.upper()} in {convert.upper()}, interval: {interval}s.")
    try:
        while True:
            price = fetch_cryptocurrency_price(symbol, convert)
            if price is not None:
                print(f"The current price of {symbol.upper()} in {convert.upper()} is ${price:.2f}")
                logging.info(f"Price of {symbol.upper()} in {convert.upper()}: ${price:.2f}")
            else:
                print(f"Failed to retrieve the price of {symbol.upper()} in {convert.upper()}. Check logs for details.")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Script interrupted by the user.")
        print("\nScript stopped by the user.")
    except Exception as e:
        logging.critical(f"Unexpected error: {e}", exc_info=True)
        print(f"An error occurred: {e}")
    finally:
        sys.exit(0)

if __name__ == "__main__":
    # Command-line argument parsing
    parser = argparse.ArgumentParser(description="Track cryptocurrency prices in real-time.")
    parser.add_argument("--symbol", type=str, default=CONFIG["default_symbol"], help="Cryptocurrency symbol (e.g., BTC).")
    parser.add_argument("--convert", type=str, default=CONFIG["default_convert"], help="Currency to convert to (e.g., USD).")
    parser.add_argument("--interval", type=int, default=CONFIG["default_interval"], help="Interval in seconds between updates.")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).")

    # Parse arguments
    args = parser.parse_args()

    # Validate interval
    if args.interval <= 0:
        print("Error: Interval must be a positive integer.")
        logging.error("Interval must be a positive integer.")
        sys.exit(1)
    else:
        # Configure logging
        configure_logging(args.log_level)
        # Start tracking
        track_prices(symbol=args.symbol, convert=args.convert, interval=args.interval)
