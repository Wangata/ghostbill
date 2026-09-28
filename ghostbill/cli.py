"""
Ghostbill - find the forgotten Azure resources you're still paying for.

Uses the Azure CLI you're already logged into (az login) and Azure Resource
Graph, so it scans every subscription you can see in a few seconds.
No Python dependencies.

Usage:
    ghostbill                          # scan all subscriptions
    ghostbill scan -s <sub-id>         # scan specific subscriptions
    ghostbill scan --snapshot-days 60  # snapshots older than 60 days
    ghostbill scan --json              # machine-readable output
    ghostbill scan --csv waste.csv     # export for a spreadsheet

Tag a resource with ghostbill-ignore=true to keep it out of the report.
"""
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys

from . import __version__

IS_WINDOWS = os.name == "nt"
AZ_PATH = None

# Common columns every check projects, so output is uniform. checkId tells
# rows apart once all checks are unioned into a single query (see build_query).
COLUMNS = "| project checkId, name, resourceGroup, subscriptionId, location, id, sku, sizeGb, tags"

DEFAULT_IGNORE_TAG = "ghostbill-ignore"

# Each check's `source` is a standalone KQL statement; build_query() unions
# them into one query per scan instead of one Azure CLI call per check.
CHECKS = [
    {
        "id": "disks",
        "label": "Unattached managed disks",
        "tip": "Snapshot if unsure, then delete the disk.",
        "source": """
Resources
| where type =~ 'microsoft.compute/disks'
| where properties.diskState =~ 'Unattached'
| extend sku = tostring(sku.name), sizeGb = toint(properties.diskSizeGB)
""",
    },
    {
        "id": "public_ips",
        "label": "Unassociated public IPs",
        "tip": "Static public IPs bill hourly even when unused.",
        "source": """
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| where isnull(properties.ipConfiguration) and isnull(properties.natGateway)
| extend sku = tostring(sku.name), sizeGb = 0
""",
    },
    {
        "id": "snapshots",
        "label": "Old disk snapshots",
        "tip": "Check nothing depends on them, then delete.",
        "source": """
Resources
| where type =~ 'microsoft.compute/snapshots'
| where todatetime(properties.timeCreated) < ago({days}d)
| extend sku = tostring(sku.name), sizeGb = toint(properties.diskSizeGB)
""",
    },
    {
        "id": "app_service_plans",
        "label": "App Service plans with no apps",
        "tip": "Paid plans bill even with zero apps. Delete or scale to Free.",
        "source": """
Resources
| where type =~ 'microsoft.web/serverfarms'
| where toint(properties.numberOfSites) == 0
| extend sku = tostring(sku.name), sizeGb = 0
""",
    },
    {
        "id": "load_balancers",
        "label": "Load balancers with no backend pools",
        "tip": "Usually left behind after a VM or cluster was removed.",
        "source": """
Resources
| where type =~ 'microsoft.network/loadbalancers'
| where array_length(coalesce(properties.backendAddressPools, dynamic([]))) == 0
| extend sku = tostring(sku.name), sizeGb = 0
""",
    },
    {
        "id": "nics",
        "label": "Unattached network interfaces",
        "tip": "Free, but clutter. Safe to remove.",
        "source": """
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| where isnull(properties.virtualMachine) and isnull(properties.privateEndpoint)
    and isnull(properties.privateLinkService)
| extend sku = '', sizeGb = 0
""",
    },
    {
        "id": "nsgs",
        "label": "Unused network security groups",
        "tip": "Free, but clutter. Safe to remove.",
        "source": """
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| where array_length(coalesce(properties.networkInterfaces, dynamic([]))) == 0
    and array_length(coalesce(properties.subnets, dynamic([]))) == 0
| extend sku = '', sizeGb = 0
""",
    },
    {
        "id": "empty_rgs",
        "label": "Empty resource groups",
        "tip": "Free, but clutter. Safe to remove.",
        "source": """
ResourceContainers
| where type =~ 'microsoft.resources/subscriptions/resourcegroups'
| extend rgKey = tolower(strcat(subscriptionId, '/', name))
| join kind=leftouter (
    Resources
    | extend rgKey = tolower(strcat(subscriptionId, '/', resourceGroup))
    | summarize resourceCount = count() by rgKey
) on rgKey
| where isnull(resourceCount) or resourceCount == 0
| extend resourceGroup = name, sku = '', sizeGb = 0
""",
    },
]

