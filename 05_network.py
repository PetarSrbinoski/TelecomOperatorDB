"""
05_network.py - Generate network infrastructure: sites, towers, sectors, coverage, roaming.

This script builds the physical network topology that CDRs (07_usage_cdrs)
reference to indicate WHERE a call/data session took place.

The hierarchy is:  network_sites → cell_towers → tower_sectors → coverage_zones
  - A site is a physical location (rooftop, field).
  - Each site hosts 1–3 cell towers (antennas on poles/structures).
  - Each tower has 3 sectors (Alpha/Beta/Gamma at 120° apart) to provide
    360° coverage.
  - Each sector has one coverage zone classifying its environment.

Design decisions:
  - Lat/long coordinates are bounded to continental US (25–48°N, 70–125°W)
    for geographic realism.  A global telecom would widen this range.
  - Sector azimuths are 0/120/240° ±10° jitter — this matches real-world
    3-sector antenna configurations.
  - Frequency bands span from 700MHz (rural long-range) to 3500MHz (5G urban)
    reflecting actual spectrum allocations.
  - Roaming partners use real carrier names and real MCC/MNC codes so the
    data looks authentic in reports and dashboards.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from setup import *


def generate(conn, scale: float, store: DataStore):
    cur = conn.cursor()
    today = date.today()
    now = datetime.now()

    technology_ids = store.get("technology_ids")

    # -----------------------------------------------------------------------
    # network_sites
    # -----------------------------------------------------------------------
    # Sites are the top level of the network hierarchy.  site_type reflects
    # deployment strategy: macro (wide coverage), micro (urban fill), small_cell
    # (capacity hotspot), indoor (malls/airports).  Weighted 50/25/15/10%
    # because macro sites dominate most real networks.
    n_sites = get_scaled_count("network_sites", scale)
    used_site_codes = set()

    regions = ["Northeast", "Southeast", "Midwest", "Southwest", "West Coast",
               "Pacific Northwest", "Mountain", "Great Plains", "Mid-Atlantic", "New England"]
    site_types = ["macro", "micro", "small_cell", "indoor"]

    site_rows = []
    for i in range(n_sites):
        code = unique_string("SITE-", 4, used_site_codes)
        name = f"{fake.city()} {random.choice(['Central', 'North', 'South', 'East', 'West', 'Tower', 'Hub'])}"
        address = fake.street_address()[:255]
        region = random.choice(regions)
        lat = round(random.uniform(25.0, 48.0), 6)  # continental US
        lon = round(random.uniform(-125.0, -70.0), 6)
        stype = weighted_choice(site_types, [50, 25, 15, 10])
        status = weighted_choice(["active", "maintenance", "decommissioned"], [85, 10, 5])
        opened = random_timestamp(datetime(2015, 1, 1), now)
        site_rows.append((code, name, address, region, lat, lon, stype, status, opened))

    site_ids = insert_batch_returning(
        cur, "network_sites",
        ["site_code", "site_name", "address", "region", "latitude", "longitude",
         "site_type", "status", "opened_at"],
        site_rows, "site_id",
    )
    store.set("site_ids", site_ids)
    print_table_count(cur, "network_sites")

    # -----------------------------------------------------------------------
    # cell_towers — 1-3 per site
    # -----------------------------------------------------------------------
    # Multiple towers per site represent co-location (carrier sharing) or
    # multi-technology deployments (e.g. one tower for 4G, another for 5G).
    # tower_site_map tracks which site each tower belongs to, used later by
    # 06_network_events when linking alarms to sites via towers.
    used_tower_codes = set()
    vendors = ["Ericsson", "Nokia", "Huawei", "Samsung", "ZTE"]
    ownership_types = ["owned", "leased", "shared"]

    tower_rows = []
    tower_site_map = []  # track which site each tower belongs to
    for sid in site_ids:
        n_towers = random.randint(1, 3)
        for _ in range(n_towers):
            tcode = unique_string("TWR-", 5, used_tower_codes)
            height = round(random.uniform(15, 80), 2)
            own = random.choice(ownership_types)
            inst_date = random_date(date(2015, 1, 1), today)
            status = weighted_choice(["active", "maintenance", "decommissioned"], [85, 10, 5])
            vendor = random.choice(vendors)
            tower_rows.append((sid, tcode, height, own, inst_date, status, vendor))
            tower_site_map.append(sid)

    tower_ids = insert_batch_returning(
        cur, "cell_towers",
        ["site_id", "tower_code", "height_meters", "ownership_type",
         "installation_date", "status", "vendor_name"],
        tower_rows, "tower_id",
    )
    store.set("tower_ids", tower_ids)
    store.set("tower_site_map", list(zip(tower_ids, tower_site_map)))
    print_table_count(cur, "cell_towers")

    # -----------------------------------------------------------------------
    # tower_sectors — 3 sectors per tower (Alpha/Beta/Gamma)
    # -----------------------------------------------------------------------
    # Real cell towers use 3 directional antennas spaced 120° apart to cover
    # 360°.  Each sector can run a different frequency band and technology
    # (e.g. Alpha=LTE@1800MHz, Beta=5G@3500MHz, Gamma=LTE@700MHz).
    # Azimuths have ±10° jitter to simulate real-world installation variance.
    # Beamwidths of 60–120° match common antenna specifications.
    sector_labels = ["Alpha", "Beta", "Gamma"]
    freq_bands = ["700MHz", "850MHz", "1800MHz", "1900MHz", "2100MHz", "2600MHz", "3500MHz"]

    sector_rows = []
    sector_tower_map = []
    for tid in tower_ids:
        for j, label in enumerate(sector_labels):
            azimuth = (j * 120 + random.randint(-10, 10)) % 361
            azimuth = min(azimuth, 360)
            beamwidth = random.choice([60, 65, 90, 120])
            freq = random.choice(freq_bands)
            tech_id = random.choice(technology_ids)
            status = "active"
            sector_rows.append((tid, label, azimuth, beamwidth, freq, tech_id, status))
            sector_tower_map.append(tid)

    sector_ids = insert_batch_returning(
        cur, "tower_sectors",
        ["tower_id", "sector_label", "azimuth", "beamwidth",
         "frequency_band", "technology_id", "status"],
        sector_rows, "sector_id",
    )
    store.set("sector_ids", sector_ids)
    store.set("sector_tower_map", list(zip(sector_ids, sector_tower_map)))
    print_table_count(cur, "tower_sectors")

    # -----------------------------------------------------------------------
    # coverage_zones — one per sector
    # -----------------------------------------------------------------------
    # Classifies the environment each sector serves.  Signal quality scores
    # range 50–100 (lower = weaker coverage).  Weighted toward urban/suburban
    # because those areas have denser deployments.
    coverage_types = ["urban", "suburban", "rural", "highway", "indoor"]
    cz_rows = []
    for sid in sector_ids:
        ctype = weighted_choice(coverage_types, [35, 30, 20, 10, 5])
        desc = f"{ctype.capitalize()} coverage zone"
        quality = round(random.uniform(50, 100), 2)
        measured = random_timestamp(now - timedelta(days=90), now)
        cz_rows.append((sid, ctype, desc, quality, measured))

    insert_batch(cur, "coverage_zones",
                 ["sector_id", "coverage_type", "zone_description",
                  "signal_quality_score", "last_measured_at"],
                 cz_rows)
    print_table_count(cur, "coverage_zones")

    # -----------------------------------------------------------------------
    # roaming_partners
    # -----------------------------------------------------------------------
    # Real carrier names with their actual ITU MCC/MNC codes.  This makes
    # CDR data look authentic when analysts filter by roaming country.
    # Agreement dates span past–future to have a mix of long-standing and
    # recently-signed partnerships.
    partner_defs = [
        ("Vodafone UK", "United Kingdom", "234", "15"),
        ("T-Mobile Germany", "Germany", "262", "01"),
        ("Orange France", "France", "208", "01"),
        ("Movistar Spain", "Spain", "214", "07"),
        ("TIM Italy", "Italy", "222", "01"),
        ("NTT Docomo", "Japan", "440", "10"),
        ("SK Telecom", "South Korea", "450", "05"),
        ("Telstra", "Australia", "505", "01"),
        ("Rogers", "Canada", "302", "720"),
        ("Telcel", "Mexico", "334", "020"),
        ("Claro Brazil", "Brazil", "724", "05"),
        ("Airtel India", "India", "404", "10"),
        ("China Mobile", "China", "460", "00"),
        ("Turkcell", "Turkey", "286", "01"),
        ("Etisalat UAE", "United Arab Emirates", "424", "02"),
        ("MTN South Africa", "South Africa", "655", "10"),
        ("Swisscom", "Switzerland", "228", "01"),
        ("KPN Netherlands", "Netherlands", "204", "08"),
    ]

    rp_rows = []
    for pname, country, mcc, mnc in partner_defs:
        astart = random_date(today - timedelta(days=1825), today - timedelta(days=365))
        aend = random_date(today + timedelta(days=365), today + timedelta(days=1825))
        status = "active"
        rp_rows.append((pname, country, mcc, mnc, astart, aend, status))

    rp_ids = insert_batch_returning(
        cur, "roaming_partners",
        ["partner_name", "country", "mcc", "mnc",
         "agreement_start", "agreement_end", "status"],
        rp_rows, "roaming_partner_id",
    )
    store.set("roaming_partner_ids", rp_ids)
    print_table_count(cur, "roaming_partners")

    conn.commit()
    print(">> 05_network done.\n")
