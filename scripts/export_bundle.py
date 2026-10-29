"""Export QuickSight dashboards as asset bundles (.qs, QUICKSIGHT_JSON, all dependencies).

Runs with the caller's AWS CLI v2 credentials (needs quicksight:ListDashboards,
StartAssetBundleExportJob, DescribeAssetBundleExportJob and read access to every exported asset:
dashboards, datasets, data sources, themes, topics). Bundles hold definitions only: no data and
no data-source passwords.

    # specific dashboards -> one bundle
    python3 export_bundle.py --account-id 123456789012 --region us-east-1 \
        --dashboard-id dash-1 --dashboard-id dash-2 -o bundles/sales.qs
    # discover and export in batches -> bundles/bundle-001.qs, bundle-002.qs, ...
    python3 export_bundle.py --account-id 123456789012 --region us-east-1 --profile qs-readonly \
        --all [--name-like sales] --batch-size 20 -o bundles/
"""
import argparse
import json
import os
import subprocess
import time
import urllib.request

TERMINAL = {"SUCCESSFUL", "FAILED"}


def aws(*args: str, profile: str | None = None) -> dict:
    cmd = ["aws", *args, "--output", "json"] + (["--profile", profile] if profile else [])
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {out.stderr.strip()}")
    return json.loads(out.stdout or "{}")


def download(url: str, out: str) -> None:
    with urllib.request.urlopen(url) as resp, open(out, "wb") as f:
        f.write(resp.read())


def discover(account_id, region, name_like=None, aws_fn=aws) -> list[str]:
    """All dashboard ids in the account/region, optionally filtered by a case-insensitive name substring."""
    ids, token = [], None
    while True:
        extra = ["--next-token", token] if token else []
        page = aws_fn("quicksight", "list-dashboards", "--aws-account-id", account_id, "--region", region, *extra)
        for d in page.get("DashboardSummaryList", []):
            if not name_like or name_like.lower() in (d.get("Name") or "").lower():
                ids.append(d["DashboardId"])
        token = page.get("NextToken")
        if not token:
            return ids


def export(account_id, region, dashboard_ids, out, aws_fn=aws, download_fn=download,
           sleep=time.sleep, poll_seconds=5, timeout_seconds=900) -> str:
    job_id = f"qs2aibi-{int(time.time() * 1000)}"
    arns = [f"arn:aws:quicksight:{region}:{account_id}:dashboard/{d}" for d in dashboard_ids]
    aws_fn("quicksight", "start-asset-bundle-export-job", "--aws-account-id", account_id,
           "--region", region, "--asset-bundle-export-job-id", job_id,
           "--resource-arns", json.dumps(arns), "--include-all-dependencies",
           "--export-format", "QUICKSIGHT_JSON")
    waited = 0
    while True:
        job = aws_fn("quicksight", "describe-asset-bundle-export-job", "--aws-account-id", account_id,
                     "--region", region, "--asset-bundle-export-job-id", job_id)
        status = job.get("JobStatus")
        if status in TERMINAL:
            break
        if waited >= timeout_seconds:
            raise TimeoutError(f"export job {job_id} still {status} after {timeout_seconds}s")
        sleep(poll_seconds)
        waited += poll_seconds
    if status == "FAILED":
        raise RuntimeError(f"export job {job_id} failed: {json.dumps(job.get('Errors', []))}")
    for w in job.get("Warnings", []):
        print(f"WARNING {w}")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    download_fn(job["DownloadUrl"], out)
    return out


def export_many(account_id, region, dashboard_ids, out_dir, batch_size=20, **kw) -> list[str]:
    """Export in batches so one bad asset or a large estate doesn't sink a single huge job."""
    outs = []
    for i in range(0, len(dashboard_ids), batch_size):
        n = i // batch_size + 1
        outs.append(export(account_id, region, dashboard_ids[i:i + batch_size],
                           os.path.join(out_dir, f"bundle-{n:03d}.qs"), **kw))
    return outs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--account-id", required=True)
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--profile", help="AWS CLI profile (default: the standard credential chain)")
    ap.add_argument("--dashboard-id", action="append", default=[])
    ap.add_argument("--all", action="store_true", help="discover dashboards with list-dashboards")
    ap.add_argument("--name-like", help="with --all: only dashboards whose name contains this text")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("-o", "--out", default="bundle.qs", help="file for --dashboard-id, folder for --all")
    a = ap.parse_args()

    def aws_fn(*args):
        return aws(*args, profile=a.profile)

    if a.all:
        ids = discover(a.account_id, a.region, a.name_like, aws_fn=aws_fn)
        print(f"found {len(ids)} dashboard(s)")
        for o in export_many(a.account_id, a.region, ids, a.out, a.batch_size, aws_fn=aws_fn):
            print(f"wrote {o}")
    elif a.dashboard_id:
        print(f"wrote {export(a.account_id, a.region, a.dashboard_id, a.out, aws_fn=aws_fn)}")
    else:
        ap.error("pass --dashboard-id (repeatable) or --all")


if __name__ == "__main__":
    main()
