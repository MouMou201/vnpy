"""Fetch A-share stock metadata with AkShare and save to CSV.

The output file contains one stock per row with:
- symbol (code + exchange suffix)
- code
- name
- industry
- listing_time

Rows are sorted by listing_time from newest to oldest.
"""

from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

import akshare as ak
import pandas as pd

OUTPUT_COLUMNS = [
    "symbol",
    "code",
    "name",
    "industry",
    "listing_time",
]


def exp_backoff_sleep(
    attempt: int,
    base_delay: float,
    backoff_factor: float,
    max_delay: float,
) -> None:
    """Sleep with exponential backoff by attempt number."""
    wait_seconds: float = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
    time.sleep(wait_seconds)


def pick_column(df: pd.DataFrame, candidates: list[str], field_name: str) -> str:
    """Pick the first existing column from candidates."""
    for column in candidates:
        if column in df.columns:
            return column

    raise ValueError(
        f"Cannot find column for {field_name}. "
        f"Available columns: {list(df.columns)}"
    )


def infer_exchange(code: str) -> str:
    """Infer exchange suffix from 6-digit stock code."""
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SSE"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZSE"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    return "UNKNOWN"


def parse_listing_time(raw_value: Any) -> pd.Timestamp | pd.NaT:
    """Convert listing date string to pandas Timestamp."""
    if raw_value is None:
        return pd.NaT

    value: str = str(raw_value).strip()
    if not value:
        return pd.NaT

    ts: pd.Timestamp | pd.NaT = pd.to_datetime(value, format="%Y%m%d", errors="coerce")
    if pd.notna(ts):
        return ts

    return pd.to_datetime(value, errors="coerce")


