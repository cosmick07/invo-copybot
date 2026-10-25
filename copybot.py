#!/usr/bin/env python3
"""
Invo-style copy-trading bot (PAPER MONEY ONLY).

Invo routes its trades through Hyperliquid, whose leaderboard and every
wallet's open positions are public. This bot:
  1. Pulls the Hyperliquid leaderboard and picks the top N traders using
     filters that favour consistency over one lucky week.
  2. Every run, reads each picked trader's open positions.
  3. Mirrors them into a fake-money account, scaled to the slice of the
     balance given to that trader, with fees and slippage charged.
  4. Logs trades and equity, and writes a plain-English report.

It never places a real order and holds no keys. Standard library only.

Usage:
  python copybot.py            # one run (schedule this every 5-15 min)
  python copybot.py --reset    # wipe the paper account and start again
  python copybot.py --mock     # offline test with simulated data
"""
import argparse, csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INFO_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

DEFAULT_CONFIG = {
    "starting_balance_usd": 10000,
    "num_traders": 5,
    "reselect_traders_every_hours": 24,
    "filters": {
        "min_account_value_usd": 100000,
        "min_alltime_pnl_usd": 250000,
        "min_month_roi": 0.05,
        "max_month_roi": 3.0,
        "require_positive_week": True,
        "min_month_volume_usd": 1000000
    },
    "rank_by": "month_pnl",
    "max_leverage": 3.0,
    "taker_fee": 0.00045,
    "slippage": 0.0005,
    "min_trade_usd": 10,
    "stop_if_drawdown_from_peak": 0.25,
    "coin_blacklist": []
}

# ---------------------------------------------------------------- helpers
def now():
    return datetime.now(timezone.utc).astimezone()

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)

def append_csv(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)

