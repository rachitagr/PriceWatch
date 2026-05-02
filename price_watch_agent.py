#!/usr/bin/env python3
"""Amazon price drop watcher using direct page checks and SMTP for email."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


AMAZON_HOSTS = {
    "US": "amazon.com",
    "UK": "amazon.co.uk",
    "IN": "amazon.in",
    "CA": "amazon.ca",
    "AU": "amazon.com.au",
    "DE": "amazon.de",
    "FR": "amazon.fr",
    "IT": "amazon.it",
    "ES": "amazon.es",
    "JP": "amazon.co.jp",
    "MX": "amazon.com.mx",
    "BR": "amazon.com.br",
    "NL": "amazon.nl",
    "SE": "amazon.se",
    "PL": "amazon.pl",
}


@dataclass(frozen=True)
class ProductWatch:
    asin: str
    name: str
    domain: str
    currency: str
    url: str
    drop_amount: float | None
    target_price: float | None


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_env(path: Path) -> None:
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def parse_watch(raw: dict[str, Any], default_email: dict[str, Any]) -> ProductWatch:
    drop_amount = raw.get("drop_amount")
    target_price = raw.get("target_price")
    if drop_amount is None and target_price is None:
        raise ValueError(f"{raw.get('asin', '<unknown>')}: set drop_amount or target_price")

    domain = raw.get("domain", default_email.get("default_domain", "US")).upper()
    if domain not in AMAZON_HOSTS:
        valid = ", ".join(sorted(AMAZON_HOSTS))
        raise ValueError(f"{raw.get('asin', '<unknown>')}: domain must be one of {valid}")

    asin = raw["asin"].strip().upper()
    return ProductWatch(
        asin=asin,
        name=raw.get("name") or asin,
        domain=domain,
        currency=raw.get("currency", default_email.get("currency", "USD")),
        url=raw.get("url") or amazon_url(asin, domain),
        drop_amount=float(drop_amount) if drop_amount is not None else None,
        target_price=float(target_price) if target_price is not None else None,
    )


def fetch_amazon_html(watch: ProductWatch) -> str:
    request = urllib.request.Request(
        watch.url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_price_text(value: str) -> float | None:
    cleaned = html.unescape(value).strip()
    cleaned = re.sub(r"\s+", "", cleaned)
    match = re.search(r"(\d[\d,.]*)", cleaned)
    if not match:
        return None

    number = match.group(1)
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        pieces = number.split(",")
        number = number.replace(",", ".") if len(pieces[-1]) == 2 else number.replace(",", "")

    try:
        return round(float(number), 2)
    except ValueError:
        return None


def extract_current_price(page_html: str) -> float | None:
    patterns = [
        r'<meta[^>]+itemprop=["\']price["\'][^>]+content=["\']([^"\']+)["\']',
        r'"priceToPay"[^}]+?"amount"\s*:\s*([0-9.]+)',
        r'"displayPrice"\s*:\s*"([^"]+)"',
        r'id=["\']priceblock_ourprice["\'][^>]*>\s*([^<]+)',
        r'id=["\']priceblock_dealprice["\'][^>]*>\s*([^<]+)',
        r'id=["\']price_inside_buybox["\'][^>]*>\s*([^<]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
        if match:
            price = parse_price_text(match.group(1))
            if price is not None:
                return price

    price_container = re.search(
        r'<span[^>]+class=["\'][^"\']*a-price[^"\']*["\'][^>]*>.*?</span>\s*</span>',
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if price_container:
        whole = re.search(
            r'class=["\'][^"\']*a-price-whole[^"\']*["\'][^>]*>\s*([^<]+)',
            price_container.group(0),
            re.IGNORECASE | re.DOTALL,
        )
        fraction = re.search(
            r'class=["\'][^"\']*a-price-fraction[^"\']*["\'][^>]*>\s*([^<]+)',
            price_container.group(0),
            re.IGNORECASE | re.DOTALL,
        )
        if whole:
            price_text = whole.group(1)
            if fraction:
                price_text = f"{price_text}.{fraction.group(1)}"
            return parse_price_text(price_text)

    if "captcha" in page_html.lower() or "robot check" in page_html.lower():
        raise RuntimeError("Amazon returned a bot-check page; try a less frequent schedule")

    return None


def amazon_url(asin: str, domain: str) -> str:
    return f"https://www.{AMAZON_HOSTS[domain]}/dp/{asin}"


def should_alert(
    watch: ProductWatch,
    current_price: float,
    previous_state: dict[str, Any],
) -> tuple[bool, str]:
    last_seen = previous_state.get("last_seen_price")
    last_alert = previous_state.get("last_alert_price")

    if watch.target_price is not None and current_price <= watch.target_price:
        if last_alert is None or current_price < float(last_alert):
            return True, f"price is at or below target {watch.currency} {watch.target_price:.2f}"

    if watch.drop_amount is not None and last_seen is not None:
        drop = float(last_seen) - current_price
        if drop >= watch.drop_amount:
            return True, f"price dropped by {watch.currency} {drop:.2f}"

    return False, ""


def send_email(config: dict[str, Any], watch: ProductWatch, price: float, reason: str) -> None:
    smtp = config["email"]
    username = require_env("SMTP_USERNAME")
    password = require_env("SMTP_PASSWORD")

    message = EmailMessage()
    message["From"] = smtp.get("from", username)
    message["To"] = smtp["to"]
    message["Subject"] = f"Price drop: {watch.name} is {watch.currency} {price:.2f}"
    message.set_content(
        "\n".join(
            [
                f"{watch.name}",
                f"ASIN: {watch.asin}",
                f"Current price: {watch.currency} {price:.2f}",
                f"Reason: {reason}",
                f"Link: {watch.url}",
            ]
        )
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp["host"], int(smtp.get("port", 465)), context=context) as server:
        server.login(username, password)
        server.send_message(message)


def run(config_path: Path, state_path: Path, dry_run: bool) -> int:
    load_env(config_path.parent / ".env")
    config = load_json(config_path)
    state = load_json(state_path) if state_path.exists() else {}
    request_delay_seconds = float(config.get("request_delay_seconds", 5))

    for index, raw_watch in enumerate(config["products"]):
        watch = parse_watch(raw_watch, config.get("email", {}))
        product_state = state.get(watch.asin, {})

        if index > 0 and request_delay_seconds > 0:
            time.sleep(request_delay_seconds)

        try:
            page_html = fetch_amazon_html(watch)
            price = extract_current_price(page_html)
        except Exception as exc:
            print(f"{watch.asin}: check failed: {exc}")
            continue

        if price is None:
            print(f"{watch.asin}: no current page price found")
            continue

        alert, reason = should_alert(watch, price, product_state)
        print(f"{watch.asin}: {watch.currency} {price:.2f}")

        if alert:
            if dry_run:
                print(f"  would email: {reason}")
            else:
                send_email(config, watch, price, reason)
                print(f"  emailed: {reason}")
                product_state["last_alert_price"] = price

        product_state["name"] = watch.name
        product_state["last_seen_price"] = price
        state[watch.asin] = product_state

    if not dry_run:
        save_json(state_path, state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch Amazon prices and email on drops.")
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--state", default="state.json", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        return run(args.config, args.state, args.dry_run)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
