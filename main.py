# -*- coding: utf-8 -*-
import argparse
import asyncio
import json
import signal
import time
from datetime import datetime
from typing import Dict, Any, Tuple

import pandas as pd
import csv
import os
import unicodedata

from data import fetch_data
from indicators import calculate_indicators
from tight_gate import build_indicator_results, StablePassTracker, _heavy_hits
from votes import tally_votes
from notifier import Notifier
from order_planner import plan_probe_and_topup
from config import SIGNAL_MONITOR_CONFIG
from signal_manager import SignalMonitor
from trade_simulator import TradeSimulator

from sideway_strategy import handle_sideway_entry

CONFIG_PATH = "config.json"

SENT_SIDE: Dict[Tuple[str, str], Dict] = {}
PRE_STATE: Dict[str, Dict[str, Any]] = {}

monitor = SignalMonitor(SIGNAL_MONITOR_CONFIG)
simulator = TradeSimulator(capital=100.0, leverage=10, fee_bps=4)
stable_tracker = None

PROBE_INDICATOR_PCT = 90
MAX_HOLD_M15 = 12
WEAK_ADX = 16
WEAK_RSI_RANGE = (45, 55)
LEVERAGE = 10

CLOSE_WARNED = {}
SPAM_PROBE_WARNED = {}
LAST_CLOSE_TIME = {}
LAST_TRADE: Dict[str, Dict[str, Any]] = {}

def safe_float_fmt(val, digits=4, default=""):
    try:
        if val is None or (hasattr(pd, "isnull") and pd.isnull(val)):
            return default
        return f"{round(float(val), digits)}"
    except Exception:
        return default

def normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "").upper()

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def decide_side(score_long: float, score_short: float, eps: float = 0.1) -> str:
    if score_long - score_short > eps: return "LONG"
    if score_short - score_long > eps: return "SHORT"
    return "NEUTRAL"

def check_indicator_input(df, min_required, label):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) < min_required:
        print(f"[WARN] {label}: DataFrame quá nhỏ ({len(df) if df is not None else 0}) cần >= {min_required}")
        return False
    return True

def remove_accents(input_str):
    nfkd_form = unicodedata.normalize('NFKD', input_str)
    return ''.join([c for c in nfkd_form if not unicodedata.combining(c)])