def http(url, body=None, retries=3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "paper-copybot/1.0"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(2 * (i + 1))

# ---------------------------------------------------------------- data source
class Live:
    def leaderboard(self):
        return http(LEADERBOARD_URL).get("leaderboardRows", [])
    def mids(self):
        return {k: float(v) for k, v in http(INFO_URL, {"type": "allMids"}).items()}
    def positions(self, addr):
        st = http(INFO_URL, {"type": "clearinghouseState", "user": addr})
        acct = float(st.get("marginSummary", {}).get("accountValue", 0) or 0)
        pos = {}
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            sz = float(p.get("szi", 0) or 0)
            if sz:
                pos[p["coin"]] = sz
        return acct, pos

class Mock:
    """Simulated market so the bot can be tested offline."""
    def __init__(self):
        import random
        self.r = random.Random(int(time.time()) // 300)
        self.base = {"BTC": 98000, "ETH": 3600, "SOL": 210, "HYPE": 38, "DOGE": 0.31}
    def leaderboard(self):
        rows = []
        for i in range(40):
            r = self.r
            mroi = r.uniform(-0.3, 1.2); acct = r.uniform(5e4, 2e7)
            rows.append({"ethAddress": f"0xmock{i:036d}", "displayName": None if i % 3 else f"trader{i}",
                         "accountValue": str(acct),
                         "windowPerformances": [
                             ["day", {"pnl": str(r.uniform(-5e4, 5e4)), "roi": "0", "vlm": "0"}],
                             ["week", {"pnl": str(r.uniform(-1e5, 3e5)), "roi": "0", "vlm": "0"}],
                             ["month", {"pnl": str(acct * mroi), "roi": str(mroi), "vlm": str(r.uniform(1e5, 5e8))}],
                             ["allTime", {"pnl": str(r.uniform(-1e6, 2e7)), "roi": "0", "vlm": "0"}]]})
        return rows
    def mids(self):
        return {c: p * (1 + self.r.uniform(-0.01, 0.01)) for c, p in self.base.items()}
    def positions(self, addr):
        r = __import__("random").Random(addr + str(int(time.time()) // 300))
        acct = r.uniform(2e5, 5e6); pos = {}
        for c, p in self.base.items():
            if r.random() < 0.4:
                pos[c] = r.choice([-1, 1]) * acct * r.uniform(0.2, 2.0) / p
        return acct, pos

# ---------------------------------------------------------------- trader selection
def perf(row, window):
    for w, d in row.get("windowPerformances", []):
        if w == window:
            return {k: float(d.get(k, 0) or 0) for k in ("pnl", "roi", "vlm")}
    return {"pnl": 0, "roi": 0, "vlm": 0}

def select_traders(rows, cfg):
    f = cfg["filters"]; picked = []
    for row in rows:
        m, w, a = perf(row, "month"), perf(row, "week"), perf(row, "allTime")
        acct = float(row.get("accountValue", 0) or 0)
        if acct < f["min_account_value_usd"]: continue
        if a["pnl"] < f["min_alltime_pnl_usd"]: continue
        if not (f["min_month_roi"] <= m["roi"] <= f["max_month_roi"]): continue
        if f["require_positive_week"] and w["pnl"] <= 0: continue
        if m["vlm"] < f["min_month_volume_usd"]: continue
        picked.append({"address": row["ethAddress"], "name": row.get("displayName") or (row["ethAddress"][:6] + ".." + row["ethAddress"][-4:]),
                       "account_value": acct, "month_pnl": m["pnl"], "month_roi": m["roi"],
                       "week_pnl": w["pnl"], "alltime_pnl": a["pnl"]})
    key = {"month_pnl": "month_pnl", "month_roi": "month_roi", "alltime_pnl": "alltime_pnl"}[cfg["rank_by"]]
    picked.sort(key=lambda t: t[key], reverse=True)
    return picked[: cfg["num_traders"]]

# ---------------------------------------------------------------- paper account
def fresh_state(cfg):
    return {"cash": cfg["starting_balance_usd"], "start": cfg["starting_balance_usd"],
            "peak_equity": cfg["starting_balance_usd"], "positions": {}, "traders": [],
            "traders_selected_at": 0, "halted": False, "started_at": now().isoformat(),
            "realized_pnl": 0.0, "fees_paid": 0.0, "runs": 0}

def equity(state, mids):
    eq = state["cash"]
    for p in state["positions"].values():
        px = mids.get(p["coin"], p["entry"])
        eq += p["size"] * (px - p["entry"])
    return eq

def gross_exposure(state, mids):
    return sum(abs(p["size"]) * mids.get(p["coin"], p["entry"]) for p in state["positions"].values())

def trade(state, cfg, leader, coin, delta, mid, why):
    """Change the paper position by `delta` units at mid +/- slippage."""
    key = f"{leader}|{coin}"
    side = 1 if delta > 0 else -1
    px = mid * (1 + side * cfg["slippage"])
    notional = abs(delta) * px
    fee = notional * cfg["taker_fee"]
    p = state["positions"].get(key, {"coin": coin, "leader": leader, "size": 0.0, "entry": px})
    old = p["size"]; new = old + delta; realized = 0.0
    if old == 0 or (old > 0) == (delta > 0):          # opening / adding
        p["entry"] = (abs(old) * p["entry"] + abs(delta) * px) / abs(new)
    else:                                              # reducing / closing / flipping
        closed = min(abs(delta), abs(old))
        realized = closed * (px - p["entry"]) * (1 if old > 0 else -1)
        if abs(delta) > abs(old):
            p["entry"] = px
    p["size"] = new
    state["cash"] += realized - fee
    state["realized_pnl"] += realized
    state["fees_paid"] += fee
    if abs(new) * px < 1e-6:
        state["positions"].pop(key, None)
    else:
        state["positions"][key] = p
    append_csv(os.path.join(DATA, "trades.csv"),
               ["time", "leader", "coin", "side", "units", "price", "notional_usd", "fee_usd", "realized_pnl_usd", "reason"],
               [now().isoformat(timespec="seconds"), leader, coin, "BUY" if delta > 0 else "SELL",
                f"{abs(delta):.6g}", f"{px:.6g}", f"{notional:.2f}", f"{fee:.2f}", f"{realized:.2f}", why])
    return notional

# ---------------------------------------------------------------- main loop
def run(src, cfg, state, log):
    mids = src.mids()
    eq = equity(state, mids)

    # Kill switch
    state["peak_equity"] = max(state["peak_equity"], eq)
    dd = 1 - eq / state["peak_equity"] if state["peak_equity"] else 0
    if not state["halted"] and dd >= cfg["stop_if_drawdown_from_peak"]:
        state["halted"] = True
        log(f"KILL SWITCH: equity down {dd:.0%} from peak - closing everything and stopping.")
        for key, p in list(state["positions"].items()):
            trade(state, cfg, p["leader"], p["coin"], -p["size"], mids.get(p["coin"], p["entry"]), "kill switch")
    if state["halted"]:
        log("Bot is halted (drawdown limit hit). Run with --reset to start a new paper account.")
        return mids

    # (Re)select traders
    age_h = (time.time() - state["traders_selected_at"]) / 3600
    if not state["traders"] or age_h >= cfg["reselect_traders_every_hours"]:
        new = select_traders(src.leaderboard(), cfg)
        old_addrs = {t["address"] for t in state["traders"]}
        new_addrs = {t["address"] for t in new}
        for key, p in list(state["positions"].items()):
            if p["leader"] not in new_addrs:
                trade(state, cfg, p["leader"], p["coin"], -p["size"], mids.get(p["coin"], p["entry"]), "trader dropped")
        state["traders"] = new; state["traders_selected_at"] = time.time()
        log(f"Selected {len(new)} traders ({len(new_addrs - old_addrs)} new).")
        if not new:
            log("No traders passed the filters - loosen them in config.json.")
            return mids

    eq = equity(state, mids)
    slice_usd = eq / len(state["traders"])
    cap = cfg["max_leverage"]

    for t in state["traders"]:
        try:
            acct, lpos = src.positions(t["address"])
        except Exception as e:
            log(f"  {t['name']}: could not read positions ({e}); leaving their copies unchanged.")
            continue
        if acct <= 0:
            continue
        scale = slice_usd / acct
        # Target = leader position scaled to our slice, then capped at max leverage on that slice
        targets = {c: s * scale for c, s in lpos.items()
                   if c in mids and c not in cfg["coin_blacklist"]}
        gross = sum(abs(s) * mids[c] for c, s in targets.items())
        if gross > cap * slice_usd and gross > 0:
            k = cap * slice_usd / gross
            targets = {c: s * k for c, s in targets.items()}
        held = {p["coin"]: p["size"] for p in state["positions"].values() if p["leader"] == t["address"]}
        for coin in set(targets) | set(held):
            delta = targets.get(coin, 0.0) - held.get(coin, 0.0)
            if abs(delta) * mids.get(coin, 0) < cfg["min_trade_usd"]:
                continue
            why = "leader opened" if coin not in held else ("leader closed" if coin not in targets else "leader resized")
            trade(state, cfg, t["address"], coin, delta, mids[coin], why)
            log(f"  copy {t['name']}: {'BUY' if delta > 0 else 'SELL'} {abs(delta):.4g} {coin} ({why})")
    return mids

def write_report(state, cfg, mids):
    eq = equity(state, mids)
    ret = eq / state["start"] - 1
    lines = [f"# Copy-bot paper report - {now():%a %d %b %Y %H:%M}", "",
             f"**Equity:** ${eq:,.2f}  ({ret:+.2%} since {state['started_at'][:10]})",
             f"**Realised P&L:** ${state['realized_pnl']:,.2f}   **Fees + slippage paid:** ${state['fees_paid']:,.2f}",
             f"**Exposure:** ${gross_exposure(state, mids):,.0f} across {len(state['positions'])} positions"
             f"   **Peak:** ${state['peak_equity']:,.2f}",
             f"**Status:** {'HALTED by kill switch' if state['halted'] else 'running'}", "",
             "## Traders being copied", "",
             "| Trader | Month ROI | Month P&L | All-time P&L | Our open P&L |", "|---|---|---|---|---|"]
    for t in state["traders"]:
        upnl = sum(p["size"] * (mids.get(p["coin"], p["entry"]) - p["entry"])
                   for p in state["positions"].values() if p["leader"] == t["address"])
        lines.append(f"| {t['name']} | {t['month_roi']:.0%} | ${t['month_pnl']:,.0f} | ${t['alltime_pnl']:,.0f} | ${upnl:,.2f} |")
    lines += ["", "## Open paper positions", "", "| Trader | Coin | Side | Units | Entry | Now | Open P&L |",
              "|---|---|---|---|---|---|---|"]
    names = {t["address"]: t["name"] for t in state["traders"]}
    for p in sorted(state["positions"].values(), key=lambda p: p["coin"]):
        px = mids.get(p["coin"], p["entry"])
        lines.append(f"| {names.get(p['leader'], p['leader'][:10])} | {p['coin']} | {'LONG' if p['size'] > 0 else 'SHORT'} | "
                     f"{abs(p['size']):.4g} | {p['entry']:.6g} | {px:.6g} | ${p['size'] * (px - p['entry']):,.2f} |")
    with open(os.path.join(DATA, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")
    append_csv(os.path.join(DATA, "equity.csv"), ["time", "equity_usd", "return", "open_positions"],
               [now().isoformat(timespec="seconds"), f"{eq:.2f}", f"{ret:.4f}", len(state["positions"])])
    return eq, ret

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="use simulated data")
    ap.add_argument("--reset", action="store_true", help="start a fresh paper account")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    cfg_path = os.path.join(HERE, "config.json")
    if not os.path.exists(cfg_path):
        save_json(cfg_path, DEFAULT_CONFIG)
    cfg = {**DEFAULT_CONFIG, **load_json(cfg_path, {})}
    cfg["filters"] = {**DEFAULT_CONFIG["filters"], **cfg.get("filters", {})}
    state_path = os.path.join(DATA, "state.json")
    if a.reset:
        for fn in ("state.json", "trades.csv", "equity.csv", "report.md"):
            try: os.remove(os.path.join(DATA, fn))
            except FileNotFoundError: pass
    state = load_json(state_path, None) or fresh_state(cfg)
    logf = open(os.path.join(DATA, "bot.log"), "a")
    def log(msg):
        line = f"{now():%Y-%m-%d %H:%M:%S} {msg}"
        print(line); logf.write(line + "\n"); logf.flush()
    try:
        mids = run(Mock() if a.mock else Live(), cfg, state, log)
        state["runs"] += 1
        save_json(state_path, state)
        eq, ret = write_report(state, cfg, mids)
        log(f"Run {state['runs']} done. Paper equity ${eq:,.2f} ({ret:+.2%}).")
    except Exception as e:
        log(f"ERROR: {e!r} - state left unchanged; will retry next run.")
        sys.exit(1)

if __name__ == "__main__":
    main()
