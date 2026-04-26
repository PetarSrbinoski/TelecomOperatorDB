"""
09_crm.py - Generate CRM tickets, interactions, ticket status history, and employee assignments.

This is the final data-generation script.  It simulates the customer support
workflow:
  1. Tickets are opened by customers for various issue types.
  2. Interactions track each contact between the customer and support agents.
  3. Ticket status history records the audit trail of status changes.
  4. Employee assignments link support agents to specific tickets.

Design decisions:
  - Ticket type distribution covers the full spectrum of telecom support
    issues (billing, technical, service requests, complaints, etc.).
  - Subject lines are curated per type for realism in search/NLP testing.
  - Priority weighting (20% low, 50% medium, 20% high, 10% critical)
    mirrors real support queues where most issues are routine.
  - Status weighting (50% resolved, 20% open, etc.) reflects a healthy
    support operation that closes most tickets.
  - Escalated tickets get a parent_ticket_id pointing to a non-escalated
    ticket, simulating the real escalation workflow.
  - Interactions are generated chronologically with increasing timestamps
    to create a realistic conversation timeline.
  - Status transitions follow a state machine:
      open → in_progress → resolved → closed
                        → escalated → (resolved or back to in_progress)
    This ensures status_history records are logically consistent.
  - Employee assignments (ticket-related) complement the outage assignments
    created in 06_network_events.py, filling the other half of the budget.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    customer_ids = store.get("customer_ids")
    account_ids = store.get("account_ids")
    sub_ids = store.get("subscription_ids")
    employee_ids = store.get("employee_ids")

    # -----------------------------------------------------------------------
    # crm_tickets
    # -----------------------------------------------------------------------
    # Each ticket links to a customer (required), and optionally to an account
    # and/or subscription (for context).  ~85% are assigned to an employee;
    # 15% are unassigned (e.g. just submitted, awaiting triage).
    # Resolved/closed tickets have closed_at set; others have it NULL.
    n_tickets = get_scaled_count("crm_tickets", scale)
    ticket_types = ["billing_inquiry", "technical_issue", "service_request",
                    "complaint", "activation", "cancellation", "plan_change",
                    "coverage_issue", "roaming_inquiry", "device_support"]
    # Subjects are curated per ticket type for realistic search/NLP testing.
    # In a real CRM, these would be free-text fields entered by customers.
    subjects = {
        "billing_inquiry": ["Unexpected charge on bill", "Payment not reflected", "Request for bill copy"],
        "technical_issue": ["Cannot make calls", "Slow data speed", "No network coverage"],
        "service_request": ["SIM replacement request", "Number porting", "Plan upgrade request"],
        "complaint": ["Poor customer service", "Billing error", "Service quality issue"],
        "activation": ["New line activation", "SIM activation delay", "eSIM setup help"],
        "cancellation": ["Cancel my subscription", "Early termination", "Port out request"],
        "plan_change": ["Upgrade to premium plan", "Downgrade plan", "Add data pack"],
        "coverage_issue": ["No signal at home", "Dropped calls", "Poor indoor coverage"],
        "roaming_inquiry": ["Roaming rates inquiry", "Roaming not working", "Data roaming activation"],
        "device_support": ["Phone not connecting", "Device warranty claim", "Device unlock request"],
    }

    # Status weights reflect a healthy support operation:
    # 50% resolved (most issues get fixed), 20% open (recent/incoming),
    # 15% in_progress, 10% closed (fully concluded), 5% escalated.
    status_map = {
        "open": 20, "in_progress": 15, "resolved": 50, "closed": 10, "escalated": 5,
    }
    statuses = list(status_map.keys())
    status_weights = list(status_map.values())

    ticket_rows = []
    ticket_meta = []
    for _ in range(n_tickets):
        cid = random.choice(customer_ids)
        aid = random.choice(account_ids) if random.random() < 0.8 else None
        sid = random.choice(sub_ids) if random.random() < 0.6 else None
        emp_id = random.choice(employee_ids) if random.random() < 0.85 else None
        ttype = random.choice(ticket_types)
        subject = random.choice(subjects.get(ttype, ["General inquiry"]))
        desc = f"Customer reported: {subject.lower()}. Details pending investigation."
        priority = weighted_choice(["low", "medium", "high", "critical"], [20, 50, 20, 10])
        status = weighted_choice(statuses, status_weights)
        created = random_timestamp(now - timedelta(days=365), now)
        closed = None
        if status in ("resolved", "closed"):
            closed = random_timestamp(created + timedelta(hours=1), min(created + timedelta(days=30), now))

        ticket_rows.append((
            cid, aid, sid, emp_id, ttype, subject, desc, priority,
            status, created, closed, None,  # parent_ticket_id = None for now
        ))
        ticket_meta.append({
            "status": status, "created": created, "closed": closed,
            "emp_id": emp_id,
        })

    ticket_ids = insert_batch_returning(
        cur, "crm_tickets",
        ["customer_id", "account_id", "subscription_id", "assigned_employee_id",
         "ticket_type", "subject", "description", "priority",
         "status", "created_at", "closed_at", "parent_ticket_id"],
        ticket_rows, "ticket_id",
    )
    store.set("ticket_ids", ticket_ids)
    store.set("ticket_meta", list(zip(ticket_ids, ticket_meta)))

    # Link escalated tickets to a parent ticket to model the real-world
    # escalation workflow ("this ticket was escalated from ticket #XYZ").
    # The parent must be a non-escalated ticket to avoid circular references.
    escalated = [i for i, m in enumerate(ticket_meta) if m["status"] == "escalated"]
    non_escalated = [ticket_ids[i] for i, m in enumerate(ticket_meta) if m["status"] != "escalated"]
    for idx in escalated:
        if non_escalated:
            parent = random.choice(non_escalated)
            cur.execute(
                "UPDATE crm_tickets SET parent_ticket_id = %s WHERE ticket_id = %s",
                (parent, ticket_ids[idx]),
            )

    print_table_count(cur, "crm_tickets")

    # -----------------------------------------------------------------------
    # crm_interactions
    # -----------------------------------------------------------------------
    # Each ticket gets 2–8 interactions (customer contacts).
    # The first is always "initial_contact" and the last (for resolved/closed
    # tickets) is "resolution".  Timestamps increase monotonically.
    # old_status/new_status on each interaction optionally records the
    # status change that occurred during that contact.
    interaction_types = ["initial_contact", "status_update", "investigation",
                         "resolution", "follow_up", "escalation", "callback"]
    channels = ["phone", "email", "chat", "sms", "in_store"]

    # Status flow defines the valid state machine transitions.
    # Used to build a logically consistent path from "open" to the ticket’s
    # final status.  For example, a "closed" ticket follows:
    #   open → in_progress → resolved → closed
    status_flow = {
        "open": ["in_progress"],
        "in_progress": ["resolved", "escalated"],
        "escalated": ["in_progress", "resolved"],
        "resolved": ["closed"],
        "closed": [],
    }

    interaction_rows = []
    tsh_rows = []  # ticket_status_history

    for i, tid in enumerate(ticket_ids):
        meta = ticket_meta[i]
        n_interactions = random.randint(2, 8)
        current_time = meta["created"]
        current_status = "open"
        target_status = meta["status"]

        # Build a status path from open to target
        path = [("open", current_status)]  # (None -> open) already exists from creation
        if target_status != "open":
            # Progress through statuses
            intermediate = []
            if target_status in ("resolved", "closed"):
                intermediate = ["in_progress"]
                if target_status == "closed":
                    intermediate.append("resolved")
            elif target_status == "escalated":
                intermediate = ["in_progress", "escalated"]
            elif target_status == "in_progress":
                intermediate = ["in_progress"]

            for ns in intermediate:
                path.append((current_status, ns))
                current_status = ns
            if current_status != target_status:
                path.append((current_status, target_status))

        for j in range(n_interactions):
            itype = interaction_types[min(j, len(interaction_types) - 1)]
            if j == 0:
                itype = "initial_contact"
            elif j == n_interactions - 1 and target_status in ("resolved", "closed"):
                itype = "resolution"

            channel = random.choice(channels)
            int_time = current_time + timedelta(hours=random.randint(1, 48))
            if int_time > now:
                int_time = now
            emp_id = meta["emp_id"] or random.choice(employee_ids)
            notes = f"Interaction #{j + 1}: {itype.replace('_', ' ').title()} via {channel}"

            old_s = None
            new_s = None
            if j < len(path):
                old_s, new_s = path[j] if j > 0 else (None, "open")
                if j > 0 and j < len(path):
                    old_s, new_s = path[j]

            interaction_rows.append((
                tid, emp_id, itype, channel, int_time, notes, old_s, new_s,
            ))
            current_time = int_time

        # ticket_status_history entries
        hist_time = meta["created"]
        for j, (old_s, new_s) in enumerate(path):
            hist_time = hist_time + timedelta(hours=random.randint(1, 24))
            if hist_time > now:
                hist_time = now
            emp_id = meta["emp_id"] or random.choice(employee_ids)
            comment = f"Status changed from {old_s or 'N/A'} to {new_s}"
            tsh_rows.append((tid, old_s, new_s, hist_time, emp_id, comment))

    # Insert interactions and status history in chunks to bound memory at ~1M rows.
    int_cols = ["ticket_id", "employee_id", "interaction_type", "channel",
                "interaction_time", "notes", "old_status", "new_status"]
    for chunk_start in range(0, len(interaction_rows), CHUNK_SIZE):
        chunk = interaction_rows[chunk_start:chunk_start + CHUNK_SIZE]
        insert_batch(cur, "crm_interactions", int_cols, chunk)
    print_table_count(cur, "crm_interactions")

    tsh_cols = ["ticket_id", "old_status", "new_status",
                "changed_at", "changed_by_employee_id", "comment"]
    for chunk_start in range(0, len(tsh_rows), CHUNK_SIZE):
        chunk = tsh_rows[chunk_start:chunk_start + CHUNK_SIZE]
        insert_batch(cur, "ticket_status_history", tsh_cols, chunk)
    print_table_count(cur, "ticket_status_history")

    # -----------------------------------------------------------------------
    # employee_assignments (ticket-related)
    # -----------------------------------------------------------------------
    # ~70% of tickets get a formal employee assignment record.
    # outage_id is NULL here (these are CRM assignments, not network ops).
    # Assignment times fall within the ticket’s created_at → closed_at window.
    # This complements the outage-related assignments from 06_network_events.
    ea_rows = []
    for i, tid in enumerate(ticket_ids):
        meta = ticket_meta[i]
        if random.random() < 0.7:
            emp_id = meta["emp_id"] or random.choice(employee_ids)
            atype = random.choice(["ticket_handling", "escalation_review"])
            start = random_timestamp(meta["created"], meta["created"] + timedelta(hours=2))
            end_t = None
            status = "assigned"
            if meta["closed"]:
                end_t = random_timestamp(start + timedelta(hours=1), meta["closed"])
                status = "completed"
            ea_rows.append((emp_id, tid, None, atype, start, end_t, status))

    insert_batch(cur, "employee_assignments",
                 ["employee_id", "ticket_id", "outage_id", "assignment_type",
                  "start_time", "end_time", "status"],
                 ea_rows)
    print_table_count(cur, "employee_assignments")

    conn.commit()
    print(">> 09_crm done.\n")
