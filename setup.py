"""
setup.py - Shared setup, utilities, configuration, and data store for synthetic data generation.

This is the foundation module imported by every generator script (01–09). It provides:
  1. Database connection configuration (via environment variables for security).
  2. Centralized row-count targets, scalable via a multiplier so the entire dataset
     can be sized up or down from a single flag (--scale).
  3. Batch-insert helpers that use psycopg2's execute_batch for performance, plus
     a row-at-a-time variant that captures RETURNING serial IDs.
  4. Deterministic random-data helpers (dates, timestamps, weighted choices) seeded
     globally so every run is reproducible with the same --seed value.
  5. A DataStore object that acts as an in-memory registry of generated IDs/metadata
     so downstream scripts can reference parent-table rows without re-querying the DB.

Design decisions:
  - Environment variables for DB creds: avoids hard-coding secrets; easy to override
    in CI, Docker, or local dev with sensible defaults for a fresh Postgres install.
  - Faker + random (not secrets): we need speed and reproducibility, not
    cryptographic randomness.  Both are seeded in lockstep.
  - DataStore is a plain dict wrapper rather than a global: it's explicitly
    passed between scripts so data flow is traceable and testable.
"""

import os
import random
from datetime import datetime, date, timedelta
from decimal import Decimal

import psycopg2
import psycopg2.extras
from faker import Faker

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Database connection settings are read from environment variables so that
# credentials never need to be committed to source control.  The defaults
# (localhost / postgres / postgres) match a stock local Postgres install.

DB_CONFIG = {
    "host": os.getenv("PG_HOST", "localhost"),
    "port": int(os.getenv("PG_PORT", 5432)),
    "dbname": os.getenv("PG_DB", "telecom"),
    "user": os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", "postgres"),
}

# DEFAULT_SCALE = 1.0 means "generate the base row counts listed below".
# Pass --scale 0.5 for a quick smoke-test dataset, or --scale 5.0 for
# a stress-test.  Every get_scaled_count() call multiplies by this.
DEFAULT_SCALE = 1.0

# DEFAULT_SEED ensures reproducibility: same seed → identical dataset.
# Useful for regression testing or sharing a deterministic fixture.
DEFAULT_SEED = 42

# Base row counts for every table at scale=1.0.
# Targets: 4 tables >10M rows (CDRs + invoice_items), 4 tables at 1M rows
# (invoices, payments, subscription_status_history, crm_interactions).
# Supporting tables are sized proportionally to satisfy FK dependencies:
#   - 100k customers/accounts feed 1M+ invoices over 12 billing periods.
#   - 200k subscriptions spread across 100k accounts (avg 2/account) so that
#     each invoice averages ~10 line items → 10M+ invoice_items.
#   - 240k SIMs > 200k subs to leave inventory headroom.
#   - 170k active subs × ~70 calls/90 days = 12M CDR calls; similar for SMS/data.
ROW_COUNTS = {
    # Lookup tables — small, hand-crafted reference data (unchanged)
    "billing_cycles": 5,
    "departments": 9,
    "employee_roles": 11,
    "services": 6,
    "overage_policies": 5,
    "network_technologies": 5,
    "payment_methods": 6,
    "addons": 12,
    "discounts_promotions": 7,
    # Core entities — scaled to support millions of downstream rows
    "customers": 100_000,
    "customer_addresses": 160_000,
    "employees": 500,
    "products": 12,
    "plans": 25,
    "accounts": 100_000,
    "contracts": 120_000,
    "sim_cards": 240_000,
    "devices": 100_000,
    "subscriptions": 200_000,
    "subscription_addons": 100_000,
    "subscription_status_history": 1_000_000,  # *** 1M target ***
    "device_assignments": 120_000,
    "sim_card_subscription_history": 240_000,
    # Network infrastructure
    "network_sites": 200,
    "cell_towers": 500,
    "tower_sectors": 1_500,
    "coverage_zones": 1_500,
    "roaming_partners": 18,
    "network_alarms": 5_000,
    "outages": 500,
    "employee_assignments": 5_000,
    # CDRs — >10M targets (highest-volume transactional tables)
    "usage_cdr_calls": 12_000_000,              # *** >10M target ***
    "usage_cdr_sms": 10_000_000,                # *** >10M target ***
    "usage_cdr_data": 10_000_000,               # *** >10M target ***
    # Billing
    "invoices": 1_000_000,                      # *** 1M target ***
    "invoice_items": 10_000_000,                # *** >10M target ***
    "payments": 1_000_000,                      # *** 1M target ***
    "billing_runs": 12,
    # CRM
    "crm_tickets": 200_000,
    "crm_interactions": 1_000_000,              # *** 1M target ***
    "ticket_status_history": 600_000,
}

