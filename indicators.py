import pandas as pd
import ta

def calculate_indicators(ohlcv_df, config=None, timeframe=None):
    min_window = 200
    if ohlcv_df is None or len(ohlcv_df) < min_window:
        print(f"[ERROR] DataFrame quá nhỏ ({len(ohlcv_df) if ohlcv_df is not None else 0}), cần >= {min_window}")
        return None

    indicators = {}
    indicators['ema200'] = ta.trend.ema_indicator(ohlcv_df['close'], window=200)
    indicators['ma50'] = ta.trend.sma_indicator(ohlcv_df['close'], window=50)
    indicators['macd'] = ta.trend.macd(ohlcv_df['close'])
    indicators['macd_signal'] = ta.trend.macd_signal(ohlcv_df['close'])
    indicators['rsi'] = ta.momentum.rsi(ohlcv_df['close'], window=14)
    indicators['adx'] = ta.trend.adx(ohlcv_df['high'], ohlcv_df['low'], ohlcv_df['close'], window=14)

    typical_price = (ohlcv_df['high'] + ohlcv_df['low'] + ohlcv_df['close']) / 3
    cum_tp_vol = (typical_price * ohlcv_df['volume']).cumsum()
    cum_vol = ohlcv_df['volume'].cumsum()
    indicators['vwap'] = cum_tp_vol / cum_vol

    def supertrend(df, period=10, multiplier=3):
        atr = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=period)
        hl2 = (df['high'] + df['low']) / 2
        basic_ub = hl2 + multiplier * atr
        basic_lb = hl2 - multiplier * atr
        final_ub = basic_ub.copy()
        final_lb = basic_lb.copy()
        supertrend_list = [1]
        for i in range(1,len(df)):
            if df['close'].iloc[i-1] > final_ub.iloc[i-1]:
                final_ub.iloc[i] = min(basic_ub.iloc[i], final_ub.iloc[i-1])
            else:
                final_ub.iloc[i] = basic_ub.iloc[i]
            if df['close'].iloc[i-1] < final_lb.iloc[i-1]:
                final_lb.iloc[i] = max(basic_lb.iloc[i], final_lb.iloc[i-1])
            else:
                final_lb.iloc[i] = basic_lb.iloc[i]
            if df['close'].iloc[i] > final_ub.iloc[i-1]:
                supertrend_list.append(1)
            elif df['close'].iloc[i] < final_lb.iloc[i-1]:
                supertrend_list.append(-1)
            else:
                supertrend_list.append(supertrend_list[-1])
        return pd.Series(supertrend_list, index=df.index)
    indicators['supertrend'] = supertrend(ohlcv_df)

    def range_filter(df, threshold=1.5):
        rng = df['high'] - df['low']
        return (rng > (rng.rolling(window=20).mean() * threshold)).astype(int)
    indicators['range_filter'] = range_filter(ohlcv_df)

    indicators['atr'] = ta.volatility.average_true_range(
        ohlcv_df['high'], ohlcv_df['low'], ohlcv_df['close'], window=14
    )

    def chaikin_money_flow(df, window=20):
        mfv = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low']) * df['volume']
        mfv = mfv.replace([float('inf'), -float('inf')], 0).fillna(0)
        cmf = mfv.rolling(window=window).sum() / df['volume'].rolling(window=window).sum()
        return cmf
    indicators['chaikin_mf'] = chaikin_money_flow(ohlcv_df, window=20)

    def volume_spike(df, threshold=1.5):
        avg_vol = df['volume'].rolling(window=20).mean()
        return (df['volume'] > avg_vol * threshold).astype(int)
    indicators['volume_spike'] = volume_spike(ohlcv_df)

    indicators['stoch_rsi'] = ta.momentum.stochrsi(ohlcv_df['close'], window=14, smooth1=3, smooth2=3)

    bb = ta.volatility.BollingerBands(ohlcv_df['close'], window=20, window_dev=2)
    indicators['bollinger_bands_upper'] = bb.bollinger_hband()
    indicators['bollinger_bands_lower'] = bb.bollinger_lband()
    indicators['bollinger_bands_mid'] = bb.bollinger_mavg()
    indicators['bollinger_bands'] = bb.bollinger_hband() - bb.bollinger_lband()

    for key, series in indicators.items():
        if isinstance(series, pd.Series):
            if series.dropna().shape[0] < 5:
                print(f"[ERROR] Indicator '{key}' có quá ít giá trị hợp lệ.")
                return None

    def calc_trend(df):
        if df is None or len(df) < 200: return "-"
        close = df['close'].iloc[-1]
        ema200 = ta.trend.ema_indicator(df['close'], window=200).iloc[-1]
        if pd.isna(close) or pd.isna(ema200): return "-"
        return "UP" if close > ema200 else "DOWN"

    if timeframe is not None:
        tf = str(timeframe).lower()
        if tf in ['4h','h4']:
            indicators['trend_h4'] = calc_trend(ohlcv_df)
        elif tf in ['1d','d1','daily']:
            indicators['trend_d1'] = calc_trend(ohlcv_df)

    indicators.setdefault('trend_h4', "-")
    indicators.setdefault('trend_d1', "-")
    return indicators
