"""
04_subscriptions.py - Generate subscriptions and related subscription data.

This is the central "activation" script that ties together accounts, plans,
SIMs, contracts, addons, and devices into active subscriber lines.  It also
creates the audit/history tables that track how subscriptions change over time.

Tables populated:
  subscriptions, subscription_addons, subscription_status_history,
  device_assignments, sim_card_subscription_history

Design decisions:
  - SIM uniqueness: the DDL enforces UNIQUE(sim_id) on subscriptions, meaning
    each SIM can only be assigned to one active subscription.  We shuffle the
    SIM ID pool and index into it sequentially to guarantee this.
  - n_subs is capped at len(sim_ids) because we can't have more subscriptions
    than available SIMs.
  - billing_start_date is set to the 1st of the month following activation,
    which is a common telecom billing convention (partial first month is free).
  - Status history uses predefined progressions per final status so that the
    audit trail is logically consistent (e.g. an active sub went NULL→pending
    →active, a cancelled sub went NULL→pending→active→cancelled).
  - Device assignments: ~15% have ended (returned/lost) to create realistic
    device lifecycle data.
  - SIM swap history: ~15% of subs have two entries (old SIM ended, new SIM
    started) to simulate real-world SIM replacement scenarios.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    account_ids = store.get("account_ids")
    plan_ids = store.get("plan_ids")
    sim_ids = store.get("sim_ids")
    contract_ids = store.get("contract_ids")
    contract_meta = store.get("contract_meta")  # [(contract_id, (cust_id, acct_id, start, end))]
    employee_ids = store.get("employee_ids")
    addon_ids = store.get("addon_ids")
    addon_prices = store.get("addon_prices")

    # -----------------------------------------------------------------------
    # subscriptions
    # -----------------------------------------------------------------------
    # The subscription is the core revenue-bearing entity: it links an account
    # to a plan and a SIM card, creating an active phone line.
    # We cap n_subs at len(sim_ids) to respect the UNIQUE constraint on sim_id.
    n_subs = get_scaled_count("subscriptions", scale)
    n_subs = min(n_subs, len(sim_ids))  # can't exceed available SIMs

    used_sub_nums = set()
    # Shuffle SIMs and index sequentially to guarantee no duplicates.
    available_sims = list(sim_ids)
    random.shuffle(available_sims)

    sub_rows = []
    sub_meta = []  # (account_id, plan_id, sim_id, activation_date, status)

    for i in range(n_subs):
        aid = random.choice(account_ids)
        pid = random.choice(plan_ids)
        sid = available_sims[i]  # unique SIM per subscription

        # Optionally link to a contract (70% chance).
        # Not all subscriptions have contracts (e.g. prepaid lines).
        cid = None
        if contract_meta and random.random() < 0.7:
            cm = random.choice(contract_meta)
            cid = cm[0]

        sub_num = unique_string("SUB-", 8, used_sub_nums)
        act_date = random_date(today - timedelta(days=1000), today - timedelta(days=30))
        status = weighted_choice(["active", "suspended", "cancelled", "pending"], [75, 10, 10, 5])
        end_dt = None
        if status == "cancelled":
            end_dt = random_date(act_date + timedelta(days=30), today)
        billing_start = act_date.replace(day=1) + timedelta(days=31)
        billing_start = billing_start.replace(day=1)  # 1st of next month

        sub_rows.append((aid, pid, sid, cid, sub_num, act_date, end_dt, status, billing_start))
        sub_meta.append({
            "account_id": aid, "plan_id": pid, "sim_id": sid,
            "activation_date": act_date, "end_date": end_dt, "status": status,
        })

    sub_ids = insert_batch_returning(
        cur, "subscriptions",
        ["account_id", "plan_id", "sim_id", "contract_id", "subscription_number",
         "activation_date", "end_date", "status", "billing_start_date"],
        sub_rows, "subscription_id",
    )
    store.set("subscription_ids", sub_ids)
    store.set("sub_meta", list(zip(sub_ids, sub_meta)))
    print_table_count(cur, "subscriptions")

    # -----------------------------------------------------------------------
    # subscription_addons
    # -----------------------------------------------------------------------
    # Each addon activation records the price at the moment of activation
    # (price_at_activation) because addon prices may change over time.
    # We apply ±5% jitter to simulate promotional pricing variations.
    # ~20% of addons are deactivated to create churn data.
    n_addons = get_scaled_count("subscription_addons", scale)
    sa_rows = []
    for _ in range(n_addons):
        idx = random.randint(0, len(sub_ids) - 1)
        sub_id = sub_ids[idx]
        meta = sub_meta[idx]
        addon_idx = random.randint(0, len(addon_ids) - 1)
        aid = addon_ids[addon_idx]
        price = float(addon_prices[addon_idx])

        act_d = random_date(meta["activation_date"] + timedelta(days=1), today)
        deact = None
        status = "active"
        if random.random() < 0.20:
            deact = random_date(act_d + timedelta(days=7), today)
            status = "deactivated"

        price_at = round(price * random.uniform(0.95, 1.05), 2)
        sa_rows.append((sub_id, aid, act_d, deact, status, price_at))

    insert_batch(cur, "subscription_addons",
                 ["subscription_id", "addon_id", "activation_date",
                  "deactivation_date", "status", "price_at_activation"],
                 sa_rows)
    print_table_count(cur, "subscription_addons")

    # -----------------------------------------------------------------------
    # subscription_status_history
    # -----------------------------------------------------------------------
    # Every subscription gets an audit trail of status transitions.
    # Base progressions define the minimum path per final status.
    # To reach the 1M target with 200k subs, we extend active/suspended subs
    # with additional lifecycle events (suspend/reactivate cycles) that are
    # common in real telecoms (non-payment suspensions, voluntary holds, etc.).
    # The target row count controls how many extra events are generated.
    target_ssh = get_scaled_count("subscription_status_history", scale)

    ssh_rows = []
    status_progressions = {
        "active": [
            (None, "pending"), ("pending", "active"),
        ],
        "suspended": [
            (None, "pending"), ("pending", "active"), ("active", "suspended"),
        ],
        "cancelled": [
            (None, "pending"), ("pending", "active"), ("active", "cancelled"),
        ],
        "pending": [
            (None, "pending"),
        ],
    }

    reasons = ["System activation", "Customer request", "Non-payment",
               "Customer cancellation", "Contract expiry", "Plan upgrade",
               "Suspension lifted", "Payment received", "Account review",
               "Voluntary hold", "Service restored"]

    for i, sub_id in enumerate(sub_ids):
        meta = sub_meta[i]
        progression = list(status_progressions.get(meta["status"], [(None, "active")]))

        # Extend active/suspended subs with extra suspend/reactivate cycles
        # to fill the gap between base transitions and the 1M target.
        if meta["status"] in ("active", "suspended"):
            extra_cycles = random.randint(0, 4)
            for _ in range(extra_cycles):
                progression.append(("active", "suspended"))
                progression.append(("suspended", "active"))
            if meta["status"] == "suspended":
                progression.append(("active", "suspended"))

        base_time = datetime.combine(meta["activation_date"], datetime.min.time())
        for j, (old_s, new_s) in enumerate(progression):
            changed_at = base_time + timedelta(hours=j * random.randint(1, 48))
            emp_id = random.choice(employee_ids)
            reason = random.choice(reasons)
            ssh_rows.append((sub_id, old_s, new_s, changed_at, emp_id, reason))

    # If we still haven't reached the target, add more random lifecycle events
    while len(ssh_rows) < target_ssh:
        idx = random.randint(0, len(sub_ids) - 1)
        sub_id = sub_ids[idx]
        meta = sub_meta[idx]
        base_time = datetime.combine(meta["activation_date"], datetime.min.time())
        t = random_timestamp(base_time, now)
        emp_id = random.choice(employee_ids)
        old_s = random.choice(["active", "suspended"])
        new_s = "suspended" if old_s == "active" else "active"
        ssh_rows.append((sub_id, old_s, new_s, t, emp_id, random.choice(reasons)))

    # Insert in chunks to bound memory
    ssh_cols = ["subscription_id", "old_status", "new_status",
                "changed_at", "changed_by_employee_id", "reason"]
    for chunk_start in range(0, len(ssh_rows), CHUNK_SIZE):
        chunk = ssh_rows[chunk_start:chunk_start + CHUNK_SIZE]
        insert_batch(cur, "subscription_status_history", ssh_cols, chunk)

    print_table_count(cur, "subscription_status_history")

    # -----------------------------------------------------------------------
    # device_assignments
    # -----------------------------------------------------------------------
    # Links a physical device to a subscription ("this phone is used on this line").
    # We shuffle device IDs and cap at the number of available devices to avoid
    # assigning the same device twice.  ~15% of assignments have ended
    # (returned or lost) to create device lifecycle data.
    device_ids = store.get("device_ids")
    n_da = min(get_scaled_count("device_assignments", scale), len(device_ids))
    shuffled_devices = list(device_ids)
    random.shuffle(shuffled_devices)

    da_rows = []
    for i in range(n_da):
        dev_id = shuffled_devices[i]
        idx = random.randint(0, len(sub_ids) - 1)
        sub_id = sub_ids[idx]
        meta = sub_meta[idx]
        assigned_from = random_timestamp(
            datetime.combine(meta["activation_date"], datetime.min.time()),
            now - timedelta(days=1),
        )
        assigned_to = None
        a_status = "active"
        if random.random() < 0.15:
            assigned_to = random_timestamp(assigned_from + timedelta(days=1), now)
            a_status = random.choice(["returned", "lost"])
        notes = random.choice([None, "Standard assignment", "Replacement device", "Upgrade"])
        da_rows.append((dev_id, sub_id, assigned_from, assigned_to, a_status, notes))

    insert_batch(cur, "device_assignments",
                 ["device_id", "subscription_id", "assigned_from", "assigned_to",
                  "assignment_status", "notes"],
                 da_rows)
    print_table_count(cur, "device_assignments")

    # -----------------------------------------------------------------------
    # sim_card_subscription_history
    # -----------------------------------------------------------------------
    # Tracks which SIM was active on which subscription and when.
    # Most subscriptions have a single entry (one SIM for the lifetime).
    # ~15% have two entries simulating a SIM swap: the old SIM’s end_date
    # is set, and a new entry (sim_id=None, representing a replacement SIM
    # from outside our generated pool) starts at that timestamp.
    sim_hist_rows = []
    for i, sub_id in enumerate(sub_ids):
        meta = sub_meta[i]
        start = datetime.combine(meta["activation_date"], datetime.min.time())
        # Most have 1 entry, some have 2 (SIM swap)
        if random.random() < 0.15:
            mid = random_timestamp(start + timedelta(days=30), now)
            sim_hist_rows.append((meta["sim_id"], sub_id, start, mid))
            sim_hist_rows.append((None, sub_id, mid, None))
        else:
            end = None
            if meta["status"] == "cancelled" and meta["end_date"]:
                end = datetime.combine(meta["end_date"], datetime.min.time())
            sim_hist_rows.append((meta["sim_id"], sub_id, start, end))

    insert_batch(cur, "sim_card_subscription_history",
                 ["sim_id", "subscription_id", "start_date", "end_date"],
                 sim_hist_rows)
    print_table_count(cur, "sim_card_subscription_history")

    conn.commit()
    print(">> 04_subscriptions done.\n")
