"""Generate a deterministic synthetic marketing dataset and load it into DuckDB.

Tables land in the `raw` schema of data/warehouse.duckdb:
  raw.channels   - marketing channels (paid / owned / organic)
  raw.campaigns  - paid + newsletter campaigns
  raw.ad_spend   - daily impressions, clicks, spend per paid campaign
  raw.sessions   - website sessions by anonymous visitors
  raw.customers  - created on a visitor's first order
  raw.orders     - orders with status (completed / refunded / cancelled)

Same seed -> same data, so eval answers stay stable across runs.

Usage: uv run python scripts/generate_data.py [--seed 42]
"""

from __future__ import annotations

import argparse
import csv
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CSV_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "warehouse.duckdb"

START = date(2026, 1, 1)
END = date(2026, 6, 30)

# channel_id, name, type, conversion rate per session
CHANNELS = [
    (1, "google_search", "paid", 0.034),
    (2, "meta_ads", "paid", 0.018),
    (3, "tiktok_ads", "paid", 0.011),
    (4, "affiliate", "paid", 0.041),
    (5, "newsletter", "owned", 0.052),
    (6, "organic_search", "organic", 0.027),
    (7, "direct", "organic", 0.045),
]
CONV_RATE = {c[0]: c[3] for c in CHANNELS}

# campaign_id, name, channel_id, objective, start, end, daily budget EUR, CPC EUR
CAMPAIGNS = [
    (101, "gs_brand_always_on", 1, "conversion", date(2026, 1, 1), END, 120, 0.45),
    (102, "gs_generic_running_shoes", 1, "conversion", date(2026, 1, 1), END, 260, 1.10),
    (103, "gs_spring_sale", 1, "conversion", date(2026, 3, 1), date(2026, 3, 31), 400, 0.95),
    (201, "meta_prospecting_broad", 2, "awareness", date(2026, 1, 1), END, 300, 0.70),
    (202, "meta_retargeting_cart", 2, "conversion", date(2026, 1, 15), END, 90, 0.55),
    (203, "meta_spring_sale", 2, "conversion", date(2026, 3, 1), date(2026, 3, 31), 350, 0.65),
    (301, "tiktok_creators_q1", 3, "awareness", date(2026, 1, 10), date(2026, 3, 31), 200, 0.35),
    (302, "tiktok_creators_q2", 3, "awareness", date(2026, 4, 1), END, 260, 0.38),
    (401, "affiliate_partners", 4, "conversion", date(2026, 1, 1), END, 80, 0.90),
    (501, "nl_weekly_digest", 5, "retention", date(2026, 1, 1), END, 0, 0),
    (502, "nl_spring_sale", 5, "conversion", date(2026, 3, 1), date(2026, 3, 31), 0, 0),
]

COUNTRIES = [("DE", 0.45, 78), ("AT", 0.12, 74), ("CH", 0.10, 105), ("NL", 0.18, 69), ("FR", 0.15, 72)]
DEVICES = [("mobile", 0.62), ("desktop", 0.31), ("tablet", 0.07)]

# organic/owned daily session baselines (newsletter only on its send days)
ORGANIC_BASE = {6: 140, 7: 95}


def weekday_factor(d: date) -> float:
    return [1.0, 1.02, 1.0, 0.98, 0.95, 0.82, 0.88][d.weekday()]


def growth_factor(d: date) -> float:
    return 1.0 + 0.15 * ((d - START).days / (END - START).days)


def pick(rng: random.Random, weighted: list[tuple]) -> tuple:
    return rng.choices(weighted, weights=[w[1] for w in weighted])[0]


def daterange(a: date, b: date):
    for i in range((b - a).days + 1):
        yield a + timedelta(days=i)


