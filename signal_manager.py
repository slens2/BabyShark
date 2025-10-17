from datetime import datetime

def calc_sl_tp(entry, direction, atr, config):
    sl_atr = config['sl_atr_mult'] * atr
    sl_percent = max(sl_atr / entry * 100, config['min_sl_percent'])
    if direction == 'LONG':
        sl = entry * (1 - sl_percent/100)
        tp = entry + (entry - sl) * config['rr_target']
        tp_min = entry * (1 + config['min_tp_percent']/100)
        tp = max(tp, tp_min)
    else:
        sl = entry * (1 + sl_percent/100)
        tp = entry - (sl - entry) * config['rr_target']
        tp_min = entry * (1 - config['min_tp_percent']/100)
        tp = min(tp, tp_min)
    return round(sl, 4), round(tp, 4)

def apply_trailing_stop(current_price, direction, entry, sl, config):
    if not config.get("enable_trailing_stop", False):
        return sl
    trailing = config.get("trailing_stop_percent", 0.3)/100 * entry
    if direction == 'LONG':
        new_sl = max(sl, current_price - trailing)
    else:
        new_sl = min(sl, current_price + trailing)
    return round(new_sl, 4)

class SignalMonitor:
    def __init__(self, config):
        self.active_signals={}
        self.config=config
        self.last_alert_level={}
    def add_signal(self, symbol, direction, score, entry_time=None):
        self.active_signals[symbol]={
            "direction":direction,
            "score":score,
            "entry_time":entry_time or datetime.utcnow()
        }
        self.last_alert_level[symbol]="none"
    def remove_signal(self, symbol):
        self.active_signals.pop(symbol, None)
        self.last_alert_level.pop(symbol, None)
    def format_time(self, dt):
        if isinstance(dt, datetime):
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return str(dt)
    def check_signal(self, symbol, current_score, current_direction, threshold, send_alert):
        if symbol not in self.active_signals or not self.config.get("enable_signal_monitor", False):
            return
        info=self.active_signals[symbol]
        score_drop_ratio = current_score / info["score"] if info["score"] else 1.0
        if self.config.get("alert_on_direction_change", True) and current_direction != info["direction"]:
            level="very_high"
        elif self.config.get("score_below_threshold_warn", True) and current_score < threshold:
            level="high"
        elif score_drop_ratio < self.config.get("score_drop_warn", 0.7):
            level="medium"
        else:
            level="none"
        last_level=self.last_alert_level.get(symbol,"none")
        if level != last_level:
            entry_time=self.format_time(info.get("entry_time"))
            base="\n==============================\n"
            if level=="medium":
                send_alert(
                    f"{base}🚨 **CẢNH BÁO TRUNG BÌNH: SCORE GIẢM**\n"
                    f"• {symbol} {info['direction']} -> {current_score}\n"
                    f"• Entry: {entry_time}\n"
                    f"👉 Xem xét khóa lợi nhuận\n=============================="
                )
            elif level=="high":
                send_alert(
                    f"{base}🚨🚨 **CẢNH BÁO CAO: SCORE < THRESHOLD**\n"
                    f"• {symbol} {info['direction']} -> {current_score} < {threshold}\n"
                    f"• Entry: {entry_time}\n👉 Siết SL mạnh\n=============================="
                )
            elif level=="very_high":
                send_alert(
                    f"{base}🚨🚨🚨 **ĐẢO CHIỀU** {symbol} {info['direction']} -> {current_direction}\n"
                    f"• SCORE hiện: {current_score}\n"
                    f"• Entry: {entry_time}\n👉 Đóng toàn bộ!\n=============================="
                )
                self.remove_signal(symbol)
            self.last_alert_level[symbol]=level
