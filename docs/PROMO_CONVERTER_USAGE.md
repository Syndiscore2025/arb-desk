# Promo Converter Usage Guide

The Promo Converter calculates optimal hedges to convert free bets and profit boosts into guaranteed cash.

---

## API Endpoint

**POST** `http://localhost:8002/promo-convert`

---

## Example 1: Free Bet (No Stake Return)

**Scenario:** DraftKings gives you a $50 free bet. The free bet does NOT return your stake if you win (most common).

You find:
- **Promo side:** Lakers ML at +200 (3.00 decimal) on DraftKings
- **Hedge side:** Celtics ML at -110 (1.91 decimal) on FanDuel

**Request:**
```bash
curl -X POST http://localhost:8002/promo-convert \
  -H "Content-Type: application/json" \
  -d '{
    "promo_type": "free_bet",
    "amount": 50.0,
    "odds_decimal": 3.00,
    "hedge_odds_decimal": 1.91,
    "free_bet_returns_stake": false
  }'
```

**Response:**
```json
{
  "promo_type": "free_bet",
  "promo_amount": 50.0,
  "promo_side_odds": 3.0,
  "hedge_side_odds": 1.91,
  "recommended_hedge_stake": 52.36,
  "guaranteed_profit": 47.64,
  "conversion_rate": 0.9528,
  "promo_side_payout": 100.0,
  "hedge_side_payout": 100.0,
  "notes": "Free bet $50.00 → $47.64 guaranteed (95.3% conversion)"
}
```

**What this means:**
- Bet your $50 free bet on Lakers ML (+200)
- Bet $52.36 of your own money on Celtics ML (-110)
- **If Lakers win:** You get $100 from DK, lose $52.36 on FD → **Profit: $47.64**
- **If Celtics win:** You lose $0 on DK (free bet), win $100 on FD, paid $52.36 → **Profit: $47.64**
- **Conversion rate:** 95.3% (you turned a $50 free bet into $47.64 cash)

---

## Example 2: Free Bet (Returns Stake)

**Scenario:** FanDuel gives you a $100 free bet that DOES return your stake if you win (rare but happens).

**Request:**
```bash
curl -X POST http://localhost:8002/promo-convert \
  -H "Content-Type: application/json" \
  -d '{
    "promo_type": "free_bet",
    "amount": 100.0,
    "odds_decimal": 2.50,
    "hedge_odds_decimal": 1.91,
    "free_bet_returns_stake": true
  }'
```

**Response:**
```json
{
  "promo_type": "free_bet",
  "promo_amount": 100.0,
  "promo_side_odds": 2.5,
  "hedge_side_odds": 1.91,
  "recommended_hedge_stake": 130.89,
  "guaranteed_profit": 119.11,
  "conversion_rate": 1.1911,
  "promo_side_payout": 250.0,
  "hedge_side_payout": 250.0,
  "notes": "Free bet $100.00 → $119.11 guaranteed (119.1% conversion)"
}
```

**What this means:**
- Bet your $100 free bet on Team A at 2.50
- Bet $130.89 on Team B at 1.91
- **If Team A wins:** You get $250 from promo side, lose $130.89 → **Profit: $119.11**
- **If Team B wins:** You lose $0, win $250, paid $130.89 → **Profit: $119.11**
- **Conversion rate:** 119% (better than 100% because stake is returned!)

---

## Example 3: Profit Boost

**Scenario:** DraftKings gives you a 50% profit boost on a $200 bet.

You find:
- **Promo side:** Patriots ML at +150 (2.50 decimal) on DraftKings with 50% boost
- **Hedge side:** Bills ML at -110 (1.91 decimal) on FanDuel

**Request:**
```bash
curl -X POST http://localhost:8002/promo-convert \
  -H "Content-Type: application/json" \
  -d '{
    "promo_type": "profit_boost",
    "amount": 200.0,
    "boost_percentage": 50.0,
    "odds_decimal": 2.50,
    "hedge_odds_decimal": 1.91
  }'
```

**Response:**
```json
{
  "promo_type": "profit_boost",
  "promo_amount": 200.0,
  "promo_side_odds": 2.5,
  "hedge_side_odds": 1.91,
  "recommended_hedge_stake": 261.78,
  "guaranteed_profit": 38.22,
  "conversion_rate": 0.1911,
  "promo_side_payout": 500.0,
  "hedge_side_payout": 500.0,
  "notes": "50% boost: effective odds 3.250, profit $38.22"
}
```

**What this means:**
- Bet $200 on Patriots ML at +150 with 50% boost (effective odds: 3.25)
- Bet $261.78 on Bills ML at -110
- **If Patriots win:** You get $500 from DK, lose $261.78 on FD → **Profit: $38.22**
- **If Bills win:** You lose $200 on DK, win $500 on FD, paid $261.78 → **Profit: $38.22**
- **Conversion rate:** 19.1% profit on your $200 stake

---

## Tips

1. **Higher odds = better conversion:** Free bets convert best at +200 to +400 odds (70-80% conversion)
2. **Close hedge odds:** The closer your hedge odds are to even money, the better
3. **Profit boosts:** Best used on longer odds (+200 or higher) to maximize the boost value
4. **Check both books:** Sometimes you can find better hedge odds on the opposite side

---

## Integration with ArbDesk

The promo converter is standalone — you call it manually when you get a promo. Future enhancement: auto-detect promos from DK/FD and suggest optimal hedges automatically.

