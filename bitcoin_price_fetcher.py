import requests
import time

# Replace 'your_api_key' with your actual CoinMarketCap API key.
API_KEY = 'your_api_key'
API_URL = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
PARAMS = {
    'symbol': 'BTC',
    'convert': 'USD'
}
HEADERS = {
    'X-CMC_PRO_API_KEY': API_KEY
}

def get_bitcoin_price():
    response = requests.get(API_URL, headers=HEADERS, params=PARAMS)
    data = response.json()
    price = data['data']['BTC']['quote']['USD']['price']
    return price

def main():
    try:
        while True:
            price = get_bitcoin_price()
            print(f"The current price of Bitcoin is ${price:.2f}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("Script stopped by the user.")

if __name__ == '__main__':
    main()
