import pandas as pd

def detect_sideway_regime(adx_h1_val, config):
    # Xác định sideway khi ADX thấp hơn ngưỡng
    adx_threshold = config.get("regime", {}).get("sideway", {}).get("adx_h1", 18)
    return adx_h1_val < adx_threshold

def signal_sideway_entry(m15, ind_m15, config):
    """
    Xác nhận tín hiệu vào lệnh sideway
    Return: ("LONG"/"SHORT"/None, info_dict)
    """
    bb_upper = ind_m15["bollinger_bands_upper"].iloc[-1]
    bb_lower = ind_m15["bollinger_bands_lower"].iloc[-1]
    close = m15["close"].iloc[-1]
    rsi = ind_m15["rsi"].iloc[-1]
    volume = m15["volume"].iloc[-1]
    avg_vol = m15["volume"].rolling(20).mean().iloc[-1]
    params = config.get("regime", {}).get("sideway", {})
    info = {
        "bb_upper": bb_upper, "bb_lower": bb_lower, "close": close, "rsi": rsi, "volume": volume, "avg_vol": avg_vol
    }
    # LONG tại BB dưới, RSI thấp, volume thấp
    if close <= bb_lower and rsi < params.get("sideway_rsi_long", 40) and volume < 1.2*avg_vol:
        return "LONG", info
    # SHORT tại BB trên, RSI cao, volume thấp
    if close >= bb_upper and rsi > params.get("sideway_rsi_short", 60) and volume < 1.2*avg_vol:
        return "SHORT", info
    return None, info

def calc_sideway_sl_tp(entry, direction, atr, config):
    params = config.get("regime", {}).get("sideway", {})
    sl_atr_mult = params.get("sl_atr_mult", 0.9)
    tp_atr_mult = params.get("tp_atr_mult", 0.8)
    if direction == "LONG":
        sl = entry - sl_atr_mult * atr
        tp = entry + tp_atr_mult * atr
    else:
        sl = entry + sl_atr_mult * atr
        tp = entry - tp_atr_mult * atr
    return round(sl, 5), round(tp, 5)

def get_sideway_size(balance, config):
    params = config.get("regime", {}).get("sideway", {})
    pct = params.get("max_pos_size_pct", 0.08)
    return max(balance * pct, 0)

def handle_sideway_entry(symbol, m15, ind_m15, adx_h1_val, simulator, notifier, config, now_epoch):
    # Check regime
    if not detect_sideway_regime(adx_h1_val, config):
        return False
    direction, info = signal_sideway_entry(m15, ind_m15, config)
    if direction is None:
        return False
    entry = m15["close"].iloc[-1]
    atr = ind_m15["atr"].iloc[-1]
    size = get_sideway_size(simulator.balance, config)
    if size < config.get("risk", {}).get("min_notional", 5.0):
        return False
    sl, tp = calc_sideway_sl_tp(entry, direction, atr, config)
    # Log và thông báo
    trade = simulator.open_trade(
        symbol, direction, entry=entry, sl=sl, tp=tp,
        size_quote=size, is_probe=True, now_ts=now_epoch,
        r_value=None, reason="sideway_entry",
        ma=None, atr=atr, breakout_volume=None, avg_volume=None
    )
    # Đánh dấu lệnh sideway để log và theo dõi
    if trade is not None:
        trade["sideway"] = True
    notifier.text(f"[SIDEWAY] {symbol} {direction}\nEntry: {entry:.4f} | SL: {sl:.4f} | TP: {tp:.4f}\n(ADX={adx_h1_val:.2f}, RSI={info['rsi']:.2f}, BB=[{info['bb_lower']:.4f}-{info['bb_upper']:.4f}])")
    return True