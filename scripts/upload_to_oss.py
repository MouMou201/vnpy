"""Upload local CSV data files to Alibaba Cloud OSS.

Credentials are read from environment variables (never hard-code secrets):
    export OSS_ACCESS_KEY_ID=...
    export OSS_ACCESS_KEY_SECRET=...

Examples:
    # Upload all CSVs under data/ to the bucket under prefix "data/"
    python scripts/upload_to_oss.py

    # Upload specific files
    python scripts/upload_to_oss.py --files data/stock_a_info.csv

    # Use the CNAME endpoint if the default public endpoint is blocked
    python scripts/upload_to_oss.py --endpoint https://cn-shanghai.taihangcda.cn --cname
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import oss2
except ImportError:
    print("Missing dependency 'oss2'. Install it first:\n    pip install oss2")
    sys.exit(1)


DEFAULT_BUCKET = "oss-pai-031vpz46w8hpgwnvap-cn-shanghai"
DEFAULT_ENDPOINT = "https://oss-cn-shanghai.aliyuncs.com"

# Git-ignored local credentials file (see .gitignore: *.local.json).
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


def get_bucket(bucket_name: str, endpoint: str, is_cname: bool) -> "oss2.Bucket":
    """Create an authenticated OSS bucket client."""
    key_id, key_secret = load_credentials()
    if not key_id or not key_secret:
        print(
            "Missing credentials. Set environment variables:\n"
            "    export OSS_ACCESS_KEY_ID=...\n"
            "    export OSS_ACCESS_KEY_SECRET=...\n"
            f"or create {CREDENTIALS_FILE.name} with those two keys."
        )
        sys.exit(1)

    auth = oss2.Auth(key_id, key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name, is_cname=is_cname)


def collect_files(files: list[str], data_dir: Path) -> list[Path]:
    """Resolve the list of local files to upload."""
    if files:
        return [Path(f).resolve() for f in files]
    return sorted(data_dir.glob("*.csv"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Upload CSV files to Alibaba Cloud OSS.")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="OSS bucket name.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OSS endpoint URL.")
    parser.add_argument(
        "--cname",
        action="store_true",
        help="Treat endpoint as a custom CNAME domain.",
    )
    parser.add_argument(
        "--prefix",
        default="data/",
        help="Key prefix (remote folder) for uploaded objects.",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="Specific local files to upload. Defaults to all CSVs under data/.",
    )
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parents[1] / "data"
    local_files = collect_files(args.files or [], data_dir)
    if not local_files:
        print(f"No files to upload (looked in {data_dir}).")
        return

    bucket = get_bucket(args.bucket, args.endpoint, args.cname)
    prefix = args.prefix.strip("/")

    for path in local_files:
        if not path.exists():
            print(f"Skip (not found): {path}")
            continue

        key = f"{prefix}/{path.name}" if prefix else path.name
        try:
            bucket.put_object_from_file(key, str(path))
            size_kb = path.stat().st_size / 1024
            print(f"Uploaded {path.name} -> oss://{args.bucket}/{key} ({size_kb:.1f} KB)")
        except oss2.exceptions.OssError as exc:
            print(
                f"Failed to upload {path.name}: {exc}\n"
                "If this is an endpoint/policy error on a new bucket (rule since "
                "2025-03-20), retry with the CNAME endpoint:\n"
                "    python scripts/upload_to_oss.py "
                "--endpoint https://cn-shanghai.taihangcda.cn --cname"
            )
            return

    print("Done.")


if __name__ == "__main__":
    main()
