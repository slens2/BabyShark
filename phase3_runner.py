# -*- coding: utf-8 -*-
import json
import time
from datetime import datetime, timezone
from typing import Dict

from data import fetch_data
from indicators import calculate_indicators
from votes import tally_votes
from report_utils import format_votes

from tight_gate import (
    StablePassTracker,
    CooldownManager,
    anti_chase_ok,
    build_indicator_results,
    _heavy_hits,
)
from order_planner import plan_probe_and_topup
from exec_engine import ExecutionEngine

CONFIG_PATH = "config.json"

def load_config() -> Dict:
    return json.load(open(CONFIG_PATH, "r", encoding="utf-8"))

def decide_side(score_long: float, score_short: float, eps: float = 0.1) -> str:
    if score_long - score_short > eps:
        return "LONG"
    if score_short - score_long > eps:
        return "SHORT"
    return "NEUTRAL"

def check_indicator_input(df, min_required, label):
    if df is None or df.empty or len(df) < min_required:
        print(f"[WARN] {label}: DataFrame quá nhỏ ({len(df) if df is not None else 0}), cần >= {min_required} dòng!")
        return False
    return True

def main():
    cfg = load_config()
    eng = ExecutionEngine(cfg)

    symbols = cfg.get("symbols", ["BTC/USDT"])
    timeframes = [tf for tf in cfg.get("timeframes", ["15m"]) if tf == "15m"]
    if not timeframes:
        timeframes = ["15m"]

    th_m15 = float((cfg.get("thresholds") or {}).get("M15", cfg.get("score_threshold", 17.0)))
    adx_h1_th = int(cfg.get("adx_h1_threshold", 25))
    anti_mult = float(cfg.get("tight_mode", {}).get("anti_chase_atr_mult", 0.5))
    cooldown_min = int(cfg.get("tight_mode", {}).get("cooldown_m15_min", 15))
    heavy_required = int(cfg.get("tight_mode", {}).get("heavy_required", 3))

    stable = StablePassTracker(
        path=cfg.get("tight_mode", {}).get("state_path", "tight_state.json"),
        min_gap_sec=int(cfg.get("tight_mode", {}).get("snapshot_min_gap_sec", 300)),
        required_passes=int(cfg.get("tight_mode", {}).get("snapshot_confirmations", 2)),
    )
    cd = CooldownManager(path=cfg.get("tight_mode", {}).get("cooldown_path", "tight_cooldown.json"))

    for symbol in symbols:
        for timeframe in timeframes:
            print(f"\n[Phase3] {symbol} {timeframe} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
            now_ts = time.time()

            m15 = fetch_data(symbol, "15m", limit=300)
            h1 = fetch_data(symbol, "1h", limit=300)
            if not check_indicator_input(m15, 200, f"{symbol} M15"): continue
            if not check_indicator_input(h1, 200, f"{symbol} H1"): continue

            ind_m15 = calculate_indicators(m15, cfg)
            ind_h1 = calculate_indicators(h1, cfg)

            map_m15 = build_indicator_results(m15, ind_m15)
            map_h1 = build_indicator_results(h1, ind_h1)

            wsets = (cfg or {}).get("weights_sets", {})
            w_m15 = wsets.get("M15", {})
            w_h1 = wsets.get("H1", {})
            vr_m15 = tally_votes(map_m15, w_m15)
            sl15, ss15 = float(vr_m15.get("score_long", 0)), float(vr_m15.get("score_short", 0))
            side = decide_side(sl15, ss15)

            adx_h1 = float(ind_h1["adx"].iloc[-1])
            hh1 = _heavy_hits(map_h1, ind_h1["ema200"], side) if side != "NEUTRAL" else 0
            h1_ok = (adx_h1 >= adx_h1_th) and (hh1 >= heavy_required) and (side != "NEUTRAL")

            m15_score = sl15 if side == "LONG" else (ss15 if side == "SHORT" else 0.0)
            m15_ok = (side != "NEUTRAL") and (m15_score >= th_m15)

            ac_ok, c, v, a = anti_chase_ok(m15, ind_m15, mult=anti_mult)

            gates_ok = h1_ok and m15_ok and ac_ok and (side != "NEUTRAL")
            is_stable = stable.update(symbol, timeframe, side, gates_ok, now_ts=now_ts)
            in_cooldown = cd.in_cooldown(symbol, timeframe, cooldown_sec=cooldown_min * 60, now_ts=now_ts)

            print(f" - Side(M15): {side} | score={m15_score:.2f} (th={th_m15}) | m15_ok={m15_ok}")
            print(f" - H1: ADX={adx_h1:.2f} (th={adx_h1_th}), Heavy={hh1}/{max(3, heavy_required)} | h1_ok={h1_ok}")
            print(f" - Anti-chase: |{c:.4f}-{v:.4f}|={abs(c-v):.4f} ≤ {anti_mult}*ATR({a:.4f}) → {ac_ok}")
            print(f" - Stable(2×M5): {is_stable} | Cooldown({cooldown_min}m): {in_cooldown}")

            last_price = float(m15["close"].iloc[-1])

            if gates_ok and is_stable and not in_cooldown:
                plan = plan_probe_and_topup(side, m15, ind_m15, cfg)
                summary = eng.tick(symbol, timeframe, side, plan, last_price, ts_now=now_ts)
                for act in summary["actions"]:
                    print("   *", act)
                ok, msg = eng.promote_to_full(symbol, timeframe, plan, last_price)
                if ok:
                    print("   *", msg)
                cd.mark(symbol, timeframe, now_ts=now_ts)
                try:
                    m15_text = format_votes(map_m15, w_m15)
                    h1_text = format_votes(map_h1, w_h1)
                    print("\n[M15 votes]\n" + m15_text)
                    print("\n[H1 votes]\n" + h1_text)
                except Exception:
                    pass
            else:
                reasons = []
                if not h1_ok: reasons.append("H1 gate")
                if not m15_ok: reasons.append("M15 score")
                if not ac_ok: reasons.append("Anti-chase")
                if side == "NEUTRAL": reasons.append("Side neutral")
                if in_cooldown: reasons.append("Cooldown")
                if not is_stable: reasons.append("Not stable 2×M5")
                print(" - BLOCKED by:", ", ".join(reasons))

    print("\n[Done] Phase 3 runner kết thúc.")

if __name__ == "__main__":
    main()
