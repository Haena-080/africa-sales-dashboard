from datetime import date, timedelta
from typing import Dict, List

from supabase import Client, create_client


def get_client(url: str, secret_key: str) -> Client:
    return create_client(url, secret_key)


def _data(resp):
    return resp.data or []


def ensure_countries(sb: Client, country_names: List[str]) -> Dict[str, str]:
    clean = sorted({str(x).strip() for x in country_names if x and str(x).strip()})
    if not clean:
        return {}
    sb.table("country_master").upsert(
        [{"country_name": x} for x in clean], on_conflict="country_name"
    ).execute()
    rows = _data(sb.table("country_master").select("id,country_name").in_("country_name", clean).execute())
    return {r["country_name"]: r["id"] for r in rows}


def ensure_managers(sb: Client, manager_names: List[str]) -> Dict[str, str]:
    clean = sorted({str(x).strip() for x in manager_names if x and str(x).strip()})
    if not clean:
        return {}
    sb.table("manager_master").upsert(
        [{"manager_name": x} for x in clean], on_conflict="manager_name"
    ).execute()
    rows = _data(sb.table("manager_master").select("id,manager_name").in_("manager_name", clean).execute())
    return {r["manager_name"]: r["id"] for r in rows}


def apply_manager_assignments(sb: Client, assignments):
    """assignments: iterable of dicts with country, manager, effective_from."""
    country_map = ensure_countries(sb, [r["country"] for r in assignments])
    manager_map = ensure_managers(sb, [r["manager"] for r in assignments])
    changes = []

    for row in assignments:
        country = row["country"].strip()
        manager = row["manager"].strip()
        effective_from = row["effective_from"]
        if hasattr(effective_from, "date"):
            effective_from = effective_from.date()
        if not isinstance(effective_from, date):
            raise ValueError(f"Invalid Effective From for {country}")

        country_id = country_map[country]
        manager_id = manager_map[manager]

        latest = _data(
            sb.table("country_manager_history")
            .select("id,manager_id,valid_from,valid_to")
            .eq("country_id", country_id)
            .order("valid_from", desc=True)
            .limit(1)
            .execute()
        )

        if latest:
            current = latest[0]
            if current["manager_id"] == manager_id and current.get("valid_to") is None:
                changes.append((country, manager, "unchanged"))
                continue
            close_date = effective_from - timedelta(days=1)
            # Close the previous assignment only when it overlaps the new one.
            if current.get("valid_to") is None or str(current["valid_to"]) >= effective_from.isoformat():
                sb.table("country_manager_history").update(
                    {"valid_to": close_date.isoformat()}
                ).eq("id", current["id"]).execute()

        sb.table("country_manager_history").upsert(
            {
                "country_id": country_id,
                "manager_id": manager_id,
                "valid_from": effective_from.isoformat(),
                "valid_to": None,
            },
            on_conflict="country_id,valid_from",
        ).execute()
        changes.append((country, manager, "updated"))
    return changes


def upsert_products(sb: Client, rows):
    payload = []
    for r in rows:
        code = str(r.get("item_code") or "").strip()
        if not code:
            continue
        payload.append({
            "item_code": code,
            "item_name": r.get("item_name"),
            "product_category": r.get("product_category") or "Unclassified",
            "product_group": r.get("product_group"),
        })
    if payload:
        sb.table("product_master").upsert(payload, on_conflict="item_code").execute()
    return len(payload)


def insert_sales_batch(sb: Client, df, source_file: str, file_hash: str = None):
    batch = _data(
        sb.table("upload_batch").insert({
            "source_file": source_file,
            "source_type": "COMBINED",
            "row_count": int(len(df)),
            "file_hash": file_hash,
        }).select("id").execute()
    )[0]
    batch_id = batch["id"]

    country_map = ensure_countries(sb, df["raw_country"].dropna().tolist())
    rows = []
    keep_cols = [
        "transaction_key", "txn_date", "raw_country", "customer_name", "invoice_no", "item_code",
        "item_name", "spec", "sales_unit", "currency", "fx_rate", "qty", "sales_amount_fc",
        "sales_amount_krw", "lot_no", "other_shipment_type", "source_type", "shipment_type",
        "original_sales_rep", "department", "hq"
    ]
    for rec in df[keep_cols].to_dict("records"):
        country = rec.get("raw_country")
        rec["country_id"] = country_map.get(country)
        rec["upload_batch_id"] = batch_id
        if rec.get("txn_date") is not None:
            rec["txn_date"] = rec["txn_date"].isoformat()
        # Convert pandas NaN/NaT to None.
        for k, v in list(rec.items()):
            try:
                if v != v:
                    rec[k] = None
            except Exception:
                pass
        rows.append(rec)

    # Chunked upsert avoids request-size problems and makes re-upload idempotent.
    chunk = 500
    for start in range(0, len(rows), chunk):
        sb.table("sales_fact").upsert(
            rows[start:start + chunk], on_conflict="transaction_key"
        ).execute()
    return batch_id, len(rows)