# --- Rough monthly cost estimates (USD, pay-as-you-go list prices, East US) ---
# Good enough to rank what matters. Real prices vary by region and agreement.
DISK_TIERS_GB = [32, 64, 128, 256, 512, 1024, 2048, 4096]
DISK_PRICES = {
    "premium_lrs": [5.28, 10.21, 19.71, 38.01, 73.22, 135.17, 259.05, 495.57],
    "standardssd_lrs": [2.40, 4.80, 9.60, 19.20, 38.40, 76.80, 153.60, 307.20],
    "standard_lrs": [1.54, 3.01, 5.89, 11.33, 21.76, 40.96, 81.92, 163.84],
}
ZRS_MULTIPLIER = {"premium_zrs": ("premium_lrs", 1.5), "standardssd_zrs": ("standardssd_lrs", 1.25)}
SNAPSHOT_PER_GB = 0.05
PUBLIC_IP_MONTHLY = 3.65


def estimate(check_id, row):
    """Return an estimated monthly cost in USD, or None if we can't guess."""
    sku = (row.get("sku") or "").lower()
    size = row.get("sizeGb") or 0
    if check_id == "disks":
        base, mult = ZRS_MULTIPLIER.get(sku, (sku, 1.0))
        prices = DISK_PRICES.get(base)
        if not prices or not size:
            return None
        for tier, price in zip(DISK_TIERS_GB, prices):
            if size <= tier:
                return round(price * mult, 2)
        return round(prices[-1] * mult * (size / DISK_TIERS_GB[-1]), 2)
    if check_id == "snapshots":
        return round(size * SNAPSHOT_PER_GB, 2) if size else None
    if check_id == "public_ips":
        return PUBLIC_IP_MONTHLY
    if check_id in ("nics", "nsgs", "empty_rgs"):
        return 0.0
    return None  # App Service plans / load balancers: depends on SKU & rules


def is_ignored(row, tag_key):
    """True if the resource carries an ignore tag, e.g. ghostbill-ignore=true."""
    tags = row.get("tags")
    if not isinstance(tags, dict):
        return False
    for k, v in tags.items():
        if k.lower() == tag_key.lower() and str(v).strip().lower() in ("true", "1", "yes"):
            return True
    return False


# --- Azure CLI plumbing ---
def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def az(args):
    # On Windows `az` is a .cmd batch file, so we call it by full path and keep
    # every argument on one line (newlines break batch-file argument parsing).
    proc = subprocess.run([AZ_PATH, *args, "-o", "json"], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "az command failed")
    return json.loads(proc.stdout or "null")


def preflight():
    global AZ_PATH
    AZ_PATH = shutil.which("az")
    if not AZ_PATH:
        fail("Azure CLI not found. Install it: https://aka.ms/installazurecli")
    try:
        az(["account", "show"])
    except RuntimeError:
        fail("Not logged in. Run: az login")
    try:
        az(["extension", "show", "--name", "resource-graph"])
    except RuntimeError:
        fail("Missing the Resource Graph extension. Run: az extension add --name resource-graph")


def one_line(query):
    return re.sub(r"\s+", " ", query).strip()


def build_query(checks, snapshot_days):
    """Union every check's source into a single query, tagged with checkId
    so rows can be split back out per check after the scan."""
    parts = []
    for ch in checks:
        src = one_line(ch["source"].replace("{days}", str(snapshot_days)))
        parts.append(f"({src} | extend checkId = '{ch['id']}')")
    return "union " + ", ".join(parts) + " " + COLUMNS


def run_query(query, subscriptions):
    rows, skip = [], None
    while True:
        args = ["graph", "query", "-q", one_line(query), "--first", "1000"]
        if subscriptions:
            args += ["--subscriptions", *subscriptions]
        if skip:
            args += ["--skip-token", skip]
        result = az(args)
        rows.extend(result.get("data", []))
        skip = result.get("skip_token") or result.get("skipToken")
        if not skip:
            return rows


