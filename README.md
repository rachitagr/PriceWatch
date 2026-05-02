# Amazon Price Drop Agent

This is a small scheduled agent that checks an Amazon product page and emails you when the price drops.

## Important

This version does not use paid APIs. It fetches the Amazon product page directly and tries to parse the displayed price. That means it is free, but it can break if Amazon changes the page, shows a CAPTCHA, blocks the request, or returns a different page for your location.

If you see `Amazon returned a bot-check page`, reduce the schedule frequency, try running from your own machine/network, or use the optional `url` field with the exact product page you normally open. There is no fully reliable free Amazon price API.

## Setup

1. Copy the examples:

   ```bash
   cp config.example.json config.json
   cp .env.example .env
   ```

2. Edit `config.json` with the ASIN, domain, currency, and alert threshold. You can also add a full `url` field for a product if you want to track a specific product URL.

3. Export your secrets:

   ```bash
   set -a
   source .env
   set +a
   ```

4. Run a dry check:

   ```bash
   python3 price_watch_agent.py --dry-run
   ```

5. Run for real:

   ```bash
   python3 price_watch_agent.py
   ```

## Config fields

- `asin`: Amazon product ASIN.
- `domain`: Amazon marketplace, for example `US`, `IN`, `UK`, `CA`, `DE`.
- `url`: Optional full Amazon product URL. If omitted, the agent builds one from `asin` and `domain`.
- `drop_amount`: Email if the current price is at least this much lower than the last seen price.
- `target_price`: Email if the current price is at or below this price.
- `request_delay_seconds`: Delay between product page checks. Keep this conservative.

You can use either `drop_amount`, `target_price`, or both.

## Scheduling

Cron example for checking every 6 hours:

```cron
0 */6 * * * cd /path/to/this/folder && set -a && . ./.env && set +a && python3 price_watch_agent.py
```

On Gmail, use an app password rather than your normal password.