def log_reason_vi_no_accent(log_dict):
    out = {}
    for k, v in log_dict.items():
        if isinstance(v, str):
            out[k] = remove_accents(v)
        else:
            out[k] = v
    fn = "entries_reasons.csv"
    fieldnames = list(out.keys())
    write_header = not os.path.exists(fn)
    with open(fn, 'a', encoding="utf-8", newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(out)

def should_send_new_entry(symbol, timeframe, direction, entry, sl, tp, min_interval_min=15, min_entry_diff_pct=0.5):
    key = (normalize_symbol(symbol), timeframe)
    now = time.time()
    last = SENT_SIDE.get(key)
    warning_text = None
    if last:
        last_side = last.get("side")
        if last_side != direction:
            warning_text = f"ĐẢO CHIỀU {symbol} {timeframe} {last_side} -> {direction}"
        elif last_side == direction:
            last_ts = last.get("ts", 0)
            if now - last_ts < min_interval_min * 60:
                print(f"[SENT_SIDE] Bỏ qua: timeout {min_interval_min} phút")
                return False, None
            last_entry = last.get("entry", entry)
            last_sl = last.get("sl", sl)
            last_tp = last.get("tp", tp)
            entry_diff_pct = abs((entry or 0) - (last_entry or 0)) / max(abs(entry or 0), 1) * 100
            sl_diff_pct = abs((sl or 0) - (last_sl or 0)) / max(abs(sl or 0), 1) * 100
            tp_diff_pct = abs((tp or 0) - (last_tp or 0)) / max(abs(tp or 0), 1) * 100
            if entry_diff_pct < min_entry_diff_pct and sl_diff_pct < min_entry_diff_pct and tp_diff_pct < min_entry_diff_pct:
                print(f"[SENT_SIDE] Bỏ qua: entry/sl/tp lệch quá nhỏ (< {min_entry_diff_pct}%)")
                return False, None
    SENT_SIDE[key] = {"side": direction, "ts": now, "entry": entry, "sl": sl, "tp": tp}
    print(f"[SENT_SIDE] Tín hiệu mới: {symbol} {direction} entry={entry}, sl={sl}, tp={tp}")
    return True, warning_text

def mark_closed_entry(symbol, timeframe, direction):
    key = (normalize_symbol(symbol), timeframe)
    SENT_SIDE.pop(key, None)

def is_data_fresh(df, tf_min, symbol, tf_name, max_lag_n=2):
    if df is None or df.empty:
        print(f"[ERROR] DataFrame {symbol} {tf_name} rỗng")
        return False
    last_ts = None
    if 'timestamp' in df.columns:
        last_ts = df['timestamp'].iloc[-1]
    elif hasattr(df.index, "astype"):
        try:
            last_ts = int(df.index[-1].timestamp())
        except Exception:
            pass
    if last_ts is None:
        print(f"[ERROR] Không lấy được timestamp {symbol} {tf_name}")
        return False
    now = time.time()
    lag = now - last_ts
    max_lag = tf_min * 60 * max_lag_n
    if lag > max_lag:
        print(f"[ERROR] Dữ liệu {symbol} {tf_name} quá cũ: lệch {lag/60:.1f} phút (> {max_lag/60:.1f})")
        return False
    try:
        last_price = float(df['close'].iloc[-1])
        print(f"[DATA] {symbol} {tf_name} | price: {last_price} | last ts: {datetime.fromtimestamp(last_ts)} | lag: {lag:.1f}s")
    except Exception:
        pass
    return True

def snapshot_m5_confirmed(side_m15, m5, ind_m5, count=2, w_m15_u=None):
    if count <= 0:
        return True
    if m5 is None or len(m5) < count:
        return False
    votes = []
    for i in range(-count, 0):
        # Tính indicator và votes cho từng nến M5 gần nhất
        df_one = m5.iloc[:i+1] if i != -1 else m5
        ind_one = calculate_indicators(df_one, timeframe="5m")
        map_one_u = build_indicator_results(df_one, ind_one)
        tally = tally_votes(map_one_u, w_m15_u)
        sl = tally.get('score_long', 0)
        ss = tally.get('score_short', 0)
        side = decide_side(sl, ss)
        votes.append(side)
    # Normalize kiểu dữ liệu để so sánh chính xác
    def normalize_side(s):
        return str(s).strip().upper()
    side_m15_norm = normalize_side(side_m15)
    votes_norm = [normalize_side(v) for v in votes]
    print(f"[DEBUG][M5] side_m15={side_m15_norm} votes={votes_norm} {[v == side_m15_norm for v in votes_norm]}")
    # Sửa điều kiện: chỉ cần >=1 nến M5 đồng pha trend M15
    return sum([v == side_m15_norm or v == "NEUTRAL" for v in votes_norm]) >= 1

def atr_reverse_filter(price_now, last_entry_price, atr_val, min_atr_ratio=0.7):
    if last_entry_price is None:
        return True
    try:
        if abs(price_now - last_entry_price) < (atr_val or 0) * min_atr_ratio:
            print(f"[ATR FILTER] Bỏ qua do biên độ giá < {min_atr_ratio} ATR (|{price_now} - {last_entry_price}| < {(atr_val or 0) * min_atr_ratio})")
            return False
    except Exception:
        pass
    return True

def should_suggest_close(active_trade, newest_side, ind_m15, adx_val, rsi_val, now_epoch):
    reason = None
    symbol = active_trade.get("symbol", "")
    direction = active_trade.get("direction", "")
    entry = active_trade.get("entry", 0)
    sl = active_trade.get("sl", 0)
    tp = active_trade.get("tp", 0)
    open_ts = active_trade.get("time_open", active_trade.get("open_ts",0))
    pnl = active_trade.get("result", 0)
    hold_m15 = int((now_epoch - open_ts) // (15*60)) if open_ts else 0
    if newest_side != direction and newest_side in ("LONG", "SHORT"):
        reason = f"ĐẢO CHIỀU sang {newest_side}"
    elif adx_val is not None and adx_val < WEAK_ADX:
        reason = "Momentum yếu (ADX↓)"
    elif rsi_val is not None and WEAK_RSI_RANGE[0] <= rsi_val <= WEAK_RSI_RANGE[1]:
        reason = "RSI trung tính"
    elif hold_m15 is not None and hold_m15 > MAX_HOLD_M15:
        reason = f"Giữ lệnh quá lâu ({hold_m15} nến M15)"
    if reason:
        content = (
            f"ĐỀ XUẤT ĐÓNG [{symbol}]\n"
            f"Lý do: {reason}\n"
            f"Chiều: {direction} | Entry: {safe_float_fmt(entry)} | SL: {safe_float_fmt(sl)} | TP: {safe_float_fmt(tp)}\n"
            f"Giữ: {hold_m15} nến M15 | PnL: {safe_float_fmt(pnl,2)}"
        )
        return True, reason, content
    return False, None, None

def is_breakout_candle(df, ind, direction="LONG", body_atr_mult=1.3, vol_ma20_mult=1.8, ema_buffer_atr=0.2):
    if df is None or ind is None or len(df) < 20:
        return False
    atr = ind.get('atr', pd.Series([0]*len(df)))
    atr_val = float(atr.iloc[-1]) if hasattr(atr, "iloc") else 0.0
    vol = float(df['volume'].iloc[-1])
    avg_vol = float(df['volume'].rolling(20).mean().iloc[-1])
    close = float(df['close'].iloc[-1])
    open_ = float(df['open'].iloc[-1])
    ema200 = ind.get('ema200', pd.Series([0]*len(df)))
    ema200_val = float(ema200.iloc[-1]) if hasattr(ema200, "iloc") else 0.0
    if atr_val <= 0 or avg_vol <= 0 or ema200_val <= 0:
        return False
    if direction == "LONG":
        body_ok = (close - open_) >= body_atr_mult * atr_val
        vol_ok = vol >= vol_ma20_mult * avg_vol
        ema_ok = close >= (ema200_val + ema_buffer_atr * atr_val)
        return body_ok and vol_ok and ema_ok
    else:
        body_ok = (open_ - close) >= body_atr_mult * atr_val
        vol_ok = vol >= vol_ma20_mult * avg_vol
        ema_ok = close <= (ema200_val - ema_buffer_atr * atr_val)
        return body_ok and vol_ok and ema_ok

def _write_trailing_log(trade, price_now, old_sl, new_sl, roi_now):
    fn = "trailing_log.csv"
    row = {
        "timestamp": int(time.time()),
        "symbol": trade.get("symbol"),
        "direction": trade.get("direction"),
        "entry": trade.get("entry"),
        "price_now": price_now,
        "old_sl": old_sl,
        "new_sl": new_sl,
        "roi_now": roi_now
    }
    write_header = not os.path.exists(fn)
    with open(fn, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)

def update_trailing_stop(trade, price_now):
    if trade is None:
        return
    entry = trade.get("entry")
    direction = trade.get("direction")
    if entry is None or direction not in ("LONG", "SHORT") or price_now is None:
        return
    leverage = getattr(simulator, "leverage", 1) or 1
    raw_roi = ((price_now - entry) / entry) if direction == "LONG" else ((entry - price_now) / entry)
    roi_now = raw_roi * leverage
    if roi_now >= 0.03:
        lock = 0.002
        old_sl = trade.get("sl")
        if direction == "LONG":
            candidate = price_now - lock * entry
            be_guard = entry
            new_sl = max(old_sl if old_sl is not None else -1e20, candidate, be_guard)
        else:
            candidate = price_now + lock * entry
            be_guard = entry
            new_sl = min(old_sl if old_sl is not None else 1e20, candidate, be_guard)
        if old_sl is None or abs(new_sl - (old_sl or 0)) > 1e-9:
            trade["sl"] = round(new_sl, 6)
            _write_trailing_log(trade, price_now, old_sl, trade["sl"], roi_now)

def _upper_keys(d: Dict[str, Any]) -> Dict[str, Any]:
    return {(k.upper() if isinstance(k, str) else k): v for k, v in (d or {}).items()}

def _sum_positive_weights(w: Dict[str, float]) -> float:
    s = 0.0
    for v in (w or {}).values():
        try:
            fv = float(v)
            if fv > 0:
                s += fv
        except Exception:
            continue
    return s

def _classify_regime(adx_h1: float, heavy_hits: int, cfg: Dict[str, Any]) -> str:
    r = (cfg.get("regime", {}) or {})
    rn = r.get("normal", {}) or {}
    rs = r.get("strong", {}) or {}
    if adx_h1 >= float(rs.get("adx_h1", 32)) and heavy_hits >= int(rs.get("heavy_hits", 4)):
        return "STRONG"
    if adx_h1 >= float(rn.get("adx_h1", 28)) and heavy_hits >= int(rn.get("heavy_hits", 3)):
        return "NORMAL"
    return "WEAK"

def _calc_initial_sl(entry_price: float, direction: str, atr_val: float, mult: float):
    if entry_price is None or atr_val is None or mult is None or mult <= 0:
        return None
    if direction == "LONG":
        return max(0.0, entry_price - mult * atr_val)
    else:
        return entry_price + mult * atr_val

async def run_once(cfg: Dict[str, Any], notifier: Notifier, tracker: StablePassTracker):
    now_epoch = time.time()

    th_m15_cfg = float((cfg.get("thresholds") or {}).get("M15", 11.0))
    th_h1_cfg  = float((cfg.get("thresholds") or {}).get("H1", 8.0))
    heavy_required = int((cfg.get("tight_mode", {}) or {}).get("heavy_required", 3))
    m5_snapshot_bars_default = int(cfg.get("m5_snapshot_bars", 3))
    symbols = cfg.get("symbols", ["BTC/USDT"])

    w_m15 = (cfg.get("weights_sets") or {}).get("M15", {})
    w_h1 = (cfg.get("weights_sets") or {}).get("H1", {})
    w_m15_u = _upper_keys(w_m15)
    w_h1_u = _upper_keys(w_h1)
    m15_max_w = _sum_positive_weights(w_m15_u)
    h1_max_w  = _sum_positive_weights(w_h1_u)

    PROBE_PCT = float((cfg.get("trading", {}) or {}).get("probe_pct", 0.1))
    FULL_PCT = float((cfg.get("trading", {}) or {}).get("full_pct", 0.5))
    min_notional = float((cfg.get("risk", {}) or {}).get("min_notional", 5.0))
    PROMOTE_PULLBACK_ATR = float(cfg.get("promote_pullback_atr", 0.5))
    AUTO_CLOSE_ON_WARNING_IF_PNL_POS = bool(cfg.get("auto_close_on_warning_if_pnl_positive", True))
    adx_h1_threshold = int(cfg.get("adx_h1_threshold", 28))
    PROBE_TIMEOUT_MIN = int((cfg.get("signal_flow", {}) or {}).get("probe_timeout_min", 30))
    sl_mult_probe = float((cfg.get("trading", {}) or {}).get("sl_atr_mult_probe", (cfg.get("tight_mode", {}) or {}).get("sl_atr_mult", 1.0)))
    sl_mult_full  = float((cfg.get("trading", {}) or {}).get("sl_atr_mult_full",  (cfg.get("tight_mode", {}) or {}).get("sl_atr_mult", 1.2)))
    bypass_breakout_anti_ch_normal = bool((cfg.get("engine", {}) or {}).get("bypass_anti_chase_on_breakout_normal", False))

    for symbol in symbols:
        cooldown_period = 15 * 60
        if symbol in LAST_CLOSE_TIME and time.time() - LAST_CLOSE_TIME[symbol] < cooldown_period:
            print(f"[COOLDOWN] {symbol}: chờ 1 nến M15 sau khi vừa đóng lệnh")
            continue

        m5 = fetch_data(symbol, "5m", limit=400)
        m15 = fetch_data(symbol, "15m", limit=300)
        h1 = fetch_data(symbol, "1h", limit=300)
        d1 = fetch_data(symbol, "1d", limit=300)

        if not check_indicator_input(m5, 215, f"{symbol} M5"): continue
        if not check_indicator_input(m15, 200, f"{symbol} M15"): continue
        if not check_indicator_input(h1, 200, f"{symbol} H1"): continue
        if not check_indicator_input(d1, 200, f"{symbol} D1"): continue

        if not is_data_fresh(m5, tf_min=5, symbol=symbol, tf_name="M5"): continue
        if not is_data_fresh(m15, tf_min=15, symbol=symbol, tf_name="M15"): continue
        if not is_data_fresh(h1, tf_min=60, symbol=symbol, tf_name="H1"): continue
        if not is_data_fresh(d1, tf_min=1440, symbol=symbol, tf_name="D1"): continue

        ind_m5 = calculate_indicators(m5, cfg, timeframe="5m")
        ind_m15 = calculate_indicators(m15, cfg, timeframe="15m")
        ind_h1 = calculate_indicators(h1, cfg, timeframe="1h")
        ind_d1 = calculate_indicators(d1, cfg, timeframe="1d")
        if ind_m5 is None or ind_m15 is None or ind_h1 is None or ind_d1 is None: continue

        map_m5_u  = _upper_keys(build_indicator_results(m5,  ind_m5))
        map_m15_u = _upper_keys(build_indicator_results(m15, ind_m15))
        map_h1_u  = _upper_keys(build_indicator_results(h1,  ind_h1))
        map_h1_u_filtered = {k: v for k, v in map_h1_u.items() if k in w_h1_u}

        vr_m5  = tally_votes(map_m5_u,  w_m15_u)
        vr_m15 = tally_votes(map_m15_u, w_m15_u)
        vr_h1  = tally_votes(map_h1_u_filtered,  w_h1_u)

        sl5, ss5   = float(vr_m5.get("score_long", 0) or 0),  float(vr_m5.get("score_short", 0) or 0)
        sl15, ss15 = float(vr_m15.get("score_long", 0) or 0), float(vr_m15.get("score_short", 0) or 0)
        slh1, ssh1 = float(vr_h1.get("score_long", 0) or 0),  float(vr_h1.get("score_short", 0) or 0)

        side_m5  = decide_side(sl5, ss5)
        side_m15 = decide_side(sl15, ss15)
        side_h1  = decide_side(slh1, ssh1)

        m15_score = sl15 if side_m15 == "LONG" else (ss15 if side_m15 == "SHORT" else 0.0)
        h1_score_raw  = slh1 if side_h1  == "LONG" else (ssh1 if side_h1  == "SHORT" else 0.0)
        h1_score = min(h1_score_raw, h1_max_w)

        hhits = _heavy_hits(map_h1_u, ind_h1['ema200'], side_m15) if side_m15 != "NEUTRAL" else 0
        try:
            adx_h1 = float(ind_h1['adx'].iloc[-1])
        except Exception:
            adx_h1 = 0.0

        price_now = float(m15['close'].iloc[-1]) if m15 is not None and not pd.isnull(m15['close'].iloc[-1]) else 0.0
        ma50_val = float(ind_m15.get("ma50").iloc[-1]) if ind_m15.get("ma50") is not None else price_now
        atr_val = float(ind_m15.get("atr").iloc[-1]) if ind_m15.get("atr") is not None else price_now*0.01

        regime = _classify_regime(adx_h1, hhits, cfg) if side_m15 == side_h1 != "NEUTRAL" else "WEAK"
        r = (cfg.get("regime", {}) or {})
        r_norm = r.get("normal", {}) or {}
        r_str  = r.get("strong", {}) or {}
        if regime == "STRONG":
            m5_snapshot_bars = int(r_str.get("m5_snapshot_bars", 1))
            body_mult = float(r_str.get("body_atr_mult", 1.2))
            vol_mult = float(r_str.get("vol_ma20_mult", 1.6))
            ema_buf = float(r_str.get("ema_buffer_atr", 0.15))
            anti_chase_mult = float(r_str.get("anti_chase_atr_mult", 1.8))
            m15_threshold_pct = float(r_str.get("m15_threshold_pct", 0.75))
            direct_full_flag_regime = bool(r_str.get("direct_full", True))
        else:
            m5_snapshot_bars = int(r_norm.get("m5_snapshot_bars", 2))
            body_mult = float(r_norm.get("body_atr_mult", 1.3))
            vol_mult = float(r_norm.get("vol_ma20_mult", 1.8))
            ema_buf = float(r_norm.get("ema_buffer_atr", 0.2))
            anti_chase_mult = float(r_norm.get("anti_chase_atr_mult", 1.2))
            m15_threshold_pct = float(r_norm.get("m15_threshold_pct", 0.85))
            direct_full_flag_regime = bool(r_norm.get("direct_full", False))

        th_m15_eff = min(th_m15_cfg, round(m15_threshold_pct * m15_max_w, 6))
        th_h1_eff  = th_h1_cfg

        try:
            anti_chase = abs(price_now - ma50_val) > anti_chase_mult * atr_val
        except Exception:
            anti_chase = False

        m5_ok = snapshot_m5_confirmed(side_m15, m5, ind_m5, count=m5_snapshot_bars)
        m15_h1_ok = (side_m15 == side_h1 != "NEUTRAL")
        gates_ok = (
            m5_ok
            and m15_h1_ok
            and m15_score >= th_m15_eff
            and h1_score  >= th_h1_eff
            and hhits >= heavy_required
            and adx_h1 >= adx_h1_threshold
            and regime in ("NORMAL", "STRONG")
        )
        is_stable = tracker.update(symbol, "15m", side_m15, gates_ok, now_ts=now_epoch)
        full_ready = gates_ok and is_stable

        m15_breakdown = (vr_m15.get("breakdown_long") if side_m15=="LONG" else vr_m15.get("breakdown_short")) or {}
        h1_breakdown  = (vr_h1.get("breakdown_long")  if side_h1 =="LONG" else vr_h1.get("breakdown_short"))  or {}

        log_data = {
            "timestamp": int(now_epoch),
            "symbol": symbol,
            "phase": "scan",
            "side": side_m15,
            "m15_score": safe_float_fmt(m15_score,2),
            "h1_score": safe_float_fmt(h1_score,2),
            "m15_max_w": safe_float_fmt(m15_max_w,2),
            "h1_max_w": safe_float_fmt(h1_max_w,2),
            "th_m15_cfg": safe_float_fmt(th_m15_cfg,2),
            "th_h1_cfg": safe_float_fmt(th_h1_cfg,2),
            "th_m15_eff": safe_float_fmt(th_m15_eff,2),
            "th_h1_eff": safe_float_fmt(th_h1_eff,2),
            "heavy_hits": hhits,
            "adx_h1": safe_float_fmt(adx_h1,2),
            "regime": regime,
            "m5_snapshot_bars": m5_snapshot_bars,
            "anti_ch": anti_chase,
            "decision": f"full_ready={full_ready}; stable={is_stable};"
        }
        # Log từng gate riêng biệt
        log_data["gate_m5_ok"] = bool(m5_ok)
        log_data["gate_m15_h1_ok"] = bool(m15_h1_ok)
        log_data["gate_m15_score_ok"] = bool(m15_score >= th_m15_eff)
        log_data["gate_h1_score_ok"] = bool(h1_score >= th_h1_eff)
        log_data["gate_heavy_ok"] = bool(hhits >= heavy_required)
        log_data["gate_adx_ok"] = bool(adx_h1 >= adx_h1_threshold)
        log_data["gate_regime_ok"] = regime in ("NORMAL", "STRONG")
        log_data["gate_stable_ok"] = bool(is_stable)
        log_data["gates_ok"] = bool(gates_ok)

        for k,v in m15_breakdown.items():
            log_data[f"m15_{(k or '').upper()}"] = v
        for k,v in h1_breakdown.items():
            log_data[f"h1_{(k or '').upper()}"] = v
        log_reason_vi_no_accent(log_data)

        # ... (phần còn lại giữ nguyên như cũ)
        # Không thay đổi logic vào lệnh, báo cáo, trailing, v.v.
        # Đảm bảo chỉ bổ sung log gate

# ... giữ nguyên phần async main(), các hàm tiện ích và luồng của file gốc ...

        roi_target = 1.0
        sim_tp_long = price_now * (1 + roi_target / LEVERAGE)
        sim_tp_short = price_now * (1 - roi_target / LEVERAGE)

        active_probe = None
        active_full = None
        for t in simulator.get_all_trades():
            if t["symbol"] == symbol and t["time_close"] is None:
                if t["stage"] == "probe":
                    active_probe = t
                elif t["stage"] == "full":
                    active_full = t

        if active_probe:
            age_min = (now_epoch - active_probe.get("time_open", now_epoch)) / 60.0
            if age_min >= PROBE_TIMEOUT_MIN:
                simulator.close_trade(active_probe, price_now, "TIMEOUT", now_epoch, reason="probe_timeout")
                monitor.remove_signal(symbol)
                LAST_CLOSE_TIME[symbol] = time.time()
                notifier.text(f"Đóng probe {symbol} do timeout {int(age_min)} phút")
                active_probe = None

        if sl15 - ss15 > 0.1:
            trade_direction = "LONG"
        elif ss15 - sl15 > 0.1:
            trade_direction = "SHORT"
        else:
            trade_direction = "LONG"

        sideway_handled = handle_sideway_entry(symbol, m15, ind_m15, adx_h1, simulator, notifier, cfg, now_epoch)
        if sideway_handled:
            continue

        if m15_h1_ok and regime in ("NORMAL","STRONG"):
            breakout_ok = is_breakout_candle(m15, ind_m15, direction=trade_direction, body_atr_mult=body_mult, vol_ma20_mult=vol_mult, ema_buffer_atr=ema_buf)
            allow_entry_despite_anti_chase = (regime == "STRONG") or (regime == "NORMAL" and bypass_breakout_anti_ch_normal)
            if breakout_ok and m5_ok and (not anti_chase or allow_entry_despite_anti_chase):
                last_entry_price_for_reverse = None
                last_dir = None
                if symbol in LAST_TRADE:
                    last_dir = LAST_TRADE[symbol].get("direction")
                    if last_dir and last_dir != trade_direction:
                        last_entry_price_for_reverse = LAST_TRADE[symbol].get("entry")
                if last_entry_price_for_reverse is not None and not atr_reverse_filter(price_now, last_entry_price_for_reverse, atr_val):
                    pass
                else:
                    allow_direct_full_cfg = bool((cfg.get("engine", {}) or {}).get("direct_full_on_strong_breakout", False))
                    do_direct_full = allow_direct_full_cfg and (regime == "STRONG" and (direct_full_flag_regime is True))
                    if do_direct_full and not active_full and not active_probe:
                        size = max(simulator.balance * FULL_PCT, 0)
                        if size >= min_notional:
                            sim_tp = sim_tp_long if trade_direction == "LONG" else sim_tp_short
                            init_sl = _calc_initial_sl(price_now, trade_direction, atr_val, sl_mult_full)
                            simulator.open_trade(
                                symbol, trade_direction, entry=price_now, sl=init_sl, tp=sim_tp,
                                size_quote=size, is_probe=False, now_ts=now_epoch,
                                r_value=None, reason=f"full_direct_breakout_{trade_direction.lower()}",
                                ma=float(ind_m15.get("ema200", pd.Series([price_now])).iloc[-1]),
                                atr=atr_val
                            )
                            notifier.text(f"FULL trực tiếp {symbol} ({trade_direction}) – STRONG | Entry {safe_float_fmt(price_now)} | SL {safe_float_fmt(init_sl)}")
                    else:
                        if not active_probe and not active_full:
                            probe_size = max(simulator.balance * PROBE_PCT, 0)
                            if probe_size >= min_notional:
                                sim_tp = sim_tp_long if trade_direction == "LONG" else sim_tp_short
                                plan = plan_probe_and_topup(trade_direction, m15, ind_m15, cfg)
                                init_sl = _calc_initial_sl(price_now, trade_direction, atr_val, sl_mult_probe)
                                simulator.open_trade(
                                    symbol, trade_direction, entry=price_now,
                                    sl=init_sl, tp=sim_tp,
                                    size_quote=probe_size, is_probe=True,
                                    now_ts=now_epoch, r_value=plan.get("r_value") if plan else None,
                                    reason=f"probe_breakout_{trade_direction.lower()}",
                                )
                                notifier.text(f"Probe {symbol} ({trade_direction}) – {regime} | Entry {safe_float_fmt(price_now)} | SL {safe_float_fmt(init_sl)}")

        active_probe = None
        active_full = None
        for t in simulator.get_all_trades():
            if t["symbol"] == symbol and t["time_close"] is None:
                if t["stage"] == "probe":
                    active_probe = t
                elif t["stage"] == "full":
                    active_full = t

        if active_probe and active_probe.get("stage") == "probe":
            last_close = float(m15["close"].iloc[-1])
            probe_entry = float(active_probe["entry"])
            atr_current = atr_val
            pullback_ok = abs(last_close - probe_entry) <= PROMOTE_PULLBACK_ATR * atr_current if last_close is not None and probe_entry is not None and atr_current is not None else False
            last_candle_dir = "LONG" if last_close > m15["open"].iloc[-1] else "SHORT"
            avg_volume = float(m15['volume'].rolling(20).mean().iloc[-1])
            last_candle_vol = float(m15['volume'].iloc[-1])
            big_trap = (last_candle_dir != active_probe["direction"]) and (last_candle_vol > 1.8 * avg_volume if avg_volume else False)
            anti_chase_now = abs(last_close - ma50_val) > anti_chase_mult * atr_current if atr_current else False
            if pullback_ok and not big_trap and not anti_chase_now:
                if not active_full:
                    promote_size = max(simulator.balance * FULL_PCT, 0)
                    if promote_size >= min_notional:
                        simulator.promote_trade(active_probe, promote_size, price_now)
                        new_sl_full = _calc_initial_sl(price_now, active_probe["direction"], atr_current, sl_mult_full)
                        old_sl = active_probe.get("sl")
                        if old_sl is None:
                            active_probe["sl"] = new_sl_full
                        else:
                            if active_probe["direction"] == "LONG":
                                active_probe["sl"] = max(old_sl, new_sl_full) if new_sl_full is not None else old_sl
                            else:
                                active_probe["sl"] = min(old_sl, new_sl_full) if new_sl_full is not None else old_sl
                        notifier.text(f"Promote FULL {symbol} sau pullback. Giá {safe_float_fmt(price_now)} | SL {safe_float_fmt(active_probe.get('sl'))}")
            elif big_trap:
                simulator.close_trade(active_probe, price_now, "TRAP", now_epoch, reason="trap_reversal")
                notifier.text(f"Đóng probe {symbol} do trap đảo chiều vol lớn")

        for t in simulator.get_all_trades():
            if t["symbol"] == symbol and t["time_close"] is None:
                dside = t.get("direction")
                tp_sim = t.get("tp")
                sl_sim = t.get("sl")
                active_id = f"{symbol}|{t.get('entry')}"
                closed = False

                update_trailing_stop(t, price_now)

                if price_now is not None and tp_sim is not None and (
                    (dside == "LONG" and price_now >= tp_sim) or
                    (dside == "SHORT" and price_now <= tp_sim)
                ):
                    simulator.close_trade(t, price_now, "TP", now_epoch, reason="take_profit")
                    closed = True

                if not closed and price_now is not None and sl_sim is not None and (
                    (dside == "LONG" and price_now <= sl_sim) or
                    (dside == "SHORT" and price_now >= sl_sim)
                ):
                    simulator.close_trade(t, price_now, "SL", now_epoch, reason="stop_loss")
                    closed = True

                if not closed and side_m15 and dside and side_m15 != dside and side_m15 in ("LONG", "SHORT"):
                    simulator.close_trade(t, price_now, "REVERSE", now_epoch, reason="reverse_signal")
                    closed = True

                try:
                    adx_latest = float(ind_m15['adx'].iloc[-1])
                except Exception:
                    adx_latest = None
                try:
                    rsi_latest = float(ind_m15['rsi'].iloc[-1])
                except Exception:
                    rsi_latest = None
                suggest, reason, content = should_suggest_close(t, side_m15, ind_m15, adx_latest, rsi_latest, now_epoch)
                pnl = t.get("result", 0)
                if not closed and (suggest and pnl is not None and pnl > 0 and AUTO_CLOSE_ON_WARNING_IF_PNL_POS):
                    simulator.close_trade(t, price_now, "CLOSE_WARN_PNL_POS", now_epoch, reason="close_on_warning_pnl_positive")
                    notifier.text(f"Đóng {symbol} do cảnh báo & PnL dương {safe_float_fmt(pnl,2)}")
                    closed = True
                elif not closed and suggest:
                    last_warned = CLOSE_WARNED.get(active_id)
                    if last_warned != reason:
                        notifier.text(content)
                        CLOSE_WARNED[active_id] = reason
                else:
                    if CLOSE_WARNED.get(active_id):
                        CLOSE_WARNED.pop(active_id, None)

                if closed:
                    try:
                        LAST_TRADE[symbol] = {"direction": dside, "entry": float(t.get("entry") or 0), "close_ts": now_epoch}
                    except Exception:
                        pass
                    mark_closed_entry(symbol, "15m", dside)
                    monitor.remove_signal(symbol)
                    CLOSE_WARNED.pop(active_id, None)
                    LAST_CLOSE_TIME[symbol] = time.time()

    now_dt = datetime.now()
    if now_dt.hour == 23 and now_dt.minute >= 59:
        csv_path = "trades_sim_log.csv"
        simulator.save_report(csv_path, date=now_dt.strftime("%Y-%m-%d"))
        md_report = simulator.format_markdown_report(date=now_dt.strftime("%Y-%m-%d"))
        summary = simulator.summary_by_stage(date=now_dt.strftime("%Y-%m-%d"))
        notifier.text("Báo cáo cuối ngày:\n" + md_report + "\n" + summary)
        try:
            notifier.send_file(csv_path, "Báo cáo giao dịch (CSV)")
        except Exception:
            pass

    for symbol in cfg.get("symbols", []):
        active_trade = simulator.get_active_trade(symbol)
        if active_trade:
            hold_m15 = int((time.time() - active_trade.get("time_open", 0)) // (15*60)) if active_trade.get("time_open") else 0
            if hold_m15 is not None and hold_m15 > MAX_HOLD_M15 and not active_trade.get("hold_warned", False):
                notifier.text(f"Lệnh {symbol} đã treo {hold_m15} nến M15")
                active_trade["hold_warned"] = True

async def main():
    global stable_tracker
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    args = parser.parse_args()

    cfg = load_config()
    prof = args.profile or cfg.get("active_profile")
    if prof:
        p = (cfg.get("profiles") or {}).get(prof) or {}
        for k in ("thresholds","tight_mode","trading","risk","scheduler","engine","signal_flow","regime","discord"):
            if k in p: cfg.setdefault(k, {}).update(p[k])
        if "adx_h1_threshold" in p:
            cfg["adx_h1_threshold"] = p["adx_h1_threshold"]

    tight = cfg.get("tight_mode", {}) or {}
    snapshot_min_gap_sec = int(tight.get("snapshot_min_gap_sec", 60))
    snapshot_confirmations = int(tight.get("snapshot_confirmations", 3))
    state_path = tight.get("state_path", "tight_state.json")
    stable_tracker = StablePassTracker(path=state_path, min_gap_sec=snapshot_min_gap_sec, required_passes=snapshot_confirmations)

    notifier = Notifier(cfg)
    if notifier.enabled():
        notifier.text(f"Bot started profile={prof} | symbols={len(cfg.get('symbols', []))}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
    except NotImplementedError:
        pass

    interval_sec = int((cfg.get("scheduler") or {}).get("interval_sec", 45))

    while not stop_event.is_set():
        start = time.time()
        print(f"[LOOP] {datetime.now().isoformat(timespec='seconds')}")
        try:
            await asyncio.wait_for(run_once(cfg, notifier, stable_tracker), timeout=max(5, interval_sec - 5))
        except asyncio.TimeoutError:
            print("[WARN] iteration timeout")
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[ERROR] run_once: {e!r}")
        elapsed = time.time() - start
        remain = max(0, interval_sec - elapsed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=remain)
        except asyncio.TimeoutError:
            pass

    print("[MAIN] Stopped")

if __name__ == "__main__":
    asyncio.run(main())