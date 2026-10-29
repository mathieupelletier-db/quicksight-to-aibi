import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from export_bundle import export  # noqa: E402


def fake(statuses, errors=None):
    calls = []

    def aws_fn(*args):
        calls.append(args)
        if args[1] == "describe-asset-bundle-export-job":
            st = statuses.pop(0)
            return {"JobStatus": st, "DownloadUrl": "https://dl", "Errors": errors or []}
        return {}
    return aws_fn, calls


def test_export_polls_until_success_and_downloads(tmp_path):
    aws_fn, calls = fake(["QUEUED_FOR_IMMEDIATE_EXECUTION", "IN_PROGRESS", "SUCCESSFUL"])
    got = []
    out = export("1", "us-east-1", ["d1"], str(tmp_path / "x" / "b.qs"), aws_fn=aws_fn,
                 download_fn=lambda u, o: got.append((u, o)), sleep=lambda s: None)
    start = calls[0]
    assert "--include-all-dependencies" in start and "QUICKSIGHT_JSON" in start
    assert '"arn:aws:quicksight:us-east-1:1:dashboard/d1"' in start[start.index("--resource-arns") + 1]
    assert got == [("https://dl", out)] and os.path.isdir(tmp_path / "x")


def test_export_failure_surfaces_errors(tmp_path):
    aws_fn, _ = fake(["FAILED"], errors=[{"Message": "AccessDenied on dataset"}])
    with pytest.raises(RuntimeError, match="AccessDenied"):
        export("1", "us-east-1", ["d1"], str(tmp_path / "b.qs"), aws_fn=aws_fn,
               download_fn=lambda u, o: None, sleep=lambda s: None)


from export_bundle import discover, export_many  # noqa: E402


def lister(pages):
    calls = []

    def aws_fn(*args):
        calls.append(args)
        assert args[1] == "list-dashboards"
        return pages.pop(0)
    return aws_fn, calls


def test_discover_pages_and_filters_by_name():
    aws_fn, calls = lister([
        {"DashboardSummaryList": [{"DashboardId": "a", "Name": "Sales Overview"}], "NextToken": "t1"},
        {"DashboardSummaryList": [{"DashboardId": "b", "Name": "HR"}, {"DashboardId": "c", "Name": "sales by store"}]}])
    assert discover("1", "us-east-1", name_like="sales", aws_fn=aws_fn) == ["a", "c"]
    assert "--next-token" in calls[1] and "t1" in calls[1]


def test_export_many_batches_and_names_files(tmp_path):
    started = []

    def aws_fn(*args):
        if args[1] == "start-asset-bundle-export-job":
            started.append(json.loads(args[args.index("--resource-arns") + 1]))
            return {}
        return {"JobStatus": "SUCCESSFUL", "DownloadUrl": "https://dl"}
    outs = export_many("1", "us-east-1", [f"d{i}" for i in range(5)], str(tmp_path), batch_size=2,
                       aws_fn=aws_fn, download_fn=lambda u, o: open(o, "w").close(), sleep=lambda s: None)
    assert [len(b) for b in started] == [2, 2, 1]
    assert [os.path.basename(o) for o in outs] == ["bundle-001.qs", "bundle-002.qs", "bundle-003.qs"]


def test_profile_is_passed_to_aws_cli(monkeypatch):
    import export_bundle as eb
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "{}", ""

    monkeypatch.setattr(eb.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and R())
    eb.aws("quicksight", "list-dashboards", profile="sandbox")
    assert seen["cmd"][-2:] == ["--profile", "sandbox"] or "--profile" in seen["cmd"]
