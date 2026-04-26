"""
03_accounts_sims.py - Generate accounts, contracts, SIM cards, and devices.

This script creates the financial and physical assets that connect customers
to the telecom network:
  - Accounts: one per customer, holding billing/credit state.
  - Contracts: legal agreements tying a customer+account to a term.
  - SIM cards: the physical (or eSIM) identity modules with ICCID/IMSI/MSISDN.
  - Devices: phones/tablets/modems with IMEI and serial numbers.

Design decisions:
  - One account per customer (1:1) simplifies the model.  In real telecoms
    a customer may have multiple accounts, but 1:1 is sufficient for testing.
  - SIM identifiers (ICCID 20-digit, IMSI 15-digit, MSISDN 11-digit) are
    generated as fixed-width digit strings with uniqueness tracked in sets.
  - We generate MORE SIMs (600) than subscriptions (500) so that some SIMs
    remain in "available" status — realistic for warehouse inventory.
  - Device IMEI is 15 digits per the GSM standard.
  - Manufacturer/model lists use real brands so the data looks plausible.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    customer_ids = store.get("customer_ids")
    billing_cycle_ids = store.get("billing_cycle_ids")

    # -----------------------------------------------------------------------
    # accounts — one per customer
    # -----------------------------------------------------------------------
    # Account numbers follow the pattern ACC-XXXXXXXX (8-digit random suffix)
    # enforced unique via a set.  credit_limit and current_balance are random
    # but bounded to plausible telecom ranges.
    used_acct_nums = set()
    acct_rows = []
    for cid in customer_ids:
        acct_num = unique_string("ACC-", 8, used_acct_nums)
        status = weighted_choice(["active", "suspended", "closed"], [85, 10, 5])
        credit = round(random.uniform(200, 5000), 2)
        balance = round(random.uniform(0, 500), 2)
        bcid = random.choice(billing_cycle_ids)
        created = random_timestamp(datetime(2020, 1, 1), now)
        updated = random_timestamp(created, now)
        acct_rows.append((cid, acct_num, status, credit, balance, bcid, created, updated))

    account_ids = insert_batch_returning(
        cur, "accounts",
        ["customer_id", "account_number", "account_status", "credit_limit",
         "current_balance", "billing_cycle_id", "created_at", "updated_at"],
        acct_rows, "account_id",
    )
    store.set("account_ids", account_ids)
    # Map customer_id → account_id for contracts and subscriptions to find
    # the right account for a given customer without a DB lookup.
    cust_to_acct = dict(zip(customer_ids, account_ids))
    store.set("cust_to_acct", list(cust_to_acct.items()))
    print_table_count(cur, "accounts")

    # -----------------------------------------------------------------------
    # contracts
    # -----------------------------------------------------------------------
    # Contracts bind a customer+account to a term (12 or 24 months).
    # Contract status depends on whether end_date has passed:
    #   past end_date   → 70% expired, 30% renewed
    #   future end_date → 90% active, 10% cancelled
    # signed_at is always 0–7 days before start_date (realistic signing window).
    # contract_meta is stored for 04_subscriptions to optionally link subs
    # to contracts.
    n_contracts = get_scaled_count("contracts", scale)
    used_contract_nums = set()
    contract_rows = []
    contract_meta = []  # (customer_id, account_id, start_date, end_date)

    for _ in range(n_contracts):
        cid = random.choice(customer_ids)
        aid = cust_to_acct[cid]
        cnum = unique_string("CTR-", 8, used_contract_nums)
        ctype = random.choice(["postpaid", "prepaid", "broadband", "iot", "bundle"])
        start = random_date(today - timedelta(days=1095), today - timedelta(days=30))
        term = random.choice([12, 24])
        end = start + timedelta(days=term * 30)
        if end < today:
            status = weighted_choice(["expired", "renewed"], [70, 30])
        else:
            status = weighted_choice(["active", "cancelled"], [90, 10])
        auto_renew = random.random() < 0.10
        signed = random_timestamp(
            datetime.combine(start - timedelta(days=7), datetime.min.time()),
            datetime.combine(start, datetime.min.time()),
        )
        contract_rows.append((cid, aid, ctype, cnum, start, end, auto_renew, status, signed))
        contract_meta.append((cid, aid, start, end))

    contract_ids = insert_batch_returning(
        cur, "contracts",
        ["customer_id", "account_id", "contract_type", "contract_number",
         "start_date", "end_date", "auto_renew", "status", "signed_at"],
        contract_rows, "contract_id",
    )
    store.set("contract_ids", contract_ids)
    store.set("contract_meta", list(zip(contract_ids, contract_meta)))
    print_table_count(cur, "contracts")

    # -----------------------------------------------------------------------
    # sim_cards
    # -----------------------------------------------------------------------
    # SIM card identifiers follow telecom standards:
    #   ICCID: 19–20 digits (we use 20) — printed on the SIM card.
    #   IMSI:  15 digits — stored on the SIM, identifies the subscriber to the network.
    #   MSISDN: the phone number ("1" + 10 digits for US-style format).
    # All three must be globally unique (enforced by UNIQUE constraints in DDL).
    # Status distribution: 20% available (warehouse), 60% active (in use),
    # 10% suspended, 10% deactivated.  Only non-available SIMs have issued_at.
    n_sims = get_scaled_count("sim_cards", scale)
    used_iccid = set()
    used_imsi = set()
    used_msisdn = set()

    sim_rows = []
    for _ in range(n_sims):
        iccid = unique_digits(20, used_iccid)
        imsi = unique_digits(15, used_imsi)
        msisdn = "1" + unique_digits(10, used_msisdn)  # US-style numbers
        sim_type = random.choice(["nano", "micro", "eSIM"])
        pin = str(random.randint(1000, 9999))
        puk = str(random.randint(10000000, 99999999))
        status = weighted_choice(
            ["available", "active", "suspended", "deactivated"],
            [20, 60, 10, 10],
        )
        issued = random_timestamp(datetime(2021, 1, 1), now) if status != "available" else None
        sim_rows.append((iccid, imsi, msisdn, sim_type, pin, puk, status, issued))

    sim_ids = insert_batch_returning(
        cur, "sim_cards",
        ["iccid", "imsi", "msisdn", "sim_type", "pin_code", "puk_code", "status", "issued_at"],
        sim_rows, "sim_id",
    )
    store.set("sim_ids", sim_ids)
    # Cache SIM ID → MSISDN mapping for CDR generation in 07_usage_cdrs,
    # where each call/SMS record needs the originating phone number.
    store.set("sim_msisdns", list(zip(sim_ids, [r[2] for r in sim_rows])))
    print_table_count(cur, "sim_cards")

    # -----------------------------------------------------------------------
    # devices
    # -----------------------------------------------------------------------
    # Real manufacturer + model combinations make the data visually credible.
    # IMEI is 15 digits per the GSM standard (we don’t compute the Luhn
    # check digit — unnecessary for synthetic data).
    # Device type distribution: 70% smartphone (the norm), 15% tablet,
    # 10% modem, 5% IoT — mirrors real telecom device registries.
    n_devices = get_scaled_count("devices", scale)
    used_imei = set()
    used_serial = set()

    manufacturers = [
        ("Apple", ["iPhone 15", "iPhone 15 Pro", "iPhone 14", "iPhone SE", "iPad Air"]),
        ("Samsung", ["Galaxy S24", "Galaxy S23", "Galaxy A54", "Galaxy Tab S9", "Galaxy Z Flip5"]),
        ("Huawei", ["P60 Pro", "Mate 60", "nova 12", "MatePad 11"]),
        ("Xiaomi", ["14 Pro", "Redmi Note 13", "Poco X6", "Pad 6"]),
        ("Google", ["Pixel 8", "Pixel 8 Pro", "Pixel 7a"]),
    ]
    device_types = ["smartphone", "tablet", "modem", "iot_device"]

    device_rows = []
    for _ in range(n_devices):
        mfr, models = random.choice(manufacturers)
        model = random.choice(models)
        imei = unique_digits(15, used_imei)
        serial = unique_string("SN-", 10, used_serial)
        dtype = weighted_choice(device_types, [70, 15, 10, 5])
        pdate = random_date(today - timedelta(days=1095), today)
        device_rows.append((imei, serial, mfr, model, dtype, pdate))

    device_ids = insert_batch_returning(
        cur, "devices",
        ["imei", "serial_number", "manufacturer", "model", "device_type", "purchase_date"],
        device_rows, "device_id",
    )
    store.set("device_ids", device_ids)
    print_table_count(cur, "devices")

    conn.commit()
    print(">> 03_accounts_sims done.\n")
