from typing import Dict, List

import pandas as pd
from supabase import Client, create_client


def get_client(url: str, secret_key: str) -> Client:
    return create_client(url, secret_key)


def _data(resp):
    return resp.data or []


def _clean_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def upsert_customer_master(sb: Client, df: pd.DataFrame) -> int:
    cols = ["customer_code", "customer_name", "country", "region"]
    master = (
        df[cols]
        .dropna(subset=["customer_code"])
        .drop_duplicates(subset=["customer_code"], keep="last")
        .copy()
    )

    rows = []
    for rec in master.to_dict("records"):
        code = str(rec.get("customer_code") or "").strip()
        if not code:
            continue
        rows.append({
            "customer_code": code,
            "customer_name": _clean_value(rec.get("customer_name")),
            "country": _clean_value(rec.get("country")),
            "region": _clean_value(rec.get("region")),
            "active": True,
        })

    for start in range(0, len(rows), 500):
        sb.table("customer_master").upsert(
            rows[start:start + 500],
            on_conflict="customer_code",
        ).execute()
    return len(rows)


def upsert_product_catalog(sb: Client, df: pd.DataFrame) -> int:
    cols = ["item_code", "item_name", "platform", "category", "product_group"]
    master = (
        df[cols]
        .dropna(subset=["item_code"])
        .drop_duplicates(subset=["item_code"], keep="last")
        .copy()
    )

    rows = []
    for rec in master.to_dict("records"):
        code = str(rec.get("item_code") or "").strip()
        if not code:
            continue
        rows.append({
            "item_code": code,
            "item_name": _clean_value(rec.get("item_name")),
            "platform": _clean_value(rec.get("platform")),
            "category": _clean_value(rec.get("category")),
            "product_group": _clean_value(rec.get("product_group")),
            "active": True,
        })

    for start in range(0, len(rows), 500):
        sb.table("product_catalog").upsert(
            rows[start:start + 500],
            on_conflict="item_code",
        ).execute()
    return len(rows)


def upsert_sales_monthly(sb: Client, df: pd.DataFrame, source_file: str) -> int:
    rows = []
    for rec in df.to_dict("records"):
        customer_code = str(rec.get("customer_code") or "").strip()
        item_code = str(rec.get("item_code") or "").strip()
        if not customer_code or not item_code:
            continue

        rows.append({
            "sales_month": _clean_value(rec.get("sales_month")),
            "customer_code": customer_code,
            "item_code": item_code,
            "qty": float(rec.get("qty") or 0),
            "sales_usd": float(rec.get("sales_usd") or 0),
            "sales_krw": float(rec.get("sales_krw") or 0),
            "source_file": source_file,
        })

    for start in range(0, len(rows), 500):
        sb.table("sales_monthly").upsert(
            rows[start:start + 500],
            on_conflict="sales_month,customer_code,item_code",
        ).execute()
    return len(rows)


def upload_sales_report(sb: Client, monthly: pd.DataFrame, source_file: str) -> Dict[str, int]:
    """
    Upload one normalized Sales Report.

    Master data is refreshed from the report first, then monthly facts are upserted.
    This makes re-uploading the same period safe and prevents duplicate sales.
    """
    customers = upsert_customer_master(sb, monthly)
    products = upsert_product_catalog(sb, monthly)
    sales_rows = upsert_sales_monthly(sb, monthly, source_file)
    return {
        "customers": customers,
        "products": products,
        "sales_rows": sales_rows,
    }


def upsert_business_plan(sb: Client, plan_df: pd.DataFrame, source_file: str) -> int:
    rows = []
    for rec in plan_df.to_dict("records"):
        rows.append({
            "plan_month": _clean_value(rec.get("plan_month")),
            "manager": _clean_value(rec.get("manager")),
            "customer_name": _clean_value(rec.get("customer_name")),
            "country": _clean_value(rec.get("country")),
            "plan_usd": float(rec.get("plan_usd") or 0),
            "source_file": source_file,
        })

    for start in range(0, len(rows), 500):
        sb.table("business_plan_monthly").upsert(
            rows[start:start + 500],
            on_conflict="plan_month,manager,customer_name",
        ).execute()
    return len(rows)
