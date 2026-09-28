import json

import pytest

from ghostbill import cli


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_one_line_collapses_whitespace():
    query = """
    Resources
    | where type =~ 'microsoft.compute/disks'
    """
    assert cli.one_line(query) == "Resources | where type =~ 'microsoft.compute/disks'"


@pytest.mark.parametrize("value, expected", [
    (None, "varies"),
    (0, "free"),
    (0.0, "free"),
    (12.5, "$12.50"),
    (1234.5, "$1,234.50"),
])
def test_money(value, expected):
    assert cli.money(value) == expected


def test_estimate_disks_known_sku():
    assert cli.estimate("disks", {"sku": "Premium_LRS", "sizeGb": 128}) == 19.71


def test_estimate_disks_zrs_multiplier():
    row = {"sku": "Premium_ZRS", "sizeGb": 128}
    assert cli.estimate("disks", row) == round(19.71 * 1.5, 2)


def test_estimate_disks_unknown_sku_returns_none():
    assert cli.estimate("disks", {"sku": "UltraSSD_LRS", "sizeGb": 128}) is None


def test_estimate_disks_beyond_largest_tier_scales_linearly():
    row = {"sku": "Standard_LRS", "sizeGb": 8192}
    expected = round(163.84 * (8192 / 4096), 2)
    assert cli.estimate("disks", row) == expected


def test_estimate_snapshots():
    assert cli.estimate("snapshots", {"sizeGb": 100}) == 5.0
    assert cli.estimate("snapshots", {"sizeGb": 0}) is None


def test_estimate_public_ips_flat_rate():
    assert cli.estimate("public_ips", {}) == cli.PUBLIC_IP_MONTHLY


@pytest.mark.parametrize("check_id", ["nics", "nsgs", "empty_rgs"])
def test_estimate_free_checks(check_id):
    assert cli.estimate(check_id, {}) == 0.0


@pytest.mark.parametrize("check_id", ["app_service_plans", "load_balancers"])
def test_estimate_varies_checks(check_id):
    assert cli.estimate(check_id, {}) is None


# ---------------------------------------------------------------------------
# Ignore tags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tags", [
    {"ghostbill-ignore": "true"},
    {"Ghostbill-Ignore": "True"},
    {"ghostbill-ignore": "1"},
    {"ghostbill-ignore": "yes"},
    {"ghostbill-ignore": " TRUE "},
])
def test_is_ignored_matches_truthy_values(tags):
    assert cli.is_ignored({"tags": tags}, "ghostbill-ignore") is True


@pytest.mark.parametrize("row", [
    {},
    {"tags": None},
    {"tags": {}},
    {"tags": {"ghostbill-ignore": "false"}},
    {"tags": {"other-tag": "true"}},
])
def test_is_ignored_false_cases(row):
    assert cli.is_ignored(row, "ghostbill-ignore") is False


def test_is_ignored_custom_tag_key():
    row = {"tags": {"do-not-report": "true"}}
    assert cli.is_ignored(row, "do-not-report") is True
    assert cli.is_ignored(row, "ghostbill-ignore") is False


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_write_csv(tmp_path, capsys):
    findings = [{
        "label": "Unattached managed disks",
        "rows": [{
            "name": "disk-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
            "location": "eastus", "sku": "Premium_LRS", "sizeGb": 128,
            "estMonthlyUsd": 19.71, "id": "/subscriptions/sub1/disk-1",
        }],
    }]
    out = tmp_path / "waste.csv"
    cli.write_csv(str(out), findings)

    content = out.read_text(encoding="utf-8").splitlines()
    assert content[0] == "check,name,resourceGroup,subscriptionId,location,sku,sizeGb,estMonthlyUsd,id"
    assert "disk-1" in content[1]
    assert f"Wrote {out}" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# preflight() (az CLI mocked out)
# ---------------------------------------------------------------------------

