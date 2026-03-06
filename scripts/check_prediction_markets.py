"""Quick manual connectivity script for Polymarket and Kalshi."""

import asyncio
import os

import httpx


async def check_polymarket() -> None:
    print("=" * 60)
    print("POLYMARKET CHECK")
    print("=" * 60)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(
                "https://gamma-api.polymarket.com/markets",
                params={"active": "true", "closed": "false"},
            )
            print(f"Status: {resp.status_code}")
            data = resp.json()
            print(f"Markets returned: {len(data)}")

            if not data:
                print("No markets returned!")
                return

            market = data[0]
            print(f"Sample keys: {list(market.keys())[:12]}")
            print(f"Question: {str(market.get('question', ''))[:100]}")
            print(f"outcomes: {market.get('outcomes')}")
            print(f"outcomePrices: {market.get('outcomePrices')}")
            print(f"conditionId: {market.get('conditionId')}")
            print(f"id: {market.get('id')}")

            valid = 0
            for item in data[:50]:
                if item.get("outcomePrices") and item.get("outcomes"):
                    valid += 1
            print(f"\nOf first 50 markets, {valid} have outcomes+prices")
        except Exception as exc:
            print(f"ERROR: {exc}")


async def check_kalshi() -> None:
    print("\n" + "=" * 60)
    print("KALSHI CHECK (unauthenticated)")
    print("=" * 60)
    base = "https://api.elections.kalshi.com/trade-api/v2"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{base}/events", params={"status": "active", "limit": "5"})
            print(f"Status: {resp.status_code}")
            if resp.status_code != 200:
                print(f"Response: {resp.text[:300]}")
                return

            data = resp.json()
            events = data.get("events", [])
            print(f"Events returned: {len(events)}")
            if not events:
                return

            event = events[0]
            print(f"Sample keys: {list(event.keys())[:10]}")
            print(f"Title: {event.get('title', '')[:100]}")
            print(f"Category: {event.get('category')}")
            print(f"Ticker: {event.get('event_ticker')}")
        except Exception as exc:
            print(f"ERROR: {exc}")


async def check_kalshi_key() -> None:
    print("\n" + "=" * 60)
    print("KALSHI KEY CHECK")
    print("=" * 60)
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "/app/secrets/kalshi.key")
    print(f"Key path: {key_path}")
    print(f"Exists: {os.path.exists(key_path)}")
    if not os.path.exists(key_path):
        return

    with open(key_path, "rb") as file_obj:
        content = file_obj.read()
    print(f"Size: {len(content)} bytes")
    lines = content.decode("utf-8", errors="replace").strip().split("\n")
    print(f"Lines: {len(lines)}")
    for i, line in enumerate(lines):
        print(f"  Line {i + 1}: len={len(line.rstrip())}  {line.rstrip()[:30]}...")

    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_private_key(content, password=None, backend=default_backend())
        print(f"Key loaded OK! Type: {type(key).__name__}")
    except Exception as exc:
        print(f"Key load FAILED: {exc}")


async def main() -> None:
    await check_polymarket()
    await check_kalshi()
    await check_kalshi_key()


if __name__ == "__main__":
    asyncio.run(main())