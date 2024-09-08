import requests
import time
import logging
import sys
import argparse

API_KEY = 'your_api_key'
API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
DEFAULT_SYMBOL = 'BTC'
DEFAULT_CONVERT = 'USD'
DEFAULT_INTERVAL = 60
MAX_RETRIES = 5
BACKOFF_FACTOR = 2

HEADERS = {
    'X-CMC_PRO_API_KEY': API_KEY
}

logging.basicConfig(filename='cryptocurrency_price.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def get_cryptocurrency_price(symbol=DEFAULT_SYMBOL, convert=DEFAULT_CONVERT):
    params = {
        'symbol': symbol,
        'convert': convert
    }
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(API_URL, headers=HEADERS, params=params)
            response.raise_for_status()
            data = response.json()
            price = data['data'][symbol]['quote'][convert]['price']
            return price
        except requests.HTTPError as e:
            logging.error(f"HTTP error {e.response.status_code}: {e.response.text}")
        except requests.RequestException as e:
            logging.error(f"Request error: {e}")
        except KeyError as e:
            logging.error(f"Data parsing error: Missing key {e}")
        time.sleep(BACKOFF_FACTOR ** attempt)  # Exponential backoff
    return None

def main(symbol=DEFAULT_SYMBOL, convert=DEFAULT_CONVERT, interval=DEFAULT_INTERVAL):
    logging.info(f"Tracking price for {symbol} every {interval}s")
    try:
        while True:
            price = get_cryptocurrency_price(symbol, convert)
            if price is not None:
                print(f"The current price of {symbol} in {convert} is ${price:.2f}")
                logging.info(f"Price of {symbol} in {convert}: ${price:.2f}")
            else:
                print(f"Failed to retrieve the price of {symbol} in {convert}.")
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Script stopped by the user.")
        print("Script stopped by the user.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Track cryptocurrency prices.')
    parser.add_argument('--symbol', type=str, default=DEFAULT_SYMBOL, help='Cryptocurrency symbol (e.g., BTC)')
    parser.add_argument('--convert', type=str, default=DEFAULT_CONVERT, help='Currency to convert to (e.g., USD)')
    parser.add_argument('--interval', type=int, default=DEFAULT_INTERVAL, help='Interval in seconds between updates')
    args = parser.parse_args()

    main(symbol=args.symbol, convert=args.convert, interval=args.interval)