def generate(seed: int) -> dict[str, tuple[list[str], list[tuple]]]:
    rng = random.Random(seed)

    channels = [(c[0], c[1], c[2]) for c in CHANNELS]
    campaigns = [(c[0], c[1], c[2], c[3], c[4], c[5]) for c in CAMPAIGNS]

    ad_spend: list[tuple] = []
    sessions: list[tuple] = []
    customers: list[tuple] = []
    orders: list[tuple] = []

    visitor_seq = 0
    session_seq = 0
    order_seq = 0
    # visitor_id -> (country, customer_id | None)
    visitors: dict[int, list] = {}
    returning_pool: list[int] = []

    def new_or_returning_visitor() -> int:
        nonlocal visitor_seq
        if returning_pool and rng.random() < 0.28:
            return rng.choice(returning_pool)
        visitor_seq += 1
        visitors[visitor_seq] = [pick(rng, COUNTRIES)[0], None]
        returning_pool.append(visitor_seq)
        return visitor_seq

    def emit_session(d: date, channel_id: int, campaign_id: int | None) -> None:
        nonlocal session_seq, order_seq
        session_seq += 1
        vid = new_or_returning_visitor()
        country = visitors[vid][0]
        device = pick(rng, DEVICES)[0]
        started = datetime(d.year, d.month, d.day) + timedelta(seconds=rng.randint(0, 86399))
        sessions.append((session_seq, vid, started, channel_id, campaign_id, device, country))

        conv = CONV_RATE[channel_id] * (0.7 if device == "mobile" else 1.15)
        if rng.random() >= conv:
            return
        # first order creates the customer
        if visitors[vid][1] is None:
            cust_id = len(customers) + 1
            visitors[vid][1] = cust_id
            customers.append((cust_id, vid, started + timedelta(minutes=rng.randint(2, 40)), country))
        cust_id = visitors[vid][1]
        aov = next(c[2] for c in COUNTRIES if c[0] == country)
        revenue = round(max(9.0, rng.gauss(aov, aov * 0.35)), 2)
        status = rng.choices(["completed", "refunded", "cancelled"], weights=[0.88, 0.07, 0.05])[0]
        order_seq += 1
        ordered_at = started + timedelta(minutes=rng.randint(3, 45))
        orders.append((order_seq, cust_id, vid, session_seq, ordered_at, revenue, status))

    for d in daterange(START, END):
        seasonal = weekday_factor(d) * growth_factor(d)

        # paid campaigns: spend -> clicks -> sessions (~88% of clicks land)
        for cid, _name, ch, _obj, c_start, c_end, budget, cpc in CAMPAIGNS:
            if not (c_start <= d <= c_end):
                continue
            if budget == 0:  # newsletter: sends on Tuesdays (+ every 3rd day during its sale)
                is_send = d.weekday() == 1 if cid == 501 else (d - c_start).days % 3 == 0
                if is_send:
                    for _ in range(int(rng.gauss(260 if cid == 501 else 340, 30))):
                        emit_session(d, ch, cid)
                continue
            spend = round(max(0.0, rng.gauss(budget, budget * 0.12)) * seasonal, 2)
            clicks = int(spend / max(0.05, rng.gauss(cpc, cpc * 0.1)))
            ctr = {1: 0.06, 2: 0.012, 3: 0.009, 4: 0.02}[ch]
            impressions = int(clicks / max(0.002, rng.gauss(ctr, ctr * 0.15)))
            ad_spend.append((d, cid, impressions, clicks, spend))
            for _ in range(int(clicks * 0.88)):
                emit_session(d, ch, cid)

        # organic / direct
        for ch, base in ORGANIC_BASE.items():
            for _ in range(int(rng.gauss(base * seasonal, base * 0.1))):
                emit_session(d, ch, None)

    return {
        "channels": (["channel_id", "channel_name", "channel_type"], channels),
        "campaigns": (
            ["campaign_id", "campaign_name", "channel_id", "objective", "start_date", "end_date"],
            campaigns,
        ),
        "ad_spend": (["spend_date", "campaign_id", "impressions", "clicks", "spend_eur"], ad_spend),
        "sessions": (
            ["session_id", "visitor_id", "started_at", "channel_id", "campaign_id", "device", "country"],
            sessions,
        ),
        "customers": (["customer_id", "visitor_id", "created_at", "country"], customers),
        "orders": (
            ["order_id", "customer_id", "visitor_id", "session_id", "ordered_at", "revenue_eur", "status"],
            orders,
        ),
    }


def write_and_load(tables: dict[str, tuple[list[str], list[tuple]]]) -> None:
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = duckdb.connect(str(DB_PATH))
    con.execute("create schema raw")
    for name, (header, rows) in tables.items():
        path = CSV_DIR / f"{name}.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        con.execute(f"create table raw.{name} as select * from read_csv_auto(?, header=true)", [str(path)])
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    tables = generate(args.seed)
    write_and_load(tables)

    con = duckdb.connect(str(DB_PATH), read_only=True)
    print(f"Loaded {DB_PATH.relative_to(ROOT)} (seed={args.seed})")
    for name in tables:
        n = con.execute(f"select count(*) from raw.{name}").fetchone()[0]
        print(f"  raw.{name:<10} {n:>7,} rows")
    con.close()


if __name__ == "__main__":
    main()
