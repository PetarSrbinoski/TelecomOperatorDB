"""
01_lookups.py - Populate standalone lookup / reference tables.

This script runs FIRST because every other table in the schema references at
least one of these lookups via a foreign key.  By inserting them before any
transactional data, we guarantee that FK constraints are always satisfiable.

All data here is hand-crafted (not randomly generated) because:
  - Lookup values must be semantically meaningful (e.g. real department names,
    real mobile network generations like 2G/3G/4G/5G).
  - The row counts are tiny (5–18 rows each), so randomization adds no value.
  - Hardcoding ensures deterministic, readable data regardless of the seed.

Tables populated:
  billing_cycles, departments, employee_roles, services, overage_policies,
  network_technologies, payment_methods, addons, discounts_promotions
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()

    # -----------------------------------------------------------------------
    # billing_cycles
    # -----------------------------------------------------------------------
    # Telecom companies split customers into billing cycles so that not all
    # invoices are generated on the same day (spreads DB and payment load).
    # day_of_month is when the cycle closes; grace_days is extra time to pay.
    cycles = [
        ("Cycle Day 1", 1, 5, True),
        ("Cycle Day 5", 5, 7, True),
        ("Cycle Day 10", 10, 5, True),
        ("Cycle Day 15", 15, 7, True),
        ("Cycle Day 20", 20, 5, True),
    ]
    ids = insert_batch_returning(
        cur, "billing_cycles",
        ["cycle_name", "day_of_month", "grace_days", "is_active"],
        cycles, "billing_cycle_id",
    )
    store.set("billing_cycle_ids", ids)
    print_table_count(cur, "billing_cycles")

    # -----------------------------------------------------------------------
    # departments
    # -----------------------------------------------------------------------
    # Typical telecom org structure.  Locations are spread across US cities
    # to give address diversity in downstream employee data.
    depts = [
        ("Network Operations Center", "New York", "active"),
        ("Sales", "Chicago", "active"),
        ("Billing & Finance", "New York", "active"),
        ("Customer Support", "Dallas", "active"),
        ("Field Operations", "Denver", "active"),
        ("Human Resources", "New York", "active"),
        ("Information Technology", "San Francisco", "active"),
        ("Engineering", "San Francisco", "active"),
        ("Marketing", "Chicago", "active"),
    ]
    ids = insert_batch_returning(
        cur, "departments",
        ["department_name", "location", "status"],
        depts, "department_id",
    )
    store.set("department_ids", ids)
    print_table_count(cur, "departments")

    # -----------------------------------------------------------------------
    # employee_roles
    # -----------------------------------------------------------------------
    # access_level (1–5) controls what systems an employee can touch.
    # The hierarchy (Technician=1 → Director=5) is used later when building
    # the employee manager tree in 02_core_entities.py.
    roles = [
        ("Field Technician", "Performs on-site installations and repairs", 1, True),
        ("Support Agent", "Handles customer inquiries via phone/chat", 1, True),
        ("Senior Support Agent", "Handles escalated customer issues", 2, True),
        ("Network Engineer", "Monitors and maintains network infrastructure", 3, True),
        ("Software Engineer", "Develops internal tools and systems", 3, True),
        ("Team Lead", "Leads a team of agents or engineers", 3, True),
        ("Billing Analyst", "Manages billing processes and disputes", 2, True),
        ("Sales Representative", "Acquires new customers and upsells", 1, True),
        ("Manager", "Manages a department section", 4, True),
        ("Director", "Heads an entire department", 5, True),
        ("Data Analyst", "Analyzes business and network data", 2, True),
    ]
    ids = insert_batch_returning(
        cur, "employee_roles",
        ["role_name", "role_description", "access_level", "is_active"],
        roles, "role_id",
    )
    store.set("role_ids", ids)
    # Also store role names mapped to IDs so 02_core_entities can look up
    # specific roles ("Director", "Manager") when building the org hierarchy.
    store.set("role_map", list(zip(ids, [r[0] for r in roles])))
    print_table_count(cur, "employee_roles")

    # -----------------------------------------------------------------------
    # services
    # -----------------------------------------------------------------------
    # High-level service categories that products/plans are grouped under.
    # service_category is the short code used for filtering; description is
    # human-readable.  Each product in 02_core_entities links to one service.
    services = [
        ("Mobile Voice", "voice", "Voice calling service for mobile subscribers", True),
        ("Mobile Data", "data", "Mobile internet access", True),
        ("SMS Messaging", "messaging", "Short message service", True),
        ("International Roaming", "roaming", "Voice and data while abroad", True),
        ("Fixed Broadband", "broadband", "Home and business fixed internet", True),
        ("IoT Connectivity", "iot", "Machine-to-machine connectivity", True),
    ]
    ids = insert_batch_returning(
        cur, "services",
        ["service_name", "service_category", "description", "is_active"],
        services, "service_id",
    )
    store.set("service_ids", ids)
    print_table_count(cur, "services")

    # -----------------------------------------------------------------------
    # overage_policies
    # -----------------------------------------------------------------------
    # Define what happens when a subscriber exceeds their plan’s included
    # allowance.  Two strategies:
    #   - Charge-per-unit: voice_rate_per_min, sms_rate, data_rate_per_mb > 0.
    #   - Throttle: throttle_after_limit=True, rates=0, fair_use_limit_mb set.
    # Plans in 02_core_entities each reference one of these policies.
    policies = [
        ("Standard Overage", 0.0500, 0.0200, 0.0100, False, None, "active"),
        ("Premium Overage", 0.0300, 0.0100, 0.0050, False, None, "active"),
        ("Throttle After Limit", 0.0000, 0.0000, 0.0000, True, 50000.00, "active"),
        ("Pay-Per-Use", 0.1000, 0.0500, 0.0200, False, None, "active"),
        ("Budget Friendly", 0.0200, 0.0100, 0.0050, True, 20000.00, "active"),
    ]
    ids = insert_batch_returning(
        cur, "overage_policies",
        ["policy_name", "voice_rate_per_min", "sms_rate", "data_rate_per_mb",
         "throttle_after_limit", "fair_use_limit_mb", "status"],
        policies, "overage_policy_id",
    )
    store.set("overage_policy_ids", ids)
    print_table_count(cur, "overage_policies")

    # -----------------------------------------------------------------------
    # network_technologies
    # -----------------------------------------------------------------------
    # Real-world mobile network generations.  tower_sectors in 05_network
    # reference these to indicate what technology each antenna sector uses.
    techs = [
        ("GSM", "2G", "Global System for Mobile Communications", "active"),
        ("UMTS", "3G", "Universal Mobile Telecommunications System", "active"),
        ("LTE", "4G", "Long Term Evolution", "active"),
        ("LTE-Advanced", "4.5G", "LTE-Advanced carrier aggregation", "active"),
        ("NR", "5G", "5G New Radio", "active"),
    ]
    ids = insert_batch_returning(
        cur, "network_technologies",
        ["technology_name", "generation", "description", "status"],
        techs, "technology_id",
    )
    store.set("technology_ids", ids)
    print_table_count(cur, "network_technologies")

    # -----------------------------------------------------------------------
    # payment_methods
    # -----------------------------------------------------------------------
    # is_online distinguishes electronic methods (auto-chargeable) from
    # in-person ones (Cash).  Used in 08_billing when generating payments.
    methods = [
        ("Credit Card", "Visa/Mastercard", True, "active"),
        ("Debit Card", "Visa/Mastercard", True, "active"),
        ("Bank Transfer", "ACH Network", True, "active"),
        ("Cash", None, False, "active"),
        ("Mobile Wallet", "Apple Pay / Google Pay", True, "active"),
        ("Direct Debit", "SEPA / ACH", True, "active"),
    ]
    ids = insert_batch_returning(
        cur, "payment_methods",
        ["method_name", "provider_name", "is_online", "status"],
        methods, "payment_method_id",
    )
    store.set("payment_method_ids", ids)
    print_table_count(cur, "payment_methods")

    # -----------------------------------------------------------------------
    # addons
    # -----------------------------------------------------------------------
    # Optional services a subscriber can activate on top of their base plan.
    # is_recurring=True means the addon renews monthly; False means one-time.
    # allowance_value/unit define what the addon provides (e.g. 5120 MB).
    # Prices are stored here and also cached in the DataStore so that
    # 04_subscriptions can stamp price_at_activation on subscription_addons.
    addons = [
        ("Extra 5GB Data Pack", "data", 9.99, 5120.00, "MB", True, "active"),
        ("Extra 10GB Data Pack", "data", 14.99, 10240.00, "MB", True, "active"),
        ("Extra 20GB Data Pack", "data", 24.99, 20480.00, "MB", True, "active"),
        ("International Calling 100min", "voice", 12.99, 100.00, "minutes", True, "active"),
        ("International Calling 300min", "voice", 29.99, 300.00, "minutes", True, "active"),
        ("Unlimited SMS Bundle", "messaging", 4.99, None, None, True, "active"),
        ("Device Insurance Basic", "insurance", 7.99, None, None, True, "active"),
        ("Device Insurance Premium", "insurance", 12.99, None, None, True, "active"),
        ("Streaming Music Pass", "entertainment", 9.99, None, None, True, "active"),
        ("Streaming Video Pass", "entertainment", 14.99, None, None, True, "active"),
        ("Roaming Day Pass", "roaming", 5.99, 500.00, "MB", False, "active"),
        ("Family Number Add-on", "voice", 3.99, None, None, True, "active"),
    ]
    ids = insert_batch_returning(
        cur, "addons",
        ["addon_name", "addon_type", "price", "allowance_value", "allowance_unit",
         "is_recurring", "status"],
        addons, "addon_id",
    )
    store.set("addon_ids", ids)
    # Cache addon prices alongside IDs for 04_subscriptions to reference
    # when setting the price_at_activation column (snapshot of price at
    # the time the customer activated the add-on).
    store.set("addon_prices", [a[2] for a in addons])
    print_table_count(cur, "addons")

    # -----------------------------------------------------------------------
    # discounts_promotions
    # -----------------------------------------------------------------------
    # Mix of percentage and fixed-value discounts with varied validity windows.
    # One row ("Black Friday") is intentionally expired to test that downstream
    # queries can filter by status.  eligibility_rule is a free-text hint
    # (not enforced in SQL) describing who qualifies.
    today = date.today()
    promos = [
        ("New Customer 20% Off", "percentage", 20.00, today - timedelta(days=365), today + timedelta(days=180), None, "active"),
        ("Summer Data Boost", "percentage", 15.00, date(2026, 6, 1), date(2026, 8, 31), None, "active"),
        ("Loyalty $10 Credit", "fixed", 10.00, today - timedelta(days=730), None, "customer_tenure > 24 months", "active"),
        ("Student Discount", "percentage", 10.00, today - timedelta(days=365), today + timedelta(days=365), "customer_type = student", "active"),
        ("Bundle Discount $5", "fixed", 5.00, today - timedelta(days=180), today + timedelta(days=180), "subscriptions >= 2", "active"),
        ("Black Friday 30% Off", "percentage", 30.00, date(2025, 11, 25), date(2025, 12, 2), None, "expired"),
        ("Refer-a-Friend $15", "fixed", 15.00, today - timedelta(days=365), None, "referred_customer", "active"),
    ]
    ids = insert_batch_returning(
        cur, "discounts_promotions",
        ["promotion_name", "discount_type", "discount_value", "valid_from", "valid_to",
         "eligibility_rule", "status"],
        promos, "promotion_id",
    )
    store.set("promotion_ids", ids)
    print_table_count(cur, "discounts_promotions")

    conn.commit()
    print(">> 01_lookups done.\n")
