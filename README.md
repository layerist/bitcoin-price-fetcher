# Bitcoin Price Fetcher

This Python script fetches the current price of Bitcoin in USD from the CoinMarketCap API every second and prints it to the console. The script can be stopped gracefully with a keyboard interrupt (Ctrl+C).

## Prerequisites

- Python 3.x
- `requests` library

## Installation

1. Clone the repository or download the script.
2. Install the `requests` library if you haven't already:

    ```bash
    pip install requests
    ```

3. Obtain your CoinMarketCap API key from [CoinMarketCap](https://coinmarketcap.com/api/).

## Usage

1. Replace `'your_api_key'` in the script with your actual CoinMarketCap API key.
2. Run the script:

    ```bash
    python bitcoin_price_fetcher.py
    ```

3. To stop the script, press `Ctrl+C`.

## Code Overview

- **API_KEY**: Your CoinMarketCap API key.
- **API_URL**: The endpoint URL for fetching cryptocurrency quotes.
- **PARAMS**: The parameters for the API request, including the cryptocurrency symbol (`BTC`) and the conversion currency (`USD`).
- **HEADERS**: The headers for the API request, including the API key.

### Functions

- `get_bitcoin_price()`: Makes a request to the CoinMarketCap API and returns the current price of Bitcoin in USD.
- `main()`: The main function that continuously fetches and prints the Bitcoin price every second. It handles keyboard interrupts to stop the script gracefully.

### Example Output

```
The current price of Bitcoin is $34987.23
The current price of Bitcoin is $34990.45
The current price of Bitcoin is $34985.67
...
```

## License

This project is licensed under the MIT License.
