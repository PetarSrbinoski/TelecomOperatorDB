"""
02_core_entities.py - Generate customers, addresses, employees, products, and plans.

This is the second script to run and creates the main "who" and "what" of the
telecom business:
  - Customers (individual & business) with realistic PII via Faker.
  - Customer addresses (1–3 per customer) with one marked as primary.
  - Employees arranged in a 3-tier hierarchy (Director → Manager → Staff)
    to satisfy the self-referencing manager_id FK.
  - Products grouped under services, and plans with included allowances.

Design decisions:
  - 70/30 individual/business split mirrors typical telecom subscriber bases.
  - Status distributions (85% active, 10% suspended, 5% closed) were chosen
    to give enough "edge" rows for testing filters without overwhelming them.
  - Employees are inserted in three phases (directors first, then managers,
    then staff) because each phase's manager_id must reference an already-
    inserted employee.  This avoids FK violations on the self-join.
  - Plan templates use realistic names and tiered pricing ($15–$150) to
    make billing calculations in 08_billing produce plausible invoice totals.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()

    # -----------------------------------------------------------------------
    # customers
    # -----------------------------------------------------------------------
    # The CHECK constraint chk_customers_type_fields requires:
    #   individual → first_name + last_name NOT NULL, company_name NULL
    #   business   → company_name NOT NULL, first/last can be NULL
    # So we branch on customer_type to satisfy this.
    # fake.unique.email() guarantees the UNIQUE constraint on email.
    n_customers = get_scaled_count("customers", scale)
    customer_ids = []
    used_emails = set()

    rows = []
    for _ in range(n_customers):
        ctype = weighted_choice(["individual", "business"], [70, 30])
        if ctype == "individual":
            fn = fake.first_name()
            ln = fake.last_name()
            cn = None
            dob = fake.date_of_birth(minimum_age=18, maximum_age=80)
        else:
            fn = None
            ln = None
            cn = fake.company()
            dob = None

        email = fake.unique.email()
        used_emails.add(email)
        phone = fake.phone_number()[:30]
        status = weighted_choice(["active", "suspended", "closed"], [85, 10, 5])
        created = random_timestamp(datetime(2020, 1, 1), datetime.now())
        updated = random_timestamp(created, datetime.now())
        rows.append((ctype, fn, ln, cn, email, phone, dob, status, created, updated))

    customer_ids = insert_batch_returning(
        cur, "customers",
        ["customer_type", "first_name", "last_name", "company_name",
         "email", "phone", "date_of_birth", "status", "created_at", "updated_at"],
        rows, "customer_id",
    )
    store.set("customer_ids", customer_ids)
    print_table_count(cur, "customers")

    # -----------------------------------------------------------------------
    # customer_addresses
    # -----------------------------------------------------------------------
    # Each customer gets 1–3 addresses.  random.sample ensures no duplicate
    # address types per customer.  The first address is always is_primary=True
    # so every customer has exactly one primary address.
    address_types = ["billing", "service", "home", "office", "other"]
    addr_rows = []
    for cid in customer_ids:
        n_addr = random.randint(1, 3)
        chosen_types = random.sample(address_types, min(n_addr, len(address_types)))
        for i, atype in enumerate(chosen_types):
            country = fake.country()[:100]
            city = fake.city()[:100]
            street = fake.street_address()[:255]
            postal = fake.postcode()[:20]
            lat = round(random.uniform(-90, 90), 6)
            lon = round(random.uniform(-180, 180), 6)
            is_primary = (i == 0)
            created = random_timestamp(datetime(2020, 1, 1), datetime.now())
            addr_rows.append((cid, atype, country, city, street, postal, lat, lon, is_primary, created))

    insert_batch(cur, "customer_addresses",
                 ["customer_id", "address_type", "country", "city", "street",
                  "postal_code", "latitude", "longitude", "is_primary", "created_at"],
                 addr_rows)
    print_table_count(cur, "customer_addresses")

    # -----------------------------------------------------------------------
    # employees  (hierarchy: directors -> managers -> staff)
    # -----------------------------------------------------------------------
    # The employees table has a self-referencing FK: manager_id → employee_id.
    # To satisfy this, we insert in three waves:
    #   Phase 1 – Directors: manager_id = NULL (they report to no one).
    #   Phase 2 – Managers/Leads: manager_id points to a Phase-1 director.
    #   Phase 3 – Staff: manager_id points to a Phase-2 manager.
    # Each phase uses insert_batch_returning to capture the new employee_id
    # before the next phase can reference it.
    #
    # Hire dates are staggered: directors hired 5–10 yrs ago, managers 2–8,
    # staff 0–5.  This produces realistic tenure distributions.
    n_employees = get_scaled_count("employees", scale)
    dept_ids = store.get("department_ids")
    role_map = store.get("role_map")  # list of (role_id, role_name)

    # Find role IDs by name
    director_role = [r[0] for r in role_map if r[1] == "Director"][0]
    manager_role = [r[0] for r in role_map if r[1] == "Manager"][0]
    lead_role = [r[0] for r in role_map if r[1] == "Team Lead"][0]
    staff_roles = [r[0] for r in role_map if r[1] not in ("Director", "Manager", "Team Lead")]

    employee_ids = []
    used_emp_emails = set()

    # Phase 1: Directors (no manager — top of the org tree)
    # Spread across departments so each dept has leadership.
    n_directors = min(5, len(dept_ids))
    for i in range(n_directors):
        fn = fake.first_name()
        ln = fake.last_name()
        email = fake.unique.email()
        used_emp_emails.add(email)
        phone = fake.phone_number()[:30]
        hdate = random_date(today - timedelta(days=3650), today - timedelta(days=1825))
        row = (dept_ids[i % len(dept_ids)], director_role, fn, ln, email, phone, hdate, "active", None)
        eid = insert_batch_returning(
            cur, "employees",
            ["department_id", "role_id", "first_name", "last_name", "email",
             "phone", "hire_date", "employment_status", "manager_id"],
            [row], "employee_id",
        )[0]
        employee_ids.append(eid)

    # Phase 2: Managers (report to directors)
    # Randomly assigned Manager or Team Lead role for variety.
    n_managers = min(15, n_employees - n_directors)
    for i in range(n_managers):
        fn = fake.first_name()
        ln = fake.last_name()
        email = fake.unique.email()
        used_emp_emails.add(email)
        phone = fake.phone_number()[:30]
        hdate = random_date(today - timedelta(days=2920), today - timedelta(days=730))
        mgr = random.choice(employee_ids[:n_directors])  # reports to a director
        role = random.choice([manager_role, lead_role])
        row = (random.choice(dept_ids), role, fn, ln, email, phone, hdate, "active", mgr)
        eid = insert_batch_returning(
            cur, "employees",
            ["department_id", "role_id", "first_name", "last_name", "email",
             "phone", "hire_date", "employment_status", "manager_id"],
            [row], "employee_id",
        )[0]
        employee_ids.append(eid)

    # Phase 3: Staff (report to managers/leads)
    # 90% active, 5% inactive, 5% on_leave for realistic payroll states.
    n_staff = n_employees - n_directors - n_managers
    for i in range(n_staff):
        fn = fake.first_name()
        ln = fake.last_name()
        email = fake.unique.email()
        used_emp_emails.add(email)
        phone = fake.phone_number()[:30]
        hdate = random_date(today - timedelta(days=1825), today - timedelta(days=30))
        mgr = random.choice(employee_ids[n_directors:n_directors + n_managers])
        status = weighted_choice(["active", "inactive", "on_leave"], [90, 5, 5])
        row = (random.choice(dept_ids), random.choice(staff_roles), fn, ln, email, phone, hdate, status, mgr)
        eid = insert_batch_returning(
            cur, "employees",
            ["department_id", "role_id", "first_name", "last_name", "email",
             "phone", "hire_date", "employment_status", "manager_id"],
            [row], "employee_id",
        )[0]
        employee_ids.append(eid)

    # Store employee ID segments so later scripts can assign appropriate
    # employees (e.g. only managers for escalations, only staff for tickets).
    store.set("employee_ids", employee_ids)
    store.set("director_ids", employee_ids[:n_directors])
    store.set("manager_ids", employee_ids[n_directors:n_directors + n_managers])
    store.set("staff_ids", employee_ids[n_directors + n_managers:])
    print_table_count(cur, "employees")

    # -----------------------------------------------------------------------
    # products
    # -----------------------------------------------------------------------
    # Products are the "sellable offerings" that group under a service.
    # Each has a unique product_code (e.g. PP-BASIC-001) following a naming
    # convention: type prefix + name + sequence number.
    # Products are linked to random services; plans (below) then link to products.
    service_ids = store.get("service_ids")
    product_defs = [
        ("Postpaid Basic", "PP-BASIC-001", "postpaid"),
        ("Postpaid Plus", "PP-PLUS-002", "postpaid"),
        ("Postpaid Premium", "PP-PREM-003", "postpaid"),
        ("Postpaid Business", "PP-BIZ-004", "postpaid"),
        ("Prepaid Starter", "PR-START-001", "prepaid"),
        ("Prepaid Value", "PR-VAL-002", "prepaid"),
        ("Prepaid Unlimited", "PR-UNL-003", "prepaid"),
        ("Broadband Home", "BB-HOME-001", "broadband"),
        ("Broadband Business", "BB-BIZ-002", "broadband"),
        ("IoT Basic", "IOT-BASIC-001", "iot"),
        ("IoT Enterprise", "IOT-ENT-002", "iot"),
        ("Roaming Traveler", "RM-TRAV-001", "roaming"),
    ]
    product_rows = []
    for pname, pcode, ptype in product_defs:
        sid = random.choice(service_ids)
        product_rows.append((sid, pname, pcode, ptype, None, "active"))

    product_ids = insert_batch_returning(
        cur, "products",
        ["service_id", "product_name", "product_code", "product_type", "description", "status"],
        product_rows, "product_id",
    )
    store.set("product_ids", product_ids)
    print_table_count(cur, "products")

    # -----------------------------------------------------------------------
    # plans
    # -----------------------------------------------------------------------
    # Each plan belongs to one product and defines the monthly fee plus
    # included allowances (voice minutes, SMS, data MB).  Key choices:
    #   - 99999 for "unlimited" plans (instead of NULL) keeps arithmetic
    #     simple in overage calculations.
    #   - contract_term_months: 0 = no commitment (prepaid/PAYG),
    #     12 or 24 = standard postpaid lock-in periods.
    #   - Each plan references an overage_policy so that CDR charge
    #     calculations in 07_usage_cdrs know the per-unit rates.
    overage_ids = store.get("overage_policy_ids")
    plan_defs = []
    plan_templates = [
        ("Basic 1GB", 15.00, 100, 100, 1024, 0),
        ("Basic 5GB", 25.00, 200, 200, 5120, 12),
        ("Standard 10GB", 39.99, 500, 500, 10240, 12),
        ("Standard 20GB", 49.99, 1000, 500, 20480, 12),
        ("Plus 50GB", 69.99, 2000, 1000, 51200, 24),
        ("Premium Unlimited", 99.99, 99999, 99999, 102400, 24),
        ("Business Basic", 45.00, 500, 500, 20480, 12),
        ("Business Pro", 79.99, 2000, 1000, 51200, 24),
        ("Business Enterprise", 149.99, 99999, 99999, 102400, 24),
        ("Prepaid Daily", 1.99, 30, 50, 512, 0),
        ("Prepaid Weekly", 9.99, 200, 200, 3072, 0),
        ("Prepaid Monthly", 19.99, 500, 500, 10240, 0),
        ("Data Only 10GB", 20.00, 0, 0, 10240, 0),
        ("Data Only 50GB", 35.00, 0, 0, 51200, 12),
        ("Data Only 100GB", 55.00, 0, 0, 102400, 24),
        ("IoT Starter", 5.00, 0, 100, 256, 12),
        ("IoT Growth", 15.00, 0, 500, 1024, 12),
        ("Broadband 50Mbps", 39.99, 0, 0, 512000, 12),
        ("Broadband 100Mbps", 59.99, 0, 0, 1024000, 24),
        ("Broadband 500Mbps", 89.99, 0, 0, 2048000, 24),
        ("Roaming Lite", 29.99, 100, 50, 2048, 0),
        ("Roaming Premium", 59.99, 500, 200, 10240, 0),
        ("Family Plan 2-Line", 79.99, 1000, 1000, 30720, 24),
        ("Family Plan 4-Line", 129.99, 3000, 2000, 61440, 24),
        ("Student Special", 29.99, 500, 500, 20480, 12),
    ]

    plan_rows = []
    for i, (pname, fee, mins, sms, data, term) in enumerate(plan_templates):
        pid = product_ids[i % len(product_ids)]
        oid = random.choice(overage_ids)
        plan_rows.append((pid, pname, fee, mins, sms, data, oid, term, "active"))

    plan_ids = insert_batch_returning(
        cur, "plans",
        ["product_id", "plan_name", "monthly_fee", "included_voice_minutes",
         "included_sms", "included_data_mb", "overage_policy_id",
         "contract_term_months", "status"],
        plan_rows, "plan_id",
    )
    store.set("plan_ids", plan_ids)
    # Cache the monthly fee alongside each plan_id so 08_billing can look
    # up the fee without querying the DB when building invoice line items.
    store.set("plan_fees", [t[1] for t in plan_templates])
    print_table_count(cur, "plans")

    conn.commit()
    print(">> 02_core_entities done.\n")