# Chunk size for generating/inserting large tables in memory-bounded batches.
# 100k rows ≈ 50–100 MB in Python memory; safe for most machines while still
# giving good insert throughput (fewer round-trips than tiny batches).
CHUNK_SIZE = 100_000


def get_scaled_count(table_name: str, scale: float = DEFAULT_SCALE) -> int:
    """Return the target row count for `table_name`, multiplied by `scale`.

    Always returns at least 1 so that every table is populated even at
    fractional scales.  Falls back to 100 for unknown table names.
    """
    base = ROW_COUNTS.get(table_name, 100)
    return max(1, int(base * scale))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    """Open a new psycopg2 connection using the global DB_CONFIG.

    The caller is responsible for committing and closing.
    """
    return psycopg2.connect(**DB_CONFIG)


def insert_batch(cursor, table: str, columns: list[str], rows: list[tuple]):
    """Bulk-insert rows into `table` using psycopg2's execute_batch.

    execute_batch groups rows into pages of 500 and sends them in a single
    round-trip per page, which is dramatically faster than row-at-a-time
    inserts for large tables (CDRs, invoice items, etc.).

    This variant does NOT return generated IDs — use insert_batch_returning
    when you need the serial primary keys for downstream FK references.
    """
    if not rows:
        return
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
    psycopg2.extras.execute_batch(cursor, query, rows, page_size=500)


def insert_batch_returning(cursor, table: str, columns: list[str], rows: list[tuple], returning_col: str = ""):
    """Bulk-insert rows and capture RETURNING values using execute_values.

    Uses psycopg2's execute_values with fetch=True, which sends multi-row
    VALUES clauses (up to page_size rows each) and collects RETURNING results.
    This is ~50-100x faster than row-at-a-time inserts for large tables
    (100k customers in ~2s vs ~100s), making it viable for 100k+ parent tables.

    If returning_col is empty, falls back to execute_batch (no results needed).
    """
    if not rows:
        return []
    cols = ", ".join(columns)
    if returning_col:
        query = f"INSERT INTO {table} ({cols}) VALUES %s RETURNING {returning_col}"
        results = psycopg2.extras.execute_values(
            cursor, query, rows, page_size=1000, fetch=True
        )
        return [r[0] for r in results]
    else:
        placeholders = ", ".join(["%s"] * len(columns))
        query = f"INSERT INTO {table} ({cols}) VALUES ({placeholders})"
        psycopg2.extras.execute_batch(cursor, query, rows, page_size=500)
        return []


# ---------------------------------------------------------------------------
# Random-data helpers
# ---------------------------------------------------------------------------
# Both Faker and Python's random module are seeded in lockstep so that the
# entire dataset is deterministic.  This means running with the same --seed
# always produces byte-identical output, which is critical for:
#   - Reproducible test fixtures.
#   - Debugging: re-run with the same seed to hit the same edge case.
#   - Sharing: two developers get the same dataset without a DB dump.

fake = Faker()
Faker.seed(DEFAULT_SEED)
random.seed(DEFAULT_SEED)


