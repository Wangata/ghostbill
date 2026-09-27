# 👻 Ghostbill

**Find the forgotten Azure resources you're still paying for.**

Deleted a VM but not its disk? Tore down a test environment but left the public IP? Those leftovers keep billing every month. Ghostbill finds them across all your subscriptions in seconds and tells you roughly what they cost.

```
$ ghostbill

Unattached managed disks (2)  ~$41.47/mo
  Snapshot if unsure, then delete the disk.
  • data-disk-2  rg-test  westeurope  Standard_LRS 512 GB  $21.76
  • old-vm-osdisk  rg-legacy  eastus  Premium_LRS 128 GB  $19.71

Unassociated public IPs (1)  ~$3.65/mo
  Static public IPs bill hourly even when unused.
  • pip-staging  rg-test  eastus  Standard  $3.65

App Service plans with no apps (1)
  Paid plans bill even with zero apps. Delete or scale to Free.
  • asp-old-site  rg-web  northeurope  S1  varies

5 orphaned resources, est. ~$45.12/month (~$541/year) in known costs.
```

## What it checks

| Check | Costs money? |
|---|---|
| Unattached managed disks | Yes |
| Unassociated public IPs | Yes |
| Disk snapshots older than 30 days | Yes |
| App Service plans with no apps | Yes (depends on SKU) |
| Load balancers with no backend pools | Sometimes |
| Unattached network interfaces | No, clutter |
| Unused network security groups | No, clutter |
| Empty resource groups | No, clutter |

Ghostbill is **read-only**. It never changes or deletes anything.

## Install

You need Python 3.8+ and the [Azure CLI](https://aka.ms/installazurecli).

```bash
pip install .            # from this folder
az login
az extension add --name resource-graph
```

## Usage

```bash
ghostbill                              # scan every subscription you can access
ghostbill scan -s <subscription-id>    # scan one subscription (repeatable)
ghostbill scan --only disks,public_ips # run specific checks
ghostbill scan --snapshot-days 90      # change the snapshot age threshold
ghostbill scan --csv waste.csv         # export to a spreadsheet
ghostbill scan --json                  # machine-readable output
```

Check names for `--only`: `disks`, `public_ips`, `snapshots`, `app_service_plans`, `load_balancers`, `nics`, `nsgs`, `empty_rgs`.

You can also run it without installing: `python -m ghostbill` from this folder.

## Permissions

The **Reader** role on the subscriptions you want to scan is enough.

## About the cost estimates

Estimates use pay-as-you-go list prices for East US. Real prices vary by region, reservations and agreements, so treat them as a way to rank what to clean up first, not as an invoice. Always confirm a resource is unused before deleting it.

## License

MIT
