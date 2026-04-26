"""
07_usage_cdrs.py - Generate CDR (Call Detail Records) for calls, SMS, data and daily aggregates.

CDRs are the highest-volume tables in a telecom database.  Every phone call,
text message, and data session generates one record.  This script creates
realistic CDR data and then computes daily aggregates from it.

Tables populated:
  usage_cdr_calls, usage_cdr_sms, usage_cdr_data, usage_aggregates_daily

Design decisions:
  - Only active/suspended subscriptions generate CDRs.  Cancelled and pending
    subs are excluded because they wouldn’t have network access.
  - CDRs span the last 90 days (3 months) — enough for billing/analytics
    without producing an unwieldy volume.
  - 5% of calls are roaming (with roaming_partner_id set) because roaming
    is a small fraction of total traffic but important for billing accuracy.
  - 90% of events have a sector_id (10% are NULL to represent femtocell/WiFi
    calling where the network doesn’t log a sector).
  - fraud_score: 95% of calls score 0–10 (normal), 5% score 50–95 (suspicious).
    This lets fraud-detection queries find realistic anomalies.
  - Inbound calls/SMS have charge=0 (caller pays, not receiver).
  - Data sessions use RFC 1918 private IPs (10.x.x.x) because carrier-grade
    NAT means subscribers rarely get public IPs.
  - usage_aggregates_daily is derived FROM the CDR data (not generated
    independently) so that SUM(CDRs) always matches the aggregate row.
    This is critical for validation in 10_validate_and_run.py.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *
from collections import defaultdict


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    now = datetime.now()
    three_months_ago = now - timedelta(days=90)

    sub_meta_list = store.get("sub_meta")  # [(sub_id, meta_dict)]
    sector_ids = store.get("sector_ids")
    roaming_partner_ids = store.get("roaming_partner_ids")
    sim_msisdns = dict(store.get("sim_msisdns"))  # sim_id -> msisdn

    # Build list of active subscriptions with their MSISDNs.
    # We only generate CDRs for active/suspended subs because cancelled/pending
    # lines wouldn’t have network access to make calls or use data.
    active_subs = []
    for sub_id, meta in sub_meta_list:
        if meta["status"] in ("active", "suspended"):
            msisdn = sim_msisdns.get(meta["sim_id"], "10000000000")
            active_subs.append((sub_id, msisdn, meta))

    if not active_subs:
        print("  WARNING: No active subscriptions found, skipping CDR generation.")
        conn.commit()
        return

    # Aggregation accumulators keyed by (subscription_id, date_string).
    # As we generate each CDR, we simultaneously accumulate totals here.
    # After all CDRs are inserted, we flush this dict into usage_aggregates_daily.
    # This "generate then aggregate" approach guarantees consistency between
    # the detail and summary tables.
    daily_agg = defaultdict(lambda: {
        "call_seconds": 0, "sms_count": 0, "data_mb": 0.0, "charge": 0.0
    })

    # -----------------------------------------------------------------------
    # usage_cdr_calls
    # -----------------------------------------------------------------------
    # Each row represents one voice call.  Key fields:
    #   originating_msisdn: the caller’s phone number (from the subscription’s SIM).
    #   destination_msisdn: randomly generated (we don’t cross-reference our subs).
    #   duration_seconds: 10s–1hr (realistic call length distribution).
    #   direction: 70% outbound / 30% inbound (outbound is more common).
    #   charge_amount: only outbound calls are charged (per-minute rate).
    n_calls = get_scaled_count("usage_cdr_calls", scale)
    call_types = ["local", "long_distance", "international", "toll_free"]
    call_cols = ["subscription_id", "originating_msisdn", "destination_msisdn",
                 "sector_id", "roaming_partner_id", "event_start_time", "event_end_time",
                 "duration_seconds", "call_type", "direction", "charge_amount", "fraud_score"]

    # At 12M+ rows, holding all tuples in memory would use ~5 GB.  Instead we
    # generate and insert in CHUNK_SIZE batches (~100k rows, ~50 MB each).
    # The daily_agg accumulator is updated incrementally per-row across chunks.
    for chunk_start in range(0, n_calls, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE, n_calls)
        call_rows = []
        for _ in range(chunk_end - chunk_start):
            sub_id, orig_msisdn, meta = random.choice(active_subs)
            dest_msisdn = "1" + "".join([str(random.randint(0, 9)) for _ in range(10)])
            sect_id = random.choice(sector_ids) if random.random() < 0.9 else None
            roam_id = random.choice(roaming_partner_ids) if random.random() < 0.05 else None
            start = random_timestamp(three_months_ago, now)
            duration = random.randint(10, 3600)
            end = start + timedelta(seconds=duration)
            ctype = weighted_choice(call_types, [60, 20, 15, 5])
            direction = weighted_choice(["outbound", "inbound"], [70, 30])
            charge = round(duration / 60 * random.uniform(0.01, 0.10), 4) if direction == "outbound" else 0.0
            fraud = round(random.uniform(0, 10), 2) if random.random() < 0.95 else round(random.uniform(50, 95), 2)

            call_rows.append((
                sub_id, orig_msisdn, dest_msisdn, sect_id, roam_id,
                start, end, duration, ctype, direction, charge, fraud,
            ))

            day_key = (sub_id, start.date().isoformat())
            daily_agg[day_key]["call_seconds"] += duration
            daily_agg[day_key]["charge"] += charge

        insert_batch(cur, "usage_cdr_calls", call_cols, call_rows)
        if chunk_end % 1_000_000 == 0 or chunk_end == n_calls:
            print(f"  usage_cdr_calls: {chunk_end:,} / {n_calls:,}")

    print_table_count(cur, "usage_cdr_calls")

    # -----------------------------------------------------------------------
    # usage_cdr_sms
    # -----------------------------------------------------------------------
    # SMS CDRs are simpler than call CDRs: no duration, just an event_time.
    # Types: standard (70%), premium (10%, e.g. voting/shortcodes), MMS (20%).
    # Outbound/inbound split is 60/40 (more even than voice).
    n_sms = get_scaled_count("usage_cdr_sms", scale)
    sms_types = ["standard", "premium", "mms"]
    sms_cols = ["subscription_id", "source_msisdn", "destination_msisdn",
                "sector_id", "roaming_partner_id", "event_time",
                "sms_type", "direction", "charge_amount"]

    for chunk_start in range(0, n_sms, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE, n_sms)
        sms_rows = []
        for _ in range(chunk_end - chunk_start):
            sub_id, src_msisdn, meta = random.choice(active_subs)
            dest_msisdn = "1" + "".join([str(random.randint(0, 9)) for _ in range(10)])
            sect_id = random.choice(sector_ids) if random.random() < 0.9 else None
            roam_id = random.choice(roaming_partner_ids) if random.random() < 0.05 else None
            etime = random_timestamp(three_months_ago, now)
            stype = weighted_choice(sms_types, [70, 10, 20])
            direction = weighted_choice(["outbound", "inbound"], [60, 40])
            charge = round(random.uniform(0, 0.50), 4) if direction == "outbound" else 0.0

            sms_rows.append((
                sub_id, src_msisdn, dest_msisdn, sect_id, roam_id,
                etime, stype, direction, charge,
            ))

            day_key = (sub_id, etime.date().isoformat())
            daily_agg[day_key]["sms_count"] += 1
            daily_agg[day_key]["charge"] += charge

        insert_batch(cur, "usage_cdr_sms", sms_cols, sms_rows)
        if chunk_end % 1_000_000 == 0 or chunk_end == n_sms:
            print(f"  usage_cdr_sms: {chunk_end:,} / {n_sms:,}")

    print_table_count(cur, "usage_cdr_sms")

    # -----------------------------------------------------------------------
    # usage_cdr_data
    # -----------------------------------------------------------------------
    # Data CDRs represent internet sessions.  Each has a start/end time,
    # bytes consumed (as MB), an APN (Access Point Name that routes traffic),
    # and a private IP assigned to the subscriber for the session.
    # Session durations: 1 minute to 4 hours (60–14400 seconds).
    # Data volumes: 0.5 MB (quick check) to 2000 MB (streaming session).
    n_data = get_scaled_count("usage_cdr_data", scale)
    apns = ["internet", "mms", "corporate", "iot.m2m", "wap.cust"]
    data_cols = ["subscription_id", "sector_id", "roaming_partner_id",
                 "session_start", "session_end", "data_used_mb",
                 "apn", "ip_address", "charge_amount"]

    for chunk_start in range(0, n_data, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE, n_data)
        data_rows = []
        for _ in range(chunk_end - chunk_start):
            sub_id, _, meta = random.choice(active_subs)
            sect_id = random.choice(sector_ids) if random.random() < 0.9 else None
            roam_id = random.choice(roaming_partner_ids) if random.random() < 0.05 else None
            sess_start = random_timestamp(three_months_ago, now)
            sess_dur = random.randint(60, 14400)  # 1min to 4hrs
            sess_end = sess_start + timedelta(seconds=sess_dur)
            data_mb = round(random.uniform(0.5, 2000), 4)
            apn = random.choice(apns)
            ip = f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
            charge = round(data_mb * random.uniform(0.001, 0.02), 4)

            data_rows.append((
                sub_id, sect_id, roam_id, sess_start, sess_end,
                data_mb, apn, ip, charge,
            ))

            day_key = (sub_id, sess_start.date().isoformat())
            daily_agg[day_key]["data_mb"] += data_mb
            daily_agg[day_key]["charge"] += charge

        insert_batch(cur, "usage_cdr_data", data_cols, data_rows)
        if chunk_end % 1_000_000 == 0 or chunk_end == n_data:
            print(f"  usage_cdr_data: {chunk_end:,} / {n_data:,}")

    print_table_count(cur, "usage_cdr_data")

    # -----------------------------------------------------------------------
    # usage_aggregates_daily — computed from CDR data above
    # -----------------------------------------------------------------------
    # Instead of generating random aggregates, we derive them from the actual
    # CDR rows above.  This guarantees that SELECT SUM() over the CDR tables
    # matches the aggregate table — a property validated in 10_validate_and_run.
    # The UNIQUE(subscription_id, usage_date) constraint is naturally satisfied
    # because our dict keys are (sub_id, date) tuples.
    # With 200k subs × 90 days the dict can hold millions of entries, so we
    # convert and insert in chunks to keep memory bounded.
    agg_cols = ["subscription_id", "usage_date", "total_call_seconds",
                "total_sms_count", "total_data_mb", "total_charge_amount"]
    agg_rows = []
    for (sub_id, date_str), vals in daily_agg.items():
        usage_date = date.fromisoformat(date_str)
        agg_rows.append((
            sub_id, usage_date,
            vals["call_seconds"],
            vals["sms_count"],
            round(vals["data_mb"], 4),
            round(vals["charge"], 4),
        ))
        if len(agg_rows) >= CHUNK_SIZE:
            insert_batch(cur, "usage_aggregates_daily", agg_cols, agg_rows)
            agg_rows = []

    if agg_rows:
        insert_batch(cur, "usage_aggregates_daily", agg_cols, agg_rows)

    # Free the large dict now that we've flushed it to the DB.
    del daily_agg
    print_table_count(cur, "usage_aggregates_daily")

    conn.commit()
    print(">> 07_usage_cdrs done.\n")