def set_seed(seed: int):
    """Reset both Faker and random seeds to `seed` for full reproducibility.

    A new Faker instance is created because Faker's internal state from
    previous .unique calls cannot be reset by re-seeding alone.
    """
    Faker.seed(seed)
    random.seed(seed)
    global fake
    fake = Faker()
    Faker.seed(seed)


def random_date(start: date, end: date) -> date:
    """Return a random date uniformly distributed between `start` and `end` (inclusive).

    If end <= start, returns start to avoid ValueError from randint.
    """
    delta = (end - start).days
    if delta <= 0:
        return start
    return start + timedelta(days=random.randint(0, delta))


def random_timestamp(start: datetime, end: datetime) -> datetime:
    """Return a random timestamp at second resolution between `start` and `end`.

    Second resolution is sufficient for synthetic data and avoids microsecond
    noise that would make debugging harder.
    """
    delta = (end - start).total_seconds()
    if delta <= 0:
        return start
    return start + timedelta(seconds=random.randint(0, int(delta)))


def weighted_choice(options: list, weights: list):
    """Pick one item from `options` with the given probability `weights`.

    Weights are relative (don't need to sum to 1).  Used throughout to
    create realistic distributions — e.g. 85% active / 10% suspended / 5%
    closed — rather than uniform randomness.
    """
    return random.choices(options, weights=weights, k=1)[0]


def unique_string(prefix: str, length: int, existing: set) -> str:
    """Generate a string like 'ACC-12345678' that hasn't been used before.

    `existing` is a set that the caller maintains across the loop.  The
    retry-until-unique pattern is safe because the numeric space (10^length)
    is orders of magnitude larger than our row counts at any realistic scale.
    """
    while True:
        val = f"{prefix}{random.randint(10 ** (length - 1), 10 ** length - 1)}"
        if val not in existing:
            existing.add(val)
            return val


def unique_digits(length: int, existing: set) -> str:
    """Generate a zero-padded digit string (e.g. ICCID, IMEI, IMSI) that is unique.

    Leading zeros are preserved because telecom identifiers like ICCID (20 digits)
    and IMSI (15 digits) are fixed-width strings, not integers.
    """
    while True:
        val = "".join([str(random.randint(0, 9)) for _ in range(length)])
        if val not in existing:
            existing.add(val)
            return val


# ---------------------------------------------------------------------------
# Shared data store — accumulates IDs / records across scripts
# ---------------------------------------------------------------------------
# Why a DataStore instead of re-querying the DB for IDs?
#   1. Speed: avoids a SELECT on every parent table before each child insert.
#   2. Simplicity: downstream scripts just do store.get("customer_ids") instead
#      of crafting queries.
#   3. Traceability: you can inspect the store at any point to see exactly what
#      IDs were generated in each step.
# The store is passed explicitly (not global) so data flow is visible.

class DataStore:
    """In-memory registry of generated IDs and metadata, passed between scripts.

    Each script writes its generated IDs (e.g. customer_ids, plan_ids) into
    the store after inserting rows.  Later scripts read those IDs to satisfy
    foreign-key relationships without re-querying the database.
    """

    def __init__(self):
        self.data: dict[str, list] = {}

    def set(self, key: str, values: list):
        self.data[key] = values

    def get(self, key: str) -> list:
        return self.data.get(key, [])

    def get_random(self, key: str):
        items = self.data.get(key, [])
        return random.choice(items) if items else None

    def get_random_or_none(self, key: str, none_pct: float = 0.2):
        """Return a random item, or None with probability `none_pct`.

        Useful for nullable FK columns where some percentage of rows
        should have the relationship empty (e.g. 20% of tickets have
        no assigned employee).
        """
        if random.random() < none_pct:
            return None
        return self.get_random(key)


def print_table_count(cursor, table: str):
    """Query and print the current row count for `table`.  Returns the count.

    Called after each batch insert so the console log shows incremental progress.
    """
    cursor.execute(f"SELECT COUNT(*) FROM {table}")
    count = cursor.fetchone()[0]
    print(f"  {table}: {count} rows")
    return count
