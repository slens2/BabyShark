# -*- coding: utf-8 -*-
import csv
import json
import os
from typing import Dict, Tuple

from broker import get_broker
from notifier import Notifier

STATE_PATH_DEFAULT = "trade_state.json"
TRADES_LOG_CSV = "trades_log.csv"

class ExecutionEngine:
    def __init__(self, cfg: Dict):
        self.cfg = cfg or {}
        self.state_path = self.cfg.get("trading", {}).get("state_path", STATE_PATH_DEFAULT)
        self.state = self._load_state()
        self.broker = get_broker(cfg)
        self.notifier = Notifier(cfg)

    def _load_state(self) -> Dict:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"positions": {}, "orders": {}, "pair_index": {}}

    def _save_state(self):
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _pair_key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}|{timeframe}"

    def _has_active_position(self, pair_key: str) -> bool:
        return pair_key in (self.state.get("positions") or {})

    def _get_active_order_id(self, pair_key: str) -> str:
        return (self.state.get("pair_index") or {}).get(pair_key, "")

    def _set_active_order_id(self, pair_key: str, order_id: str):
        self.state.setdefault("pair_index", {})[pair_key] = order_id

    def _clear_active_order_id(self, pair_key: str):
        self.state.setdefault("pair_index", {}).pop(pair_key, None)

    def _append_trade_log(self, row: Dict):
        header = ["symbol","timeframe","side","stage","entry","exit","pnl","pnl_r","sl","tp","size","opened_at","closed_at"]
        exists = os.path.exists(TRADES_LOG_CSV)
        try:
            with open(TRADES_LOG_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=header)
                if not exists:
                    w.writeheader()
                w.writerow({k: row.get(k) for k in header})
        except Exception:
            pass

    def tick(self, symbol: str, timeframe: str, side: str, plan: Dict, price_now: float, ts_now: float = None) -> Dict:
        if ts_now is None:
            ts_now = self.broker.now()

        summary = {"actions": []}
        pair_key = self._pair_key(symbol, timeframe)
        trading = self.cfg.get("trading", {})
        allow_mkt_fallback = bool(trading.get("allow_market_fallback", True))
        slippage_guard_pct = float(trading.get("slippage_guard_pct", 0.2))

        active_oid = self._get_active_order_id(pair_key)
        if active_oid:
            actions = self._handle_pending_order(active_oid, symbol, timeframe, price_now, ts_now, allow_mkt_fallback, slippage_guard_pct)
            summary["actions"].extend(actions)

        if (not self._has_active_position(pair_key)) and (not self._get_active_order_id(pair_key)):
            entry = float(plan["entry_price"])
            ttl_sec = int(plan.get("ttl_sec", 30))
            size_probe = float(plan.get("size_probe", 0.0))
            if size_probe > 0:
                od = self.broker.place_limit(symbol, side, entry, size_probe, ttl_sec=ttl_sec)
                self.state.setdefault("orders", {})[od["id"]] = od
                self._set_active_order_id(pair_key, od["id"])
                msg = (
                    f"ĐẶT LỆNH THĂM DÒ GIÁ GIỚI HẠN {symbol} {side}\n"
                    f"├ Số lượng: {size_probe}\n"
                    f"├ Giá: {entry}\n"
                    f"└ TTL: {ttl_sec}s"
                )
                summary["actions"].append(
                    f"Đặt lệnh thăm dò id={od['id']} giá={entry} khối lượng={size_probe} thời hạn TTL={ttl_sec}s"
                )
                self.notifier.send(msg)
                self._save_state()

        pos = (self.state.get("positions") or {}).get(pair_key)
        if pos:
            actions = self._update_risk_and_exit(pair_key, pos, price_now, plan, ts_now)
            summary["actions"].extend(actions)
            self._save_state()

        return summary

    def _handle_pending_order(self, order_id: str, symbol: str, timeframe: str, price_now: float, ts_now: float, allow_mkt_fallback: bool, slippage_guard_pct: float):
        actions = []
        od = (self.state.get("orders") or {}).get(order_id)
        if not od:
            self._clear_active_order_id(self._pair_key(symbol, timeframe))
            return actions

        if od["status"] != "open":
            return actions

        side = od["side"]
        limit_px = float(od["price"])
        ok_cross = (price_now <= limit_px) if side == "LONG" else (price_now >= limit_px)
        slip_pct = abs(price_now - limit_px) / max(1e-12, limit_px) * 100.0
        within_slip = slip_pct <= slippage_guard_pct

        pair_key = self._pair_key(symbol, timeframe)

        if ok_cross and within_slip:
            od["status"] = "filled"
            od["filled_price"] = float(price_now)
            od["filled_size"] = float(od["size"])
            actions.append(f"Khớp lệnh thăm dò id={order_id} giá={price_now}")
            self.state.setdefault("positions", {})[pair_key] = {
                "symbol": symbol,
                "timeframe": timeframe,
                "side": side,
                "size": float(od["size"]),
                "entry": float(price_now),
                "stage": "probe",
                "opened_at": ts_now,
                "sl": None,
                "tp": None,
                "r_value": None
            }
            self._clear_active_order_id(pair_key)
            self.notifier.send(
                f"KHỚP LỆNH THĂM DÒ {symbol} {side}\n├ Số lượng: {od['size']}\n└ Giá: {round(price_now,6)}"
            )
        else:
            if ts_now >= float(od["expires_at"]):
                if allow_mkt_fallback and within_slip:
                    od["status"] = "filled"
                    od["type"] = "market_fallback"
                    od["filled_price"] = float(price_now)
                    od["filled_size"] = float(od["size"])
                    actions.append(f"Khớp lệnh thăm dò (fallback thị trường) id={order_id} giá={price_now}")
                    self.state.setdefault("positions", {})[pair_key] = {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "side": side,
                        "size": float(od["size"]),
                        "entry": float(price_now),
                        "stage": "probe",
                        "opened_at": ts_now,
                        "sl": None,
                        "tp": None,
                        "r_value": None
                    }
                    self._clear_active_order_id(pair_key)
                    self.notifier.send(
                        f"KHỚP LỆNH THĂM DÒ (Thị trường) {symbol} {side}\n├ Số lượng: {od['size']}\n└ Giá: {round(price_now,6)}"
                    )
                else:
                    od["status"] = "canceled"
                    actions.append(f"Hủy lệnh thăm dò id={order_id}")
                    self._clear_active_order_id(pair_key)
                    self.notifier.send(
                        f"HỦY LỆNH THĂM DÒ {symbol} do hết TTL\n├ Trượt giá: {round(slip_pct,3)}%\n└ Bảo vệ: {slippage_guard_pct}%"
                    )
        return actions

    def promote_to_full(self, symbol: str, timeframe: str, plan: Dict, price_now: float) -> Tuple[bool, str]:
        pair_key = self._pair_key(symbol, timeframe)
        pos = (self.state.get("positions") or {}).get(pair_key)
        if not pos or pos.get("stage") != "probe":
            return False, "no_probe"

        add_size = float(plan.get("size_full", 0.0)) - float(pos["size"])
        if add_size <= 0:
            pos["stage"] = "full"
            self._save_state()
            return True, "already_full"

        entry_old = float(pos["entry"])
        size_old = float(pos["size"])
        size_new = size_old + add_size
        entry_new = (entry_old * size_old + price_now * add_size) / max(1e-12, size_new)

        pos["entry"] = entry_new
        pos["size"] = size_new
        pos["stage"] = "full"
        pos["sl"] = float(plan.get("sl"))
        pos["tp"] = float(plan.get("tp"))
        pos["r_value"] = float(plan.get("r_value", 0.0))
        self.state.setdefault("positions", {})[pair_key] = pos
        self._save_state()
        self.notifier.send(
            f"NÂNG LỆNH THĂM DÒ LÊN FULL {symbol}\n├ Số lượng mới: {round(size_new,6)}\n└ Giá bình quân: {round(entry_new,6)}"
        )
        return True, f"nâng lên full size={size_new} avg_entry={round(entry_new,6)}"

    def _update_risk_and_exit(self, pair_key: str, pos: Dict, price_now: float, plan: Dict, ts_now: float):
        actions = []
        side = pos["side"]
        entry = float(pos["entry"])
        sl = pos.get("sl")
        tp = pos.get("tp")
        r_value = float(pos.get("r_value") or plan.get("r_value") or 0.0)
        breakeven_at = float((self.cfg.get("tight_mode") or {}).get("breakeven_at_r", 0.7))
        trailing_at = float((self.cfg.get("tight_mode") or {}).get("trailing_at_r", 1.0))

        if sl is None or tp is None or (pos.get("r_value") is None and r_value > 0):
            sl = float(plan.get("sl"))
            tp = float(plan.get("tp"))
            pos["sl"] = sl
            pos["tp"] = tp
            pos["r_value"] = float(r_value)
            actions.append(f"Thiết lập SL/TP ban đầu SL={round(sl,6)} TP={round(tp,6)}")
            self.notifier.send(f"THIẾT LẬP SL/TP ban đầu SL={round(sl,6)} TP={round(tp,6)}")

        hit_tp = (price_now >= tp) if side == "LONG" else (price_now <= tp)
        hit_sl = (price_now <= sl) if side == "LONG" else (price_now >= sl)

        if hit_tp or hit_sl:
            result = "CHỐT LỜI" if hit_tp else "DỪNG LỖ"
            size = float(pos.get("size", 0.0))
            pnl = (price_now - entry) * size if side == "LONG" else (entry - price_now) * size
            pnl_r = (pnl / (r_value * size)) if (r_value > 0 and size > 0) else 0.0
            self._append_trade_log({
                "symbol": pos["symbol"], "timeframe": pos["timeframe"], "side": side,
                "stage": pos.get("stage", ""), "entry": round(entry,6), "exit": round(price_now,6),
                "pnl": round(pnl,6), "pnl_r": round(pnl_r,3),
                "sl": round(pos.get("sl", 0.0),6), "tp": round(pos.get("tp", 0.0),6),
                "size": round(size,6), "opened_at": int(pos.get("opened_at", 0)), "closed_at": int(ts_now)
            })
            self.notifier.send(
                f"ĐÓNG LỆNH {result} {pos['symbol']} {side}\n├ Lợi nhuận: {round(pnl,6)}\n└ ({round(pnl_r,2)}R)"
            )
            (self.state.get("positions") or {}).pop(pair_key, None)
            actions.append(f"Đóng lệnh {result} tại giá={round(price_now,6)}")
            return actions

        if r_value > 0:
            gain = (price_now - entry) if side == "LONG" else (entry - price_now)
            if gain >= breakeven_at * r_value:
                new_sl = entry
                if (side == "LONG" and (sl is None or new_sl > sl)) or (side == "SHORT" and (sl is None or new_sl < sl)):
                    pos["sl"] = new_sl
                    actions.append(f"Đưa SL về hòa vốn tại={round(new_sl,6)}")
                    self.notifier.send(f"ĐƯA SL VỀ HÒA VỐN {pos['symbol']} tại={round(new_sl,6)}")

            if gain >= trailing_at * r_value:
                if side == "LONG":
                    trail_sl = price_now - r_value
                    if trail_sl > pos["sl"]:
                        pos["sl"] = trail_sl
                        actions.append(f"SL động (trailing) lên={round(trail_sl,6)}")
                        self.notifier.send(f"SL ĐỘNG (TRAILING) {pos['symbol']} lên={round(trail_sl,6)}")
                else:
                    trail_sl = price_now + r_value
                    if trail_sl < pos["sl"]:
                        pos["sl"] = trail_sl
                        actions.append(f"SL động (trailing) xuống={round(trail_sl,6)}")
                        self.notifier.send(f"SL ĐỘNG (TRAILING) {pos['symbol']} xuống={round(trail_sl,6)}")
        return actions
