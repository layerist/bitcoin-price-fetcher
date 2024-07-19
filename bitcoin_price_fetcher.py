import requests
import time
import logging

# Replace 'your_api_key' with your actual CoinMarketCap API key.
API_KEY = 'your_api_key'
API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
DEFAULT_SYMBOL = 'BTC'
DEFAULT_CONVERT = 'USD'
DEFAULT_INTERVAL = 60  # Интервал обновления в секундах

HEADERS = {
    'X-CMC_PRO_API_KEY': API_KEY
}

logging.basicConfig(filename='bitcoin_price.log', level=logging.INFO, 
                    format='%(asctime)s - %(levelname)s - %(message)s')

def get_cryptocurrency_price(symbol=DEFAULT_SYMBOL, convert=DEFAULT_CONVERT):
    params = {
        'symbol': symbol,
        'convert': convert
    }
    try:
        response = requests.get(API_URL, headers=HEADERS, params=params)
        response.raise_for_status()  # Проверка на успешный ответ
        data = response.json()
        price = data['data'][symbol]['quote'][convert]['price']
        return price
    except requests.RequestException as e:
        logging.error(f"Error fetching data: {e}")
        return None
    except KeyError:
        logging.error("Error parsing data: incorrect response format")
        return None

def main(symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL):
    try:
        while True:
            price = get_cryptocurrency_price(symbol)
            if price is not None:
                print(f"The current price of {symbol} is ${price:.2f}")
                logging.info(f"The current price of {symbol} is ${price:.2f}")
            else:
                print("Failed to retrieve the price. Check logs for details.")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("Script stopped by the user.")
        logging.info("Script stopped by the user.")

if __name__ == '__main__':
    main()
