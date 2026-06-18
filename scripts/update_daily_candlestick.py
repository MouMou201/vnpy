"""Incrementally update daily K-line CSVs for many stocks directly on OSS.

For every unique symbol found in a source CSV (default:
``data/stock_a_info.csv``, the full A-share universe), this script:

1. Downloads the existing per-stock CSV from OSS (``data/a_stock/<symbol>.csv``).
2. Fetches only the new daily bars since the last stored date.
3. Merges + de-duplicates + sorts, and uploads the result back to OSS.

Nothing is written to the local disk. Work is parallelized across stocks.

Notes on price adjustment:
- Default is "" (raw / 不复权), which is append-safe: historical values never
  change, so incremental appends stay consistent.
- "qfq" (前复权) is NOT append-safe: every new dividend rewrites all historical
  values, so mixing old and new rows becomes inconsistent. Avoid for updates.

Examples:
    # Whole market (initial build or daily update) from data/stock_a_info.csv
    python scripts/update_daily_candlestick.py --workers 16

    # Try a small batch first
    python scripts/update_daily_candlestick.py --limit 20

    # Specific symbols
    python scripts/update_daily_candlestick.py --symbols 300285.SZSE 600519.SSE
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

import pandas as pd

try:
    import oss2
except ImportError:
    print("Missing dependency 'oss2'. Install it first:\n    pip install oss2")
    sys.exit(1)

# Make sibling scripts importable when run as `python scripts/update_daily_candlestick.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from get_daily_candlestick import build_daily_candlestick  # noqa: E402
from upload_to_oss import (  # noqa: E402
    DEFAULT_BUCKET,
    DEFAULT_ENDPOINT,
    load_credentials,
)

BASE_START_DATE = "19900101"


def make_bucket(endpoint: str, bucket_name: str, is_cname: bool) -> "oss2.Bucket":
    """Create an authenticated OSS bucket client."""
    key_id, key_secret = load_credentials()
    if not key_id or not key_secret:
        print(
            "Missing credentials. Set OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET "
            "or create scripts/oss_credentials.local.json."
        )
        sys.exit(1)
    auth = oss2.Auth(key_id, key_secret)
    return oss2.Bucket(auth, endpoint, bucket_name, is_cname=is_cname)


def read_symbols(source_csv: Path, override: list[str] | None) -> list[str]:
    """Read the list of symbols to update."""
    if override:
        return override

    df = pd.read_csv(source_csv, dtype={"symbol": str})
    if "symbol" not in df.columns:
        raise ValueError(f"'symbol' column not found in {source_csv}")
    symbols = sorted({s for s in df["symbol"].dropna().astype(str) if s.strip()})
    return symbols


def download_existing(bucket: "oss2.Bucket", key: str) -> pd.DataFrame | None:
    """Download an existing per-stock CSV from OSS, or None if absent."""
    try:
        obj = bucket.get_object(key)
        raw = obj.read()
    except oss2.exceptions.NoSuchKey:
        return None
    except oss2.exceptions.OssError:
        return None

    if not raw:
        return None
    try:
        return pd.read_csv(io.BytesIO(raw), dtype={"symbol": str})
    except Exception:
        return None


def update_one(
    symbol: str,
    bucket: "oss2.Bucket",
    remote_prefix: str,
    end_date: str,
    adjust: str,
    retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> tuple[str, str, int, int]:
    """Update one stock's CSV on OSS. Returns (symbol, status, added, total)."""
    code = symbol.split(".")[0]
    key = f"{remote_prefix.strip('/')}/{symbol}.csv"

    existing = download_existing(bucket, key)
    if existing is not None and not existing.empty and "date" in existing.columns:
        last_date = pd.to_datetime(existing["date"], errors="coerce").max()
        start_date = last_date.strftime("%Y%m%d") if pd.notna(last_date) else BASE_START_DATE
    else:
        existing = None
        start_date = BASE_START_DATE

    try:
        new_df = build_daily_candlestick(
            code=code,
            start_date=start_date,
            end_date=end_date,
            adjust=adjust,
            retries=retries,
            delay_seconds=delay_seconds,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
        )
    except Exception as exc:
        if existing is not None:
            # Could not fetch new data, but keep the existing file intact.
            return symbol, f"no-update ({exc})", 0, len(existing)
        return symbol, f"error ({exc})", 0, 0

    if existing is not None:
        merged = pd.concat([existing, new_df], ignore_index=True)
        merged = (
            merged.drop_duplicates(subset="date", keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        added = len(merged) - len(existing)
    else:
        merged = new_df
        added = len(merged)

    csv_bytes = merged.to_csv(index=False).encode("utf-8-sig")
    try:
        bucket.put_object(key, csv_bytes)
    except oss2.exceptions.OssError as exc:
        return symbol, f"upload-failed ({exc})", 0, len(merged)

    return symbol, "ok", added, len(merged)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Incrementally update per-stock daily K-line CSVs directly on OSS."
    )
    parser.add_argument(
        "--source",
        type=str,
        default=str(
            Path(__file__).resolve().parents[1] / "data" / "stock_a_info.csv"
        ),
        help="CSV whose 'symbol' column lists the stocks to update "
        "(default: the full A-share universe in data/stock_a_info.csv).",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Explicit symbols (e.g. 300285.SZSE), overriding --source.",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="OSS bucket name.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OSS endpoint URL.")
    parser.add_argument("--cname", action="store_true", help="Treat endpoint as a CNAME domain.")
    parser.add_argument(
        "--remote-prefix",
        default="data/a_stock",
        help="Remote key prefix (folder) for per-stock CSVs.",
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="",
        choices=["", "qfq", "hfq"],
        help="Price adjustment. Default '' (raw) is append-safe; 'qfq' is NOT.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=time.strftime("%Y%m%d"),
        help="End date YYYYMMDD; defaults to today.",
    )
    parser.add_argument("--retries", type=int, default=5, help="Retry count per source.")
    parser.add_argument("--delay", type=float, default=0.8, help="Base retry delay seconds.")
    parser.add_argument("--backoff-factor", type=float, default=2.0, help="Backoff factor.")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max backoff seconds.")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent workers (each handles one stock end-to-end).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process the first N symbols (0 = all). Useful for testing.",
    )
    args = parser.parse_args()

    if args.adjust == "qfq":
        print(
            "Warning: --adjust qfq is not append-safe for incremental updates; "
            "historical values shift on each dividend. Prefer '' (raw) or 'hfq'."
        )

    source_csv = Path(args.source).resolve()
    try:
        symbols = read_symbols(source_csv, args.symbols)
    except Exception as exc:
        print(f"Failed to read symbols: {exc}")
        return

    if args.limit and args.limit > 0:
        symbols = symbols[: args.limit]

    if not symbols:
        print("No symbols to update.")
        return

    print(f"Updating {len(symbols)} symbol(s) -> oss://{args.bucket}/{args.remote_prefix}/")

    progress_lock = Lock()
    done = 0
    total = len(symbols)
    ok_count = 0

    def worker(symbol: str) -> tuple[str, str, int, int]:
        bucket = make_bucket(args.endpoint, args.bucket, args.cname)
        return update_one(
            symbol=symbol,
            bucket=bucket,
            remote_prefix=args.remote_prefix,
            end_date=args.end_date,
            adjust=args.adjust,
            retries=args.retries,
            delay_seconds=args.delay,
            backoff_factor=args.backoff_factor,
            max_delay=args.max_delay,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, s) for s in symbols]
        for future in as_completed(futures):
            symbol, status, added, total_rows = future.result()
            with progress_lock:
                done += 1
                if status == "ok":
                    ok_count += 1
                print(
                    f"[{done}/{total}] {symbol}: {status} "
                    f"(+{added} new, {total_rows} total)"
                )

    print(f"\nDone. {ok_count}/{total} updated successfully.")


if __name__ == "__main__":
    main()