def call_with_retry(
    func: Any,
    label: str,
    global_retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> pd.DataFrame:
    """Call a no-arg fetch function with retry and exponential backoff."""
    last_error: Exception | None = None

    for attempt in range(1, global_retries + 1):
        try:
            df: pd.DataFrame = func()
            if df is None or df.empty:
                raise RuntimeError(f"{label} returned empty data.")
            return df
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt == global_retries:
                break
            print(
                f"{label} failed ({attempt}/{global_retries}): {exc}. "
                "Retrying with exponential backoff..."
            )
            exp_backoff_sleep(
                attempt=attempt,
                base_delay=delay_seconds,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
            )

    raise RuntimeError(f"{label} failed after {global_retries} attempts.") from last_error


def fetch_exchange_meta(
    global_retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> dict[str, dict[str, Any]]:
    """Fetch industry and listing date in batch from exchange name-code lists.

    These endpoints are far more reliable than the per-stock detail endpoint:
    - SZSE list provides listing date and industry.
    - BSE list provides listing date and industry.
    - SSE list provides listing date only (industry filled later as fallback).
    """
    meta: dict[str, dict[str, Any]] = {}

    # SZSE: 板块/A股代码/A股简称/A股上市日期/A股总股本/A股流通股本/所属行业
    sz_df = call_with_retry(
        ak.stock_info_sz_name_code, "SZSE name list",
        global_retries, delay_seconds, backoff_factor, max_delay,
    )
    for row in sz_df.to_dict("records"):
        code = str(row.get("A股代码", "")).zfill(6)
        if code and code != "000000":
            meta[code] = {
                "industry": row.get("所属行业"),
                "listing": row.get("A股上市日期"),
            }

    # SSE: 证券代码/证券简称/.../上市日期 (no industry).
    # Default board is 主板A股; 科创板 (STAR, 688xxx) must be requested separately.
    for sh_board in ["主板A股", "科创板"]:
        sh_df = call_with_retry(
            lambda board=sh_board: ak.stock_info_sh_name_code(symbol=board),
            f"SSE name list ({sh_board})",
            global_retries, delay_seconds, backoff_factor, max_delay,
        )
        for row in sh_df.to_dict("records"):
            code = str(row.get("证券代码", "")).zfill(6)
            if code:
                entry = meta.setdefault(code, {"industry": None, "listing": None})
                entry["listing"] = row.get("上市日期")

    # BSE: 证券代码/证券简称/.../上市日期/所属行业
    bj_df = call_with_retry(
        ak.stock_info_bj_name_code, "BSE name list",
        global_retries, delay_seconds, backoff_factor, max_delay,
    )
    for row in bj_df.to_dict("records"):
        code = str(row.get("证券代码", "")).zfill(6)
        if code:
            meta[code] = {
                "industry": row.get("所属行业"),
                "listing": row.get("上市日期"),
            }

    return meta


def fetch_code_name_list(
    global_retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> pd.DataFrame:
    """Fetch the full A-share code+name list via a lightweight single request.

    Uses ``stock_info_a_code_name`` which performs a single HTTP request,
    avoiding the heavy multi-page ``stock_zh_a_spot_em`` endpoint.
    """
    last_error: Exception | None = None

    for attempt in range(1, global_retries + 1):
        try:
            list_df: pd.DataFrame = ak.stock_info_a_code_name()
            if list_df.empty:
                raise RuntimeError("AkShare returned empty dataframe from stock_info_a_code_name().")

            code_col = pick_column(list_df, ["code", "代码"], "code")
            name_col = pick_column(list_df, ["name", "名称"], "name")

            result: pd.DataFrame = list_df[[code_col, name_col]].copy()
            result.columns = ["code", "name"]
            result["code"] = result["code"].astype(str).str.zfill(6)
            return result
        except Exception as exc:  # pragma: no cover - network dependent
            last_error = exc
            if attempt == global_retries:
                break
            print(
                f"Code list fetch failed ({attempt}/{global_retries}): {exc}. "
                "Retrying with exponential backoff..."
            )
            exp_backoff_sleep(
                attempt=attempt,
                base_delay=delay_seconds,
                backoff_factor=backoff_factor,
                max_delay=max_delay,
            )

    raise RuntimeError(
        f"Failed to fetch stock code list after {global_retries} attempts."
    ) from last_error


def fetch_industry_map_sina(
    global_retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
    workers: int,
) -> dict[str, str]:
    """Build a code -> industry map from Sina industry sectors.

    This source (sina) is push2-free, fast, and covers all markets including
    SSE, so it reliably fills industry where exchange lists cannot (SSE).
    """
    spot_df = call_with_retry(
        lambda: ak.stock_sector_spot(indicator="行业"),
        "Sina sector list",
        global_retries, delay_seconds, backoff_factor, max_delay,
    )

    label_col = pick_column(spot_df, ["label"], "sector label")
    name_col = pick_column(spot_df, ["板块"], "sector name")
    pairs: list[tuple[str, str]] = list(
        zip(spot_df[label_col].astype(str), spot_df[name_col].astype(str))
    )

    industry_map: dict[str, str] = {}
    lock = Lock()
    done = 0
    total = len(pairs)

    def worker(label: str, name: str) -> tuple[str, list[str]]:
        try:
            det = call_with_retry(
                lambda: ak.stock_sector_detail(sector=label),
                f"Sina sector {name}",
                global_retries, delay_seconds, backoff_factor, max_delay,
            )
            return name, [str(c).zfill(6) for c in det["code"].tolist()]
        except Exception:
            # One failed sector should not abort the whole run.
            return name, []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, label, name) for label, name in pairs]
        for future in as_completed(futures):
            name, codes = future.result()
            with lock:
                for code in codes:
                    industry_map[code] = name
                done += 1
                if done % 20 == 0 or done == total:
                    print(f"Sina industry sectors: {done}/{total}")

    return industry_map


def fetch_cninfo_industry(
    code: str,
    global_retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> str | None:
    """Fetch CSRC industry for one stock via cninfo company profile.

    A free, push2-free, per-stock source used only to fill the few stocks
    not yet classified by the Sina industry sectors (e.g. very new listings
    and CDRs).
    """
    try:
        df = call_with_retry(
            lambda: ak.stock_profile_cninfo(symbol=code),
            f"cninfo profile {code}",
            global_retries, delay_seconds, backoff_factor, max_delay,
        )
    except Exception:
        return None

    if "所属行业" in df.columns and len(df):
        value = df.iloc[0]["所属行业"]
        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def build_stock_info(
    delay_seconds: float,
    global_retries: int,
    backoff_factor: float,
    max_delay: float,
    workers: int,
) -> pd.DataFrame:
    """Build full A-share stock metadata dataframe.

    All data comes from stable, push2-free sources:
    1. code+name: ``stock_info_a_code_name`` (single lightweight request).
    2. listing_time: exchange name-code lists (SSE/SZSE/BSE).
    3. industry: Sina industry sectors (covers all markets, incl. SSE),
       with exchange-list industry (SZSE/BSE) as a secondary fill.
    """
    result_df: pd.DataFrame = fetch_code_name_list(
        global_retries=global_retries,
        delay_seconds=delay_seconds,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )

    result_df["symbol"] = result_df["code"].apply(
        lambda code: f"{code}.{infer_exchange(code)}"
    )

    codes: list[str] = result_df["code"].tolist()

    # Listing dates (and SZSE/BSE industry) from reliable exchange lists.
    meta = fetch_exchange_meta(
        global_retries=global_retries,
        delay_seconds=delay_seconds,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )

    # Industry from Sina sectors (covers all markets including SSE).
    sina_industry = fetch_industry_map_sina(
        global_retries=global_retries,
        delay_seconds=delay_seconds,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
        workers=workers,
    )

    def is_missing(value: Any) -> bool:
        return value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ""

    industry_by_code: dict[str, Any] = {}
    for c in codes:
        industry = sina_industry.get(c)
        if is_missing(industry):
            industry = meta.get(c, {}).get("industry")
        industry_by_code[c] = industry

    # Final fallback for the few still-missing (new listings, CDRs) via cninfo.
    still_missing = [c for c in codes if is_missing(industry_by_code[c])]
    if still_missing:
        print(f"Filling {len(still_missing)} remaining industries via cninfo...")
        lock = Lock()
        done = 0
        total = len(still_missing)

        def worker(code: str) -> tuple[str, str | None]:
            return code, fetch_cninfo_industry(
                code, global_retries, delay_seconds, backoff_factor, max_delay
            )

        with ThreadPoolExecutor(max_workers=min(workers, 8)) as executor:
            futures = [executor.submit(worker, c) for c in still_missing]
            for future in as_completed(futures):
                code, industry = future.result()
                if not is_missing(industry):
                    industry_by_code[code] = industry
                with lock:
                    done += 1
                    if done % 5 == 0 or done == total:
                        print(f"cninfo industry: {done}/{total}")

    result_df["industry"] = [industry_by_code[c] for c in codes]
    result_df["listing_time_dt"] = [
        parse_listing_time(meta.get(c, {}).get("listing")) for c in codes
    ]
    result_df = result_df.sort_values(
        by="listing_time_dt", ascending=False, na_position="last"
    ).reset_index(drop=True)
    result_df["listing_time"] = result_df["listing_time_dt"].dt.strftime("%Y-%m-%d")

    return result_df[OUTPUT_COLUMNS]


def normalize_and_sort(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize output schema and sort by listing_time descending."""
    normalized: pd.DataFrame = df.copy()

    for column in OUTPUT_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = pd.NA

    normalized["code"] = normalized["code"].astype(str).str.zfill(6)
    normalized["symbol"] = normalized["code"].apply(
        lambda code: f"{code}.{infer_exchange(code)}"
    )

    normalized["listing_time_dt"] = pd.to_datetime(
        normalized["listing_time"], errors="coerce"
    )
    normalized = normalized.sort_values(
        by="listing_time_dt", ascending=False, na_position="last"
    ).reset_index(drop=True)
    normalized["listing_time"] = normalized["listing_time_dt"].dt.strftime("%Y-%m-%d")

    return normalized[OUTPUT_COLUMNS]


def merge_with_existing(output_path: Path, latest_df: pd.DataFrame) -> pd.DataFrame:
    """Update existing CSV with latest data.

    Priority:
    - Use latest non-null values first.
    - Fallback to old values when latest is missing.
    """
    if not output_path.exists():
        return normalize_and_sort(latest_df)

    existing_df: pd.DataFrame = pd.read_csv(output_path)
    if existing_df.empty:
        return normalize_and_sort(latest_df)

    latest_norm: pd.DataFrame = normalize_and_sort(latest_df).set_index("code")
    existing_norm: pd.DataFrame = normalize_and_sort(existing_df).set_index("code")

    merged_df: pd.DataFrame = latest_norm.combine_first(existing_norm).reset_index()
    return normalize_and_sort(merged_df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch A-share stock info (code, name, industry, listing time)."
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(Path(__file__).resolve().parents[1] / "data" / "stock_a_info.csv"),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.8,
        help="Base retry delay seconds.",
    )
    parser.add_argument(
        "--global-retries",
        type=int,
        default=5,
        help="Retry count for the A-share code+name list fetch.",
    )
    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=2.0,
        help="Exponential backoff factor.",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=8.0,
        help="Maximum backoff delay seconds.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Number of concurrent workers for Sina industry sector fetch.",
    )
    args = parser.parse_args()

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    existed_before: bool = output_path.exists()

    try:
        latest_df = build_stock_info(
            delay_seconds=args.delay,
            global_retries=args.global_retries,
            backoff_factor=args.backoff_factor,
            max_delay=args.max_delay,
            workers=args.workers,
        )
        stock_df = merge_with_existing(output_path=output_path, latest_df=latest_df)
        stock_df.to_csv(output_path, index=False, encoding="utf-8-sig")

        action: str = "Updated" if existed_before else "Created"
        print(f"{action} {len(stock_df)} rows in: {output_path}")
    except KeyboardInterrupt:
        if existed_before:
            print(f"\nInterrupted by user. Existing CSV kept unchanged: {output_path}")
        else:
            print("\nInterrupted by user before any data was saved.")
        return
    except Exception as exc:  # pragma: no cover - network dependent
        if existed_before:
            print(
                f"Data fetch failed, keeping existing CSV unchanged: {output_path}\n"
                f"Reason: {exc}"
            )
            return

        empty_df = pd.DataFrame(columns=OUTPUT_COLUMNS)
        empty_df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(
            f"Data fetch failed and no old CSV found. Created empty CSV: {output_path}\n"
            f"Reason: {exc}"
        )


if __name__ == "__main__":
    main()
