import os
from datetime import datetime, timedelta
import pandas as pd
import csv

def safe_float_fmt(val, digits=4, default=""):
    try:
        if val is None or (hasattr(pd, "isnull") and pd.isnull(val)):
            return default
        return f"{round(float(val), digits)}"
    except Exception:
        return default

def to_gmt7_str(ts):
    try:
        if ts is None or (hasattr(pd, "isnull") and pd.isnull(ts)):
            return ""
        dt = datetime.utcfromtimestamp(float(ts)) + timedelta(hours=7)
        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""

class TradeSimulator:
    def __init__(self, capital=100.0, leverage=10, fee_bps=4, log_path="trades_sim_log.csv"):
        self.initial_capital = capital
        self.balance = capital
        self.leverage = leverage
        self.fee_bps = fee_bps  # 4 bps = 0.04% (Binance taker fee)
        self.trades = []
        self.log_path = log_path
        self.csv_fieldnames = [
            "event_type","symbol","direction","stage","entry","close_price","time_open","time_close",
            "size","result","status","sl","tp","is_probe","fee","pnl_pct","r_value","reason",
            "time_open_human","time_close_human"
        ]
        self._init_csv()

    def _init_csv(self):
        if not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0:
            with open(self.log_path, "w", encoding="utf-8", newline='') as f:
                writer = csv.DictWriter(f, fieldnames=self.csv_fieldnames)
                writer.writeheader()

    def log_event(self, event_type, trade, extra=None):
        row = {k: trade.get(k, "") for k in self.csv_fieldnames}
        row['event_type'] = event_type
        if extra:
            row.update(extra)
        for tcol in ["time_open", "time_close"]:
            row[tcol + "_human"] = to_gmt7_str(trade.get(tcol))
        write_header = not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0
        with open(self.log_path, "a", encoding="utf-8", newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.csv_fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

    def _calc_fee(self, entry, exit_price, size):
        # Binance: fee = (entry + exit) * size * fee_rate
        return self.fee_bps / 10000.0 * (entry + exit_price) * size

    def open_trade(self, symbol, direction, entry, sl, tp, size_quote, is_probe=True, now_ts=None, r_value=None, reason=None, ma=None, atr=None, breakout_volume=None, avg_volume=None):
        """
        Ghi log mọi lệnh được gọi vào đây, không filter logic tại đây.
        Điều kiện mở lệnh (anti-chase, breakout_volume...) phải xử lý ngoài main.py!
        """
        now_ts = now_ts or datetime.utcnow().timestamp()
        notional = size_quote
        size = notional / max(1e-12, entry) if entry else 0
        trade = {
            "symbol": symbol,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "size": size,
            "stage": "probe" if is_probe else "full",
            "time_open": now_ts,
            "time_full": None if is_probe else now_ts,
            "time_close": None,
            "close_price": None,
            "result": None,
            "reason": reason,
            "fee": 0.0,
            "pnl_pct": None,
            "r_value": r_value,
            "is_probe": is_probe,
            "status": "open"
        }
        self.trades.append(trade)
        self.log_event("open", trade)
        return trade

    def promote_trade(self, trade, add_notional, price_now, now_ts=None):
        now_ts = now_ts or datetime.utcnow().timestamp()
        if trade["stage"] == "full":
            return
        old_notional = trade["entry"] * trade["size"]
        add_size = add_notional / max(1e-12, price_now)
        new_size = trade["size"] + add_size
        new_entry = (trade["entry"] * trade["size"] + price_now * add_size) / max(1e-12, new_size)
        trade["entry"] = new_entry
        trade["size"] = new_size
        trade["stage"] = "full"
        trade["time_full"] = now_ts
        trade["is_probe"] = False
        trade["status"] = "promoted"
        self.log_event("promote", trade)

    def adjust_trailing(self, trade, lock_ratio):
        if trade is None or trade.get("r_value") in (None, 0):
            return
        r = trade["r_value"]
        direction = trade["direction"]
        entry = trade["entry"]
        target = entry + (lock_ratio * r) * (1 if direction == "LONG" else -1)
        if direction == "LONG":
            if trade["sl"] is None or target > trade["sl"]:
                trade["sl"] = round(target, 6)
        else:
            if trade["sl"] is None or target < trade["sl"]:
                trade["sl"] = round(target, 6)

    def close_trade(self, trade, price, status, now_ts=None, reason=None):
        now_ts = now_ts or datetime.utcnow().timestamp()
        if trade is None or trade.get("time_close"):
            return
        trade["close_price"] = price
        trade["time_close"] = now_ts
        trade["status"] = status
        trade["reason"] = reason
        pnl_gross = (price - trade["entry"]) * trade["size"] if trade["direction"] == "LONG" else (trade["entry"] - price) * trade["size"]
        fee = self._calc_fee(trade["entry"], price, trade["size"])
        pnl_net = pnl_gross - fee
        trade["result"] = pnl_net
        trade["fee"] = fee
        base_cap = self.initial_capital
        trade["pnl_pct"] = (pnl_net / base_cap) * 100.0 if base_cap > 0 else 0
        self.balance += pnl_net
        self.log_event("close", trade)

    def cancel_probe(self, trade, now_ts=None, reason="cancel_probe"):
        now_ts = now_ts or datetime.utcnow().timestamp()
        if trade is None or trade.get("time_close"):
            return
        trade["time_close"] = now_ts
        trade["status"] = "cancel"
        trade["reason"] = reason
        self.log_event("cancel", trade)

    def get_active_trade(self, symbol):
        for t in self.trades:
            if t["symbol"] == symbol and t["time_close"] is None:
                return t
        return None

    def has_probe_only(self, symbol):
        t = self.get_active_trade(symbol)
        return bool(t and t["stage"] == "probe")

    def is_probe_expired(self, trade, timeout_min, now_ts=None):
        if trade is None or trade["stage"] != "probe":
            return False
        now_ts = now_ts or datetime.utcnow().timestamp()
        return (now_ts - trade["time_open"]) >= timeout_min * 60

    def daily_report(self, date_str=None, include_open=True):
        rows=[]
        for t in self.trades:
            # Lệnh đã đóng trong ngày
            if t["time_close"]:
                d = (datetime.utcfromtimestamp(float(t["time_close"])) + timedelta(hours=7)).date().isoformat()
                if date_str is None or d == date_str:
                    rows.append(t)
            # Lệnh đang mở
            elif include_open:
                d = (datetime.utcfromtimestamp(float(t["time_open"])) + timedelta(hours=7)).date().isoformat()
                if date_str is None or d == date_str:
                    rows.append(t)
        return pd.DataFrame(rows)

    def get_all_trades(self):
        return self.trades

    def save_report(self, path, date=None):
        trades = self.daily_report(date, include_open=True)
        fieldnames = self.csv_fieldnames
        if "time_open_human" not in trades.columns:
            trades["time_open_human"] = trades["time_open"].apply(to_gmt7_str)
        if "time_close_human" not in trades.columns:
            trades["time_close_human"] = trades["time_close"].apply(to_gmt7_str)
        trades = trades if not trades.empty else pd.DataFrame(columns=fieldnames)
        trades.to_csv(path, header=True, index=False, columns=fieldnames)

    def format_markdown_report(self, date=None):
        df = self.daily_report(date, include_open=True)
        if df.empty:
            return "Không có giao dịch nào trong ngày."
        stats = (
            f"**Lệnh:** {len(df)} | Win: {(df['result']>0).sum()} | "
            f"Loss: {(df['result']<0).sum()} | Tổng P&L: {safe_float_fmt(df['result'].sum(skipna=True),2)} | "
            f"Vốn: {safe_float_fmt(self.balance,2)}"
        )
        lines = [stats]
        for _, t in df.iterrows():
            entry = safe_float_fmt(getattr(t, "entry", None))
            exit_ = safe_float_fmt(getattr(t, "close_price", None))
            tp = safe_float_fmt(getattr(t, "tp", None))
            sl = safe_float_fmt(getattr(t, "sl", None))
            size = safe_float_fmt(getattr(t, "size", None))
            fee = safe_float_fmt(getattr(t, "fee", None), 2)
            pnl = safe_float_fmt(getattr(t, "result", None), 2)
            pnl_pct = safe_float_fmt(getattr(t, "pnl_pct", None), 2, "") + "%" if getattr(t, "pnl_pct", None) is not None and not pd.isnull(getattr(t, "pnl_pct", None)) else ""
            reason = getattr(t, "reason", "")
            open_time = getattr(t, "time_open_human", "") or to_gmt7_str(getattr(t, "time_open", None))
            close_time = getattr(t, "time_close_human", "") or to_gmt7_str(getattr(t, "time_close", None))
            pair = getattr(t, "symbol", "")
            direction = getattr(t, "direction", "")
            stage = getattr(t, "stage", "")
            lines.append(
                "──────────────\n"
                f"Cặp:     {pair}\n"
                f"Chiều:   {direction}\n"
                f"Stage:   {stage}\n"
                f"Entry:   {entry}\n"
                f"Exit:    {exit_}\n"
                f"TP:      {tp}\n"
                f"SL:      {sl}\n"
                f"Size:    {size}\n"
                f"Fee:     {fee}\n"
                f"P&L:     {pnl} ({pnl_pct})\n"
                f"Reason:  {reason}\n"
                f"Open:    {open_time}\n"
                f"Close:   {close_time}\n"
            )
        return "\n".join(lines)

    def summary_by_stage(self, date=None):
        df = self.daily_report(date, include_open=True)
        if df.empty: return "Không có giao dịch nào trong ngày."
        lines = ["--- Tổng hợp theo loại lệnh ---"]
        for stage in ["probe", "full"]:
            d = df[df["stage"] == stage]
            if d.empty: continue
            win = (d["result"] > 0).sum()
            loss = (d["result"] < 0).sum()
            total = len(d)
            pnl = d["result"].sum(skipna=True)
            rr = d["r_value"].mean(skipna=True)
            lines.append(
                f"{stage.upper()}: Tổng {total} | Win {win} | Loss {loss} | PnL: {safe_float_fmt(pnl,2)} | R tb: {safe_float_fmt(rr,2)}"
            )
        return "\n".join(lines)
