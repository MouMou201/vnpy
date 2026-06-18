"""List objects stored in the Alibaba Cloud OSS bucket.

Credentials are read from environment variables, falling back to the
git-ignored local file ``scripts/oss_credentials.local.json``.

Examples:
    python scripts/list_oss.py                 # list everything
    python scripts/list_oss.py --prefix data/  # list only under data/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import oss2
except ImportError:
    print("Missing dependency 'oss2'. Install it first:\n    pip install oss2")
    sys.exit(1)


DEFAULT_BUCKET = "oss-pai-031vpz46w8hpgwnvap-cn-shanghai"
DEFAULT_ENDPOINT = "https://oss-cn-shanghai.aliyuncs.com"
CREDENTIALS_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "oss_credentials.local.json"
)


def load_credentials() -> tuple[str | None, str | None]:
    """Load credentials from environment, falling back to the local file."""
    key_id = os.environ.get("OSS_ACCESS_KEY_ID")
    key_secret = os.environ.get("OSS_ACCESS_KEY_SECRET")
    if key_id and key_secret:
        return key_id, key_secret

    if CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            return data.get("OSS_ACCESS_KEY_ID"), data.get("OSS_ACCESS_KEY_SECRET")
        except Exception as exc:
            print(f"Failed to read {CREDENTIALS_FILE.name}: {exc}")

    return None, None


def human_size(num_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(description="List objects in an OSS bucket.")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="OSS bucket name.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OSS endpoint URL.")
    parser.add_argument("--cname", action="store_true", help="Treat endpoint as a CNAME domain.")
    parser.add_argument("--prefix", default="", help="Only list keys under this prefix.")
    args = parser.parse_args()

    key_id, key_secret = load_credentials()
    if not key_id or not key_secret:
        print(
            "Missing credentials. Set env vars or create "
            f"{CREDENTIALS_FILE.name} with OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET."
        )
        sys.exit(1)

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, args.endpoint, args.bucket, is_cname=args.cname)

    count = 0
    total_bytes = 0
    try:
        for obj in oss2.ObjectIterator(bucket, prefix=args.prefix):
            count += 1
            total_bytes += obj.size
            mtime = datetime.fromtimestamp(obj.last_modified).strftime("%Y-%m-%d %H:%M:%S")
            print(f"{mtime}  {human_size(obj.size):>10}  {obj.key}")
    except oss2.exceptions.OssError as exc:
        print(f"Failed to list objects: {exc}")
        return

    if count == 0:
        print("(no objects found)")
    else:
        print(f"\nTotal: {count} object(s), {human_size(total_bytes)}")


if __name__ == "__main__":
    main()
