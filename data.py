import ccxt
import pandas as pd

_exchange = ccxt.binance()

def fetch_data(symbol, timeframe, limit=300):
    try:
        ohlcv = _exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
        return df
    except Exception as e:
        print(f"Lỗi khi lấy dữ liệu {symbol} {timeframe}: {e}")
        return None
