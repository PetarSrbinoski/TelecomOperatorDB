"""
06_network_events.py - Generate network alarms, outages, and related employee assignments.

This script simulates the operational events that a telecom NOC (Network
Operations Center) would handle:
  - Alarms: automated alerts from tower/sector monitoring systems.
  - Outages: service disruptions that affect customers.
  - Employee assignments: who was dispatched to handle each outage.

Design decisions:
  - Alarm severity distribution is weighted toward minor/warning (65%) with
    only 10% critical, matching real networks where most alarms are informational.
  - ~60% of alarms are cleared (resolved), 40% remain open — gives a realistic
    backlog for NOC dashboard testing.
  - Outage durations range from 30 minutes to 48 hours (2880 min), covering
    both quick fixes and major incidents.
  - affected_customers_count (50–5000) is a rough estimate, not computed from
    actual subscription data, which mirrors how real telecoms estimate impact.
  - Employee assignments are linked to outages here (ticket_id=NULL);
    ticket-linked assignments are created later in 09_crm.py.
  - We use half the total employee_assignments budget here, reserving the
    other half for CRM ticket assignments.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    tower_ids = store.get("tower_ids")
    sector_ids = store.get("sector_ids")
    site_ids = store.get("site_ids")
    employee_ids = store.get("employee_ids")

    # -----------------------------------------------------------------------
    # network_alarms
    # -----------------------------------------------------------------------
    # Alarms simulate real-time monitoring alerts from cell towers and sectors.
    # tower_id is set for 80% of alarms (some alarms are site-level, not
    # tower-specific).  sector_id is set for 60% (some tower-level alarms
    # don’t target a specific sector).  Both are nullable in the DDL.
    n_alarms = get_scaled_count("network_alarms", scale)
    alarm_types = ["high_temperature", "power_failure", "link_down", "high_cpu",
                   "signal_degradation", "hardware_fault", "backhaul_congestion",
                   "antenna_misalignment", "software_error", "battery_low"]
    severities = ["critical", "major", "minor", "warning"]

    alarm_rows = []
    for _ in range(n_alarms):
        tid = random.choice(tower_ids) if random.random() < 0.8 else None
        sid = random.choice(sector_ids) if random.random() < 0.6 else None
        atype = random.choice(alarm_types)
        severity = weighted_choice(severities, [10, 25, 35, 30])
        raised = random_timestamp(now - timedelta(days=365), now)
        cleared = None
        status = "open"
        if random.random() < 0.60:
            cleared = random_timestamp(raised + timedelta(minutes=5), min(raised + timedelta(hours=72), now))
            status = "cleared"
        desc = f"{atype.replace('_', ' ').title()} detected"
        alarm_rows.append((tid, sid, atype, severity, raised, cleared, status, desc))

    alarm_ids = insert_batch_returning(
        cur, "network_alarms",
        ["tower_id", "sector_id", "alarm_type", "severity",
         "raised_at", "cleared_at", "status", "description"],
        alarm_rows, "alarm_id",
    )
    store.set("alarm_ids", alarm_ids)
    # Cache alarm raised timestamps so outages can optionally reference
    # the alarm that triggered them (50% of outages are alarm-linked).
    store.set("alarm_raised_times", [(alarm_ids[i], alarm_rows[i][4]) for i in range(len(alarm_ids))])
    print_table_count(cur, "network_alarms")

    # -----------------------------------------------------------------------
    # outages
    # -----------------------------------------------------------------------
    # Outages represent customer-visible service disruptions.
    # ~50% are linked to an alarm (the alarm that triggered the investigation).
    # ~70% are resolved (end_time set, root_cause documented).
    # Open outages have end_time=NULL, simulating ongoing incidents.
    # outage_meta is stored for employee_assignments to constrain assignment
    # start/end times to fall within the outage’s duration.
    n_outages = get_scaled_count("outages", scale)
    outage_types = ["planned_maintenance", "power_outage", "fiber_cut",
                    "equipment_failure", "software_bug", "weather_damage"]

    outage_rows = []
    outage_meta = []
    for _ in range(n_outages):
        sid = random.choice(site_ids)
        aid = random.choice(alarm_ids) if alarm_ids and random.random() < 0.5 else None
        otype = random.choice(outage_types)
        start = random_timestamp(now - timedelta(days=365), now - timedelta(hours=1))
        duration_mins = random.randint(30, 2880)  # 30min to 48hrs
        end = None
        status = "open"
        affected = random.randint(50, 5000)
        root_cause = None
        if random.random() < 0.70:
            end = start + timedelta(minutes=duration_mins)
            if end > now:
                end = now
            status = "resolved"
            root_cause = random.choice([
                "Power supply failure", "Fiber cable damaged by construction",
                "Software update conflict", "Hardware component failure",
                "Severe weather conditions", "Scheduled maintenance window",
                "DDoS attack on backhaul", "Cooling system malfunction",
            ])
        outage_rows.append((sid, aid, otype, start, end, affected, status, root_cause))
        outage_meta.append({"site_id": sid, "start": start, "end": end or now})

    outage_ids = insert_batch_returning(
        cur, "outages",
        ["site_id", "alarm_id", "outage_type", "start_time", "end_time",
         "affected_customers_count", "status", "root_cause"],
        outage_rows, "outage_id",
    )
    store.set("outage_ids", outage_ids)
    store.set("outage_meta", list(zip(outage_ids, outage_meta)))
    print_table_count(cur, "outages")

    # -----------------------------------------------------------------------
    # employee_assignments (outage-related)
    # -----------------------------------------------------------------------
    # We use half the total employee_assignments budget (//2) for outages,
    # saving the rest for CRM ticket assignments in 09_crm.py.
    # Assignment times are constrained to fall within the outage’s window
    # (start → end) to satisfy the CHECK(end_time >= start_time) constraint.
    # ticket_id is NULL here because these are network operations assignments.
    n_ea = min(get_scaled_count("employee_assignments", scale) // 2, len(outage_ids) * 5)
    ea_rows = []
    for _ in range(n_ea):
        emp_id = random.choice(employee_ids)
        oidx = random.randint(0, len(outage_ids) - 1)
        oid = outage_ids[oidx]
        meta = outage_meta[oidx]
        atype = random.choice(["outage_response", "field_repair", "remote_diagnostics"])
        start = random_timestamp(meta["start"], meta["start"] + timedelta(hours=1))
        end_t = None
        status = "assigned"
        if meta["end"] and random.random() < 0.7:
            end_t = random_timestamp(start + timedelta(minutes=30), meta["end"])
            status = "completed"
        ea_rows.append((emp_id, None, oid, atype, start, end_t, status))

    insert_batch(cur, "employee_assignments",
                 ["employee_id", "ticket_id", "outage_id", "assignment_type",
                  "start_time", "end_time", "status"],
                 ea_rows)
    print_table_count(cur, "employee_assignments")

    conn.commit()
    print(">> 06_network_events done.\n")
