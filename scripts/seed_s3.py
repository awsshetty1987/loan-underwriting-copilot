"""Upload sample_data/ to the S3 bucket (documents, rules, applicant index). Audit records are never uploaded.
Existing objects are skipped unless --overwrite is given, so the bucket's current data is safe by default.

    python scripts/seed_s3.py --bucket loan-bucket2026            # shows what it would do
    python scripts/seed_s3.py --bucket loan-bucket2026 --yes      # uploads new files only
"""
import argparse
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent / "sample_data"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", required=True)
    p.add_argument("--region", default="eu-north-1")
    p.add_argument("--yes", action="store_true", help="really upload (otherwise only list)")
    p.add_argument("--overwrite", action="store_true", help="replace objects that already exist")
    args = p.parse_args()

    s3 = boto3.client("s3", region_name=args.region)
    existing = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=args.bucket):
        existing |= {o["Key"] for o in page.get("Contents", [])}

    files = sorted(f for f in ROOT.rglob("*") if f.is_file() and "/audit/" not in f.as_posix())
    new = skipped = 0
    for f in files:
        key = f.relative_to(ROOT).as_posix()
        if key in existing and not args.overwrite:
            skipped += 1
            continue
        new += 1
        if args.yes:
            s3.upload_file(str(f), args.bucket, key)
        else:
            print("would upload", key)
    action = "uploaded" if args.yes else "to upload (run again with --yes)"
    print(f"{new} {action}, {skipped} already in the bucket and left alone")


if __name__ == "__main__":
    main()
