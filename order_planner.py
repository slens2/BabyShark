from typing import Dict
from position_sizer import compute_size

def _get_account_balance_quote(cfg: Dict) -> float:
    return float(((cfg.get("trading") or {}).get("paper_balance_quote") or 10000.0))

def plan_probe_and_topup(side: str, ohlcv_m15, indicators_m15: Dict, cfg: Dict) -> Dict:
    # LẤY ENTRY LÀ GIÁ CLOSE MỚI NHẤT, KHÔNG DÙNG VWAP
    entry = float(ohlcv_m15['close'].iloc[-1])
    atr_series = indicators_m15.get("atr")
    atr = float(atr_series.iloc[-1]) if atr_series is not None else entry * 0.01

    tight = cfg.get("tight_mode") or {}
    sl_mult = float(tight.get("sl_atr_mult", 1.2))
    rr = float(tight.get("rr_target", 2.0))

    if side == "LONG":
        sl = entry - sl_mult * atr
        r_value = entry - sl
        tp = entry + rr * r_value
    else:
        sl = entry + sl_mult * atr
        r_value = sl - entry
        tp = entry - rr * r_value

    bal_q = _get_account_balance_quote(cfg)
    risk = cfg.get("risk") or {}
    qty_full, notional = compute_size(
        entry=entry,
        sl=sl,
        balance_quote=bal_q,
        risk_pct=float(risk.get("per_trade_risk_pct", 0.01)),
        price_step=float(risk.get("price_step", 0.0)),
        qty_step=float(risk.get("qty_step", 0.0)),
        min_notional=float(risk.get("min_notional", 5.0))
    )
    return {
        "entry_price": round(entry, 6),
        "sl": round(sl, 6),
        "tp": round(tp, 6),
        "r_value": round(r_value, 6),
        "size_full": float(qty_full),
        "notional_full": round(notional, 4)
    }