# --- Output ---
def _setup_console():
    """Enable colors and Unicode on Windows consoles; plain text when piped."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if IS_WINDOWS:
        os.system("")  # turns on ANSI escape handling in cmd/PowerShell
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


USE_COLOR = _setup_console()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def money(v):
    if v is None:
        return "varies"
    return "free" if v == 0 else f"${v:,.2f}"


def print_report(findings):
    total_items = sum(len(f["rows"]) for f in findings)
    if total_items == 0:
        print(c("✔ No orphaned resources found. Nice and tidy.", "32"))
        return

    grand = 0.0
    for f in findings:
        if not f["rows"]:
            continue
        subtotal = sum(r["estMonthlyUsd"] or 0 for r in f["rows"])
        grand += subtotal
        header = f"{f['label']} ({len(f['rows'])})"
        cost = f"  ~{money(subtotal)}/mo" if subtotal else ""
        print("\n" + c(header, "1") + c(cost, "33"))
        print(c(f"  {f['tip']}", "2"))
        for r in sorted(f["rows"], key=lambda r: -(r["estMonthlyUsd"] or 0)):
            detail = " ".join(x for x in [r.get("sku") or "", f"{r['sizeGb']} GB" if r.get("sizeGb") else ""] if x)
            print(f"  • {r['name']}  {c(r['resourceGroup'], '36')}  {r['location']}"
                  f"  {detail}  {c(money(r['estMonthlyUsd']), '33')}")

    print("\n" + c(f"{total_items} orphaned resources, est. ~${grand:,.2f}/month "
                   f"(~${grand * 12:,.0f}/year) in known costs.", "1"))
    print(c("Estimates use East US list prices. Always confirm before deleting.", "2"))


def write_csv(path, findings):
    fields = ["check", "name", "resourceGroup", "subscriptionId", "location", "sku",
              "sizeGb", "estMonthlyUsd", "id"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for f in findings:
            for r in f["rows"]:
                w.writerow({"check": f["label"], **r})
    print(f"Wrote {path}")


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="ghostbill",
        description="Find the forgotten Azure resources you're still paying for.")
    p.add_argument("--version", action="version", version=f"ghostbill {__version__}")
    sub = p.add_subparsers(dest="command")
    scan = sub.add_parser("scan", help="Scan subscriptions for orphaned resources (default).")
    for parser in (p, scan):
        parser.add_argument("-s", "--subscription", action="append", default=[],
                            help="Subscription ID to scan (repeatable). Default: all you can access.")
        parser.add_argument("--snapshot-days", type=int, default=30,
                            help="Flag snapshots older than this many days (default 30).")
        parser.add_argument("--only", help="Comma-separated checks: " + ",".join(ch["id"] for ch in CHECKS))
        parser.add_argument("--ignore-tag", default=DEFAULT_IGNORE_TAG, metavar="KEY",
                            help="Tag key that excludes a resource when set to true "
                                 f"(default: {DEFAULT_IGNORE_TAG}).")
        parser.add_argument("--json", action="store_true", help="Print JSON instead of a report.")
        parser.add_argument("--csv", metavar="FILE", help="Also write results to a CSV file.")
    a = p.parse_args(argv)

    checks = CHECKS
    if a.only:
        wanted = {x.strip() for x in a.only.split(",")}
        checks = [ch for ch in CHECKS if ch["id"] in wanted]
        if not checks:
            fail("--only matched no checks")

    preflight()

    if not a.json:
        noun = "check" if len(checks) == 1 else "checks"
        print(c(f"Scanning: {len(checks)} {noun} in one query…", "2"), file=sys.stderr)

    try:
        rows = run_query(build_query(checks, a.snapshot_days), a.subscription)
    except RuntimeError as e:
        fail(f"scan failed: {e}")

    by_check = {ch["id"]: [] for ch in checks}
    for r in rows:
        check_id = r.pop("checkId", None)
        if check_id in by_check:
            by_check[check_id].append(r)

    findings = []
    for ch in checks:
        kept, ignored = [], 0
        for r in by_check[ch["id"]]:
            if is_ignored(r, a.ignore_tag):
                ignored += 1
                continue
            r.pop("tags", None)
            r["estMonthlyUsd"] = estimate(ch["id"], r)
            kept.append(r)
        if ignored and not a.json:
            print(c(f"  {ch['label']}: {ignored} ignored via '{a.ignore_tag}' tag", "2"), file=sys.stderr)
        findings.append({"id": ch["id"], "label": ch["label"], "tip": ch["tip"], "rows": kept})

    if a.json:
        print(json.dumps(findings, indent=2))
    else:
        print_report(findings)
    if a.csv:
        write_csv(a.csv, findings)

