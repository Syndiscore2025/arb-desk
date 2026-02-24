"""Quick connectivity script for Polymarket and Kalshi.

Note: this is a *manual* debug script, not a pytest test module.
Pytest will try to collect it because of the filename, so we skip it when
imported under pytest.
"""

import sys

if "pytest" in sys.modules:  # pragma: no cover
    import pytest

    pytest.skip(
        "scripts/test_prediction_markets.py is a manual connectivity script; run it via `python scripts/test_prediction_markets.py`",
        allow_module_level=True,
    )

import asyncio
import httpx
import os
import json


async def test_polymarket():
    print("=" * 60)
    print("POLYMARKET TEST")
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

            if data:
                m = data[0]
                print(f"Sample keys: {list(m.keys())[:12]}")
                print(f"Question: {str(m.get('question', ''))[:100]}")
                print(f"outcomes: {m.get('outcomes')}")
                print(f"outcomePrices: {m.get('outcomePrices')}")
                print(f"conditionId: {m.get('conditionId')}")
                print(f"id: {m.get('id')}")

                # Count how many have valid prices
                valid = 0
                for market in data[:50]:
                    prices = market.get("outcomePrices", [])
                    outcomes = market.get("outcomes", [])
                    if prices and outcomes:
                        valid += 1
                print(f"\nOf first 50 markets, {valid} have outcomes+prices")
            else:
                print("No markets returned!")
        except Exception as e:
            print(f"ERROR: {e}")


async def test_kalshi():
    print("\n" + "=" * 60)
    print("KALSHI TEST (unauthenticated)")
    print("=" * 60)
    base = "https://api.elections.kalshi.com/trade-api/v2"
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(
                f"{base}/events",
                params={"status": "active", "limit": "5"},
            )
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                data = resp.json()
                events = data.get("events", [])
                print(f"Events returned: {len(events)}")
                if events:
                    e = events[0]
                    print(f"Sample keys: {list(e.keys())[:10]}")
                    print(f"Title: {e.get('title', '')[:100]}")
                    print(f"Category: {e.get('category')}")
                    print(f"Ticker: {e.get('event_ticker')}")
            else:
                print(f"Response: {resp.text[:300]}")
        except Exception as e:
            print(f"ERROR: {e}")


async def test_kalshi_key():
    print("\n" + "=" * 60)
    print("KALSHI KEY TEST")
    print("=" * 60)
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "/app/secrets/kalshi.key")
    print(f"Key path: {key_path}")
    print(f"Exists: {os.path.exists(key_path)}")
    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            content = f.read()
        print(f"Size: {len(content)} bytes")
        lines = content.decode("utf-8", errors="replace").strip().split("\n")
        print(f"Lines: {len(lines)}")
        for i, line in enumerate(lines):
            print(f"  Line {i+1}: len={len(line.rstrip())}  {line.rstrip()[:30]}...")

        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.backends import default_backend
            key = serialization.load_pem_private_key(content, password=None, backend=default_backend())
            print(f"Key loaded OK! Type: {type(key).__name__}")
        except Exception as e:
            print(f"Key load FAILED: {e}")


async def main():
    await test_polymarket()
    await test_kalshi()
    await test_kalshi_key()


if __name__ == "__main__":
    asyncio.run(main())