def test_preflight_missing_az(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        cli.preflight()


def test_preflight_not_logged_in(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(cli, "az", lambda args: (_ for _ in ()).throw(RuntimeError("not logged in")))
    with pytest.raises(SystemExit):
        cli.preflight()


def test_preflight_missing_extension(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/az")

    def fake_az(args):
        if args[:2] == ["account", "show"]:
            return {}
        raise RuntimeError("extension missing")

    monkeypatch.setattr(cli, "az", fake_az)
    with pytest.raises(SystemExit):
        cli.preflight()


def test_preflight_success(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(cli, "az", lambda args: {})
    cli.preflight()
    assert cli.AZ_PATH == "/usr/bin/az"


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_preflight(monkeypatch):
    monkeypatch.setattr(cli, "preflight", lambda: None)


def test_main_only_invalid_check_exits(stub_preflight):
    with pytest.raises(SystemExit):
        cli.main(["scan", "--only", "not-a-real-check"])


def test_main_json_reports_estimate(stub_preflight, monkeypatch, capsys):
    rows = [{
        "checkId": "disks", "name": "disk-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
        "location": "eastus", "id": "/subscriptions/sub1/disk-1",
        "sku": "Premium_LRS", "sizeGb": 128, "tags": {},
    }]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)

    cli.main(["scan", "--only", "disks", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out[0]["id"] == "disks"
    assert out[0]["rows"][0]["estMonthlyUsd"] == 19.71
    assert "tags" not in out[0]["rows"][0]
    assert "checkId" not in out[0]["rows"][0]


def test_main_skips_ignored_resources(stub_preflight, monkeypatch, capsys):
    rows = [
        {"checkId": "disks", "name": "keep-me", "resourceGroup": "rg1", "subscriptionId": "sub1",
         "location": "eastus", "id": "id-1", "sku": "Premium_LRS", "sizeGb": 128,
         "tags": {}},
        {"checkId": "disks", "name": "ignore-me", "resourceGroup": "rg1", "subscriptionId": "sub1",
         "location": "eastus", "id": "id-2", "sku": "Premium_LRS", "sizeGb": 128,
         "tags": {"ghostbill-ignore": "true"}},
    ]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)

    cli.main(["scan", "--only", "disks", "--json"])

    out = json.loads(capsys.readouterr().out)
    names = [r["name"] for r in out[0]["rows"]]
    assert names == ["keep-me"]


def test_main_custom_ignore_tag(stub_preflight, monkeypatch, capsys):
    rows = [{
        "checkId": "nics", "name": "res-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
        "location": "eastus", "id": "id-1", "sku": "", "sizeGb": 0,
        "tags": {"do-not-report": "true"},
    }]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)

    cli.main(["scan", "--only", "nics", "--ignore-tag", "do-not-report", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out[0]["rows"] == []


def test_main_writes_csv(stub_preflight, monkeypatch, tmp_path):
    rows = [{
        "checkId": "disks", "name": "disk-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
        "location": "eastus", "id": "id-1", "sku": "Premium_LRS", "sizeGb": 128,
        "tags": {},
    }]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)
    out_path = tmp_path / "out.csv"

    cli.main(["scan", "--only", "disks", "--json", "--csv", str(out_path)])

    assert out_path.exists()
    assert "disk-1" in out_path.read_text(encoding="utf-8")


def test_main_groups_rows_by_check_id(stub_preflight, monkeypatch, capsys):
    rows = [
        {"checkId": "disks", "name": "disk-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
         "location": "eastus", "id": "id-1", "sku": "Premium_LRS", "sizeGb": 128, "tags": {}},
        {"checkId": "public_ips", "name": "ip-1", "resourceGroup": "rg1", "subscriptionId": "sub1",
         "location": "eastus", "id": "id-2", "sku": "Standard", "sizeGb": 0, "tags": {}},
    ]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)

    cli.main(["scan", "--only", "disks,public_ips", "--json"])

    out = json.loads(capsys.readouterr().out)
    by_id = {f["id"]: [r["name"] for r in f["rows"]] for f in out}
    assert by_id == {"disks": ["disk-1"], "public_ips": ["ip-1"]}


def test_main_ignores_rows_with_unknown_check_id(stub_preflight, monkeypatch, capsys):
    rows = [{"checkId": "some_future_check", "name": "x", "tags": {}}]
    monkeypatch.setattr(cli, "run_query", lambda query, subs: rows)

    cli.main(["scan", "--only", "disks", "--json"])

    out = json.loads(capsys.readouterr().out)
    assert out[0]["rows"] == []


def test_main_exits_when_query_fails(stub_preflight, monkeypatch):
    def boom(query, subs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_query", boom)
    with pytest.raises(SystemExit):
        cli.main(["scan", "--only", "disks"])


# ---------------------------------------------------------------------------
# build_query()
# ---------------------------------------------------------------------------

def test_build_query_unions_checks_with_check_id():
    checks = [ch for ch in cli.CHECKS if ch["id"] in ("disks", "public_ips")]
    query = cli.build_query(checks, 30)

    assert query.startswith("union (")
    assert "checkId = 'disks'" in query
    assert "checkId = 'public_ips'" in query
    assert "project checkId, name, resourceGroup" in query
    assert query.count("(") == query.count(")")


def test_build_query_substitutes_snapshot_days():
    checks = [ch for ch in cli.CHECKS if ch["id"] == "snapshots"]
    query = cli.build_query(checks, 90)

    assert "ago(90d)" in query
    assert "{days}" not in query


def test_build_query_single_check_is_valid_union():
    checks = [ch for ch in cli.CHECKS if ch["id"] == "disks"]
    query = cli.build_query(checks, 30)

    assert query.count("checkId = 'disks'") == 1
    assert query.count("(") == query.count(")")
