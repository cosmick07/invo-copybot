# Invo-style Copy Bot (paper money)

Copies the top Hyperliquid traders (the same traders Invo lets you mirror) into a **$10,000 fake-money account**. No keys, no real orders.

Runs automatically on GitHub every ~10 minutes, PC on or off.

## Check on it (works from your phone)
- **[data/report.md](data/report.md)** - equity, traders being copied, open positions
- [data/equity.csv](data/equity.csv) - equity over time
- [data/trades.csv](data/trades.csv) - every copied trade
- [data/bot.log](data/bot.log) - what happened each run
- **Actions** tab - green tick = run worked, red cross = failed

## What it does each run
1. Once a day, picks the top 5 traders on the Hyperliquid leaderboard who pass the filters in `config.json`.
2. Gives each an equal slice of the paper balance and mirrors their positions, capped at 3x leverage, with fees and slippage charged.
3. Drops traders who fall off the list and closes their copies.
4. Kill switch: if equity falls 25% from its peak, it closes everything and stops.

## Controls
- **Pause:** Actions tab -> copy-bot -> ... menu -> Disable workflow
- **Run now:** Actions tab -> copy-bot -> Run workflow
- **Tune:** edit `config.json` (number of traders, leverage cap, kill switch, filters)

## Limits
- GitHub's schedule can lag by several minutes at busy times, so fast scalpers copy badly.
- Funding payments aren't modelled.
- Last month's leaderboard winners are often next month's losers. Treat this as an experiment.
