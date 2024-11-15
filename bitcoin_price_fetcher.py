import requests
import time
import logging
import argparse
from requests.exceptions import HTTPError, RequestException

# Configuration
API_KEY = 'your_api_key'
API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
DEFAULT_SYMBOL = 'BTC'
DEFAULT_CONVERT = 'USD'
DEFAULT_INTERVAL = 60
MAX_RETRIES = 5
BACKOFF_FACTOR = 2

# Headers for the API request
HEADERS = {
    'X-CMC_PRO_API_KEY': API_KEY
}

# Logging configuration
logging.basicConfig(
    filename='cryptocurrency_price.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def fetch_cryptocurrency_price(symbol: str, convert: str) -> float | None:
    """
    Fetch the latest price of a cryptocurrency from CoinMarketCap API.
    
    :param symbol: The cryptocurrency symbol (e.g., 'BTC').
    :param convert: The currency to convert to (e.g., 'USD').
    :return: The current price or None if the request fails.
    """
    params = {'symbol': symbol, 'convert': convert}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logging.info(f"Fetching price for {symbol} in {convert} (Attempt {attempt}/{MAX_RETRIES}).")
            response = requests.get(API_URL, headers=HEADERS, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Extract price
            price = data['data'][symbol]['quote'][convert]['price']
            return price
        except KeyError as e:
            logging.error(f"Response structure missing key {e}. Aborting further attempts.")
            return None
        except (HTTPError, RequestException) as e:
            logging.warning(f"Attempt {attempt} failed: {e}. Retrying after {BACKOFF_FACTOR ** attempt} seconds.")
            time.sleep(BACKOFF_FACTOR ** attempt)
        except Exception as e:
            logging.critical(f"Unexpected error: {e}", exc_info=True)
            break

    logging.error(f"Failed to retrieve price for {symbol} after {MAX_RETRIES} attempts.")
    return None

def track_prices(symbol: str, convert: str, interval: int) -> None:
    """
    Tracks cryptocurrency prices at regular intervals.

    :param symbol: The cryptocurrency symbol to track.
    :param convert: The currency to convert the price to.
    :param interval: Time interval between updates, in seconds.
    """
    logging.info(f"Starting price tracking for {symbol} in {convert}, interval: {interval}s.")
    try:
        while True:
            price = fetch_cryptocurrency_price(symbol, convert)
            if price is not None:
                print(f"The current price of {symbol} in {convert} is ${price:.2f}")
                logging.info(f"Price of {symbol} in {convert}: ${price:.2f}")
            else:
                print(f"Failed to retrieve the price of {symbol} in {convert}. Check logs for details.")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Script interrupted by the user.")
        print("\nScript stopped by the user.")
    except Exception as e:
        logging.critical(f"Unexpected error: {e}", exc_info=True)
        print(f"An error occurred: {e}")

if __name__ == '__main__':
    # Command-line argument parsing
    parser = argparse.ArgumentParser(description='Track cryptocurrency prices in real-time.')
    parser.add_argument('--symbol', type=str, default=DEFAULT_SYMBOL, help='Cryptocurrency symbol (e.g., BTC).')
    parser.add_argument('--convert', type=str, default=DEFAULT_CONVERT, help='Currency to convert to (e.g., USD).')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='Interval in seconds between updates.')

    # Parse arguments
    args = parser.parse_args()

    # Validate interval
    if args.interval <= 0:
        logging.error("Interval must be a positive integer.")
        print("Error: Interval must be a positive integer.")
    else:
        # Start tracking
        track_prices(symbol=args.symbol, convert=args.convert, interval=args.interval)
