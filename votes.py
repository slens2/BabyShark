from typing import Dict, List

DEFAULT_WEIGHTS: Dict[str, float] = {
    "EMA200": 2.65,
    "MA50": 1.27,
    "SUPERTREND": 2.12,
    "MACD": 1.80,
    "RSI": 1.16,
    "ADX": 1.16,
    "VWAP": 1.38,
    "RANGE": 1.38,
    "CHAIKIN_MF": 1.06,
    "VOLUME_SPIKE": 1.06,
    "STOCHRSI": 1.06,
    "BOLLINGERBANDS": 1.90
}

def _normalize_key(name: str) -> str:
    if not name: return ""
    return (name.strip().replace(" ","").replace("-","").replace("_","").upper())

def _to_direction(v):
    if v is None: return "NEUTRAL"
    s = str(v).strip().upper()
    if s in ("LONG","BUY","BULL"): return "LONG"
    if s in ("SHORT","SELL","BEAR"): return "SHORT"
    return "NEUTRAL"

def _normalize_weight_dict(d: Dict[str,float]) -> Dict[str,float]:
    out={}
    for k,v in (d or {}).items():
        nk=_normalize_key(k)
        if nk: out[nk]=float(v)
    return out

def tally_votes(indicators: Dict[str,str], weights: Dict[str,float]=None) -> Dict:
    wmap=_normalize_weight_dict(DEFAULT_WEIGHTS)
    if weights: wmap.update(_normalize_weight_dict(weights))
    votes_long=votes_short=0
    score_long=score_short=0.0
    long_list=[]; short_list=[]; neutral_list=[]
    breakdown_long = {}
    breakdown_short = {}
    breakdown_neutral = {}
    active_keys=set()

    # Sử dụng key đã normalize cho breakdown để cộng tay luôn khớp với score
    for raw_name, raw_dir in (indicators or {}).items():
        nk=_normalize_key(raw_name)
        d=_to_direction(raw_dir)
        active_keys.add(nk)
        weight=wmap.get(nk,1.0)
        if d == "LONG":
            votes_long+=1; score_long+=weight; long_list.append(nk); breakdown_long[nk]=weight
            breakdown_short[nk]=0.0
            breakdown_neutral[nk]=0.0
        elif d == "SHORT":
            votes_short+=1; score_short+=weight; short_list.append(nk); breakdown_short[nk]=weight
            breakdown_long[nk]=0.0
            breakdown_neutral[nk]=0.0
        else:
            neutral_list.append(nk); breakdown_neutral[nk]=weight
            breakdown_long[nk]=0.0
            breakdown_short[nk]=0.0

    total_weight=sum(wmap.values())
    active_total=sum(wmap.get(k,0.0) for k in active_keys)
    # Đảm bảo breakdown trả về cho tất cả các chỉ báo trong weights (kể cả nếu không active)
    for k in wmap.keys():
        if k not in breakdown_long:
            breakdown_long[k]=0.0
        if k not in breakdown_short:
            breakdown_short[k]=0.0
        if k not in breakdown_neutral:
            breakdown_neutral[k]=0.0

    return {
        "votes_long": votes_long,
        "votes_short": votes_short,
        "score_long": round(score_long,2),
        "score_short": round(score_short,2),
        "long_list": long_list,
        "short_list": short_list,
        "neutral_list": neutral_list,
        "breakdown_long": breakdown_long,
        "breakdown_short": breakdown_short,
        "breakdown_neutral": breakdown_neutral,
        "total_weight": round(total_weight,2),
        "active_total_weight": round(active_total,2),
    }
