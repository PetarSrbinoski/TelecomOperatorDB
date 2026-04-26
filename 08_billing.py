"""
08_billing.py - Generate billing runs, invoices, invoice items, and payments.

This script simulates the telecom billing cycle:
  1. Billing runs: monthly batch jobs that generate invoices.
  2. Invoices: one per account per billing period, with calculated totals.
  3. Invoice items: line-by-line charges (plan fees, addons, overage, tax).
  4. Payments: customer payments against invoices.

Design decisions:
  - Billing runs go back 6–12 months.  The most recent month’s run has
    status=‘running’ (simulating an in-progress batch); all others are ‘completed’.
  - ~5% of accounts are skipped per period (simulating newly opened or
    recently closed accounts that don’t get billed).
  - Invoice totals are computed bottom-up: sum line items → apply discount →
    add tax.  This ensures total_amount = SUM(invoice_items.line_amount),
    which is validated in 10_validate_and_run.
  - dateutil.relativedelta is used for month arithmetic because timedelta
    can’t express "one calendar month" (months vary in length).
  - Payments match invoice totals for ‘paid’ invoices.  Overdue invoices
    sometimes have partial payments (30–80% of total) to create realistic
    accounts-receivable scenarios.
  - Tax rates are randomly chosen from common US state sales tax rates.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *
from datetime import date
from dateutil.relativedelta import relativedelta


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    account_ids = store.get("account_ids")
    billing_cycle_ids = store.get("billing_cycle_ids")
    sub_meta_list = store.get("sub_meta")  # [(sub_id, meta_dict)]
    plan_ids = store.get("plan_ids")
    plan_fees = store.get("plan_fees")
    payment_method_ids = store.get("payment_method_ids")

    # Build plan_id → monthly_fee map from the cached data.
    # This avoids querying the plans table when calculating invoice line items.
    plan_fee_map = dict(zip(plan_ids, plan_fees))

    # Build account_id → [(sub_id, plan_id), ...] so we know which
    # subscriptions to bill on each account’s invoice.
    acct_subs = {}
    for sub_id, meta in sub_meta_list:
        aid = meta["account_id"]
        pid = meta["plan_id"]
        if aid not in acct_subs:
            acct_subs[aid] = []
        acct_subs[aid].append((sub_id, pid))

    # -----------------------------------------------------------------------
    # billing_runs — last N months
    # -----------------------------------------------------------------------
    # Each billing run represents a monthly batch job.  run_started_at is
    # midnight of the day after the period ends (the 1st of the next month).
    # run_finished_at is 1–4 hours later.  The current month’s run is
    # ‘running’ (in progress); all prior months are ‘completed’.
    n_months = max(6, get_scaled_count("billing_runs", scale))
    br_rows = []
    billing_periods = []
    for i in range(n_months):
        month_start = (today.replace(day=1) - relativedelta(months=i))
        month_end = (month_start + relativedelta(months=1)) - timedelta(days=1)
        bcid = random.choice(billing_cycle_ids)
        run_start = datetime.combine(month_end + timedelta(days=1), datetime.min.time())
        run_end = run_start + timedelta(hours=random.randint(1, 4))
        status = "completed" if i > 0 else "running"
        br_rows.append((bcid, month_start, month_end, run_start,
                         run_end if status == "completed" else None,
                         status, 0))
        billing_periods.append((month_start, month_end))

    br_ids = insert_batch_returning(
        cur, "billing_runs",
        ["billing_cycle_id", "period_start", "period_end",
         "run_started_at", "run_finished_at", "status", "generated_invoices_count"],
        br_rows, "billing_run_id",
    )
    store.set("billing_run_ids", br_ids)
    print_table_count(cur, "billing_runs")

    # -----------------------------------------------------------------------
    # invoices & invoice_items
    # -----------------------------------------------------------------------
    # For each billing period x account, we generate one invoice.  Line items
    # now include more fee types per subscription (line rental, regulatory fee,
    # international charges) to reach the 10M invoice_items target at ~10
    # items per invoice x ~1M invoices.
    #
    # Performance: invoices are batch-inserted per billing period using
    # execute_values (via insert_batch_returning) instead of one-at-a-time.
    # Invoice items are also flushed per period so memory stays bounded at
    # ~100 MB per period instead of accumulating all 10M rows.
    used_inv_nums = set()
    all_invoice_ids = []
    invoice_account_map = []  # (invoice_id, account_id, total_amount, status)

    inv_cols = ["account_id", "invoice_number", "billing_period_start",
                "billing_period_end", "issue_date", "due_date",
                "total_amount", "tax_amount", "discount_amount", "status"]
    item_cols = ["invoice_id", "subscription_id", "item_type", "description",
                 "quantity", "unit_price", "line_amount", "tax_rate"]

    for period_start, period_end in billing_periods:
        period_label = period_start.strftime("%Y%m")

        # Accumulate all invoices for this period before batch-inserting.
        inv_rows = []
        inv_items_pending = []  # parallel list: items per invoice
        inv_totals = []
        inv_statuses = []
        inv_aids = []

        for aid in account_ids:
            if random.random() < 0.05:
                continue  # skip ~5% (new/closed accounts)

            inv_num = f"INV-{period_label}-{unique_string('', 8, used_inv_nums)}"
            issue_date = period_end + timedelta(days=1)
            due_date = issue_date + timedelta(days=random.choice([15, 20, 30]))
            status = weighted_choice(["paid", "issued", "overdue", "cancelled"], [70, 15, 10, 5])

            items = []
            subs = acct_subs.get(aid, [])
            total = 0.0

            for sub_id, plan_id in subs[:5]:  # up to 5 subs per invoice
                fee = plan_fee_map.get(plan_id, 29.99)
                items.append((sub_id, "plan_fee", "Monthly plan fee", 1, fee, fee, 0.00))
                total += fee

                # Line rental -- always charged per active line
                rental = round(random.uniform(1.99, 4.99), 2)
                items.append((sub_id, "line_rental", "Line rental charge", 1, rental, rental, 0.00))
                total += rental

                # Regulatory recovery fee -- mandated pass-through cost
                reg_fee = round(random.uniform(0.50, 2.99), 2)
                items.append((sub_id, "regulatory_fee", "Regulatory recovery fee", 1, reg_fee, reg_fee, 0.00))
                total += reg_fee

                # Add-on service charges (60% of subs)
                if random.random() < 0.60:
                    addon_fee = round(random.uniform(3.99, 14.99), 2)
                    items.append((sub_id, "addon_fee", "Add-on service", 1, addon_fee, addon_fee, 0.00))
                    total += addon_fee

                # Overage charges (40% of subs)
                if random.random() < 0.40:
                    ov_type = random.choice(["overage_voice", "overage_sms", "overage_data"])
                    qty = round(random.uniform(1, 100), 2)
                    unit_p = round(random.uniform(0.01, 0.10), 2)
                    line_amt = round(qty * unit_p, 2)
                    items.append((sub_id, ov_type, f"{ov_type.replace('_', ' ').title()}", qty, unit_p, line_amt, 0.00))
                    total += line_amt

                # International usage charges (20% of subs)
                if random.random() < 0.20:
                    intl = round(random.uniform(0.50, 5.00), 2)
                    items.append((sub_id, "international_charge", "International usage", 1, intl, intl, 0.00))
                    total += intl

            if not items:
                items.append((None, "plan_fee", "Account maintenance fee", 1, 5.00, 5.00, 0.00))
                total = 5.00

            # Discount
            discount = 0.0
            if random.random() < 0.15:
                discount = round(total * random.uniform(0.05, 0.20), 2)
                items.append((None, "discount", "Promotional discount", 1, -discount, -discount, 0.00))
                total -= discount

            # Tax
            tax_rate = random.choice([0.06, 0.07, 0.08, 0.085, 0.10])
            tax = round(total * tax_rate, 2)
            items.append((None, "tax", "Sales tax", 1, tax, tax, round(tax_rate * 100, 2)))
            total_with_tax = round(total + tax, 2)

            inv_rows.append((aid, inv_num, period_start, period_end, issue_date,
                             due_date, total_with_tax, tax, discount, status))
            inv_items_pending.append(items)
            inv_totals.append(total_with_tax)
            inv_statuses.append(status)
            inv_aids.append(aid)

        # Batch-insert all invoices for this period (execute_values is ~100x
        # faster than row-at-a-time for 100k invoices per period).
        period_inv_ids = insert_batch_returning(
            cur, "invoices", inv_cols, inv_rows, "invoice_id",
        )

        # Build invoice_items for this period and flush to DB.
        period_item_rows = []
        for inv_id, items, total, status, aid in zip(
            period_inv_ids, inv_items_pending, inv_totals, inv_statuses, inv_aids
        ):
            all_invoice_ids.append(inv_id)
            invoice_account_map.append((inv_id, aid, total, status))
            for sub_id, itype, desc, qty, uprice, lamt, trate in items:
                period_item_rows.append((inv_id, sub_id, itype, desc, qty, uprice, lamt, trate))

        insert_batch(cur, "invoice_items", item_cols, period_item_rows)
        print(f"  Period {period_label}: {len(period_inv_ids):,} invoices, {len(period_item_rows):,} items")

    store.set("invoice_ids", all_invoice_ids)
    store.set("invoice_account_map", invoice_account_map)
    print_table_count(cur, "invoices")
    print_table_count(cur, "invoice_items")

    # Update billing_runs generated_invoices_count
    invoices_per_run = len(all_invoice_ids) // max(len(br_ids), 1)
    for br_id in br_ids:
        cur.execute(
            "UPDATE billing_runs SET generated_invoices_count = %s WHERE billing_run_id = %s",
            (invoices_per_run, br_id),
        )

    # -----------------------------------------------------------------------
    # payments
    # -----------------------------------------------------------------------
    # Payments are generated independently to hit the 1M target.  Each payment
    # is linked to a random invoice.  For paid invoices the amount matches the
    # total; for others it's a partial or advance payment.  Multiple payments
    # per invoice are allowed (split payments), which is realistic.
    n_payments = get_scaled_count("payments", scale)
    used_refs = set()
    payment_cols = ["account_id", "invoice_id", "payment_method_id",
                    "payment_date", "amount", "reference_number", "status"]

    for chunk_start in range(0, n_payments, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE, n_payments)
        payment_rows = []
        for _ in range(chunk_end - chunk_start):
            inv_id, aid, total, inv_status = random.choice(invoice_account_map)
            pmid = random.choice(payment_method_ids)
            pdate = random_timestamp(now - timedelta(days=365), now)
            ref = unique_string("PAY-", 10, used_refs)

            if inv_status == "paid":
                amount = total
                pstatus = "completed"
            elif inv_status == "overdue":
                amount = round(total * random.uniform(0.3, 0.8), 2)
                pstatus = weighted_choice(["completed", "pending"], [80, 20])
            else:
                amount = round(total * random.uniform(0.5, 1.0), 2)
                pstatus = weighted_choice(["completed", "pending", "failed"], [60, 25, 15])

            payment_rows.append((aid, inv_id, pmid, pdate, amount, ref, pstatus))

        insert_batch(cur, "payments", payment_cols, payment_rows)

    print_table_count(cur, "payments")

    conn.commit()
    print(">> 08_billing done.\n")
