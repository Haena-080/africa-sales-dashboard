import hashlib
import hmac
from datetime import date

import pandas as pd
import streamlit as st

from db import apply_manager_assignments, get_client, insert_sales_batch, upsert_products
from transform import combine_company_rule, read_erp_excel

st.set_page_config(page_title="Africa Sales DB", page_icon="🌍", layout="wide")


def require_app_password():
    expected = st.secrets.get("APP_PASSWORD", "")
    if not expected:
        st.error("APP_PASSWORD is not configured in Streamlit Secrets.")
        st.stop()
    if st.session_state.get("authenticated"):
        return
    password = st.text_input("App password", type="password")
    if st.button("Sign in"):
        if hmac.compare_digest(password, expected):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Incorrect password")
    st.stop()


def sb_client():
    try:
        return get_client(st.secrets["SUPABASE_URL"], st.secrets["SUPABASE_SECRET_KEY"])
    except Exception:
        st.error("Supabase connection is not configured. Add SUPABASE_URL and SUPABASE_SECRET_KEY to Streamlit Secrets.")
        st.stop()


def read_table(sb, table: str, columns: str = "*", page_size: int = 1000) -> pd.DataFrame:
    rows = []
    start = 0
    while True:
        batch = (
            sb.table(table)
            .select(columns)
            .order("id")
            .range(start, start + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return pd.DataFrame(rows)


def hash_uploads(*files):
    h = hashlib.sha256()
    for f in files:
        h.update(f.getvalue())
    return h.hexdigest()


def add_usd_equivalent(df: pd.DataFrame):
    """
    Add a transaction-time USD equivalent without using today's market FX.

    Rules:
    - USD rows: use ERP foreign-currency sales amount directly.
    - Non-USD rows (e.g. EUR): use booked KRW sales amount divided by an ERP-derived
      USD/KRW accounting rate for the same transaction date.
    - If no USD transaction exists on that date, use the median ERP USD/KRW rate
      for that year-month.
    - If no suitable USD reference exists, leave USD equivalent blank rather than
      guessing.
    """
    out = df.copy()
    out["currency"] = out.get("currency", pd.Series(index=out.index, dtype=object)).fillna("").astype(str).str.strip().str.upper()
    out["fx_rate"] = pd.to_numeric(out.get("fx_rate"), errors="coerce")
    out["sales_amount_fc"] = pd.to_numeric(out.get("sales_amount_fc"), errors="coerce")
    out["sales_amount_krw"] = pd.to_numeric(out.get("sales_amount_krw"), errors="coerce").fillna(0)
    out["txn_date"] = pd.to_datetime(out.get("txn_date"), errors="coerce")

    sales_mask = out["source_type"].eq("SALES")
    usd_ref_source = out[
        sales_mask
        & out["currency"].eq("USD")
        & out["fx_rate"].gt(0)
        & out["txn_date"].notna()
    ].copy()

    daily_rate = usd_ref_source.groupby(
        usd_ref_source["txn_date"].dt.normalize()
    )["fx_rate"].median()

    monthly_rate = usd_ref_source.groupby(
        usd_ref_source["txn_date"].dt.to_period("M").astype(str)
    )["fx_rate"].median()

    date_key = out["txn_date"].dt.normalize()
    month_key = out["txn_date"].dt.to_period("M").astype(str)
    out["usd_krw_reference"] = date_key.map(daily_rate)
    out["usd_krw_reference"] = out["usd_krw_reference"].fillna(month_key.map(monthly_rate))

    out["sales_amount_usd"] = pd.NA
    usd_sales = sales_mask & out["currency"].eq("USD")
    out.loc[usd_sales, "sales_amount_usd"] = out.loc[usd_sales, "sales_amount_fc"]

    non_usd_sales = (
        sales_mask
        & ~out["currency"].eq("USD")
        & out["usd_krw_reference"].gt(0)
    )
    out.loc[non_usd_sales, "sales_amount_usd"] = (
        out.loc[non_usd_sales, "sales_amount_krw"]
        / out.loc[non_usd_sales, "usd_krw_reference"]
    )

    out["sales_amount_usd"] = pd.to_numeric(out["sales_amount_usd"], errors="coerce")
    missing_conversion = int(
        (
            sales_mask
            & ~out["currency"].eq("USD")
            & out["sales_amount_krw"].ne(0)
            & out["sales_amount_usd"].isna()
        ).sum()
    )
    return out, missing_conversion


require_app_password()

st.title("Africa Sales DB")
st.caption("ERP Excel → standardized DB → master data → dashboard")

sb = sb_client()
page = st.sidebar.radio("Menu", ["Dashboard", "ERP Upload", "Country Manager", "Product Master"])

if page == "Dashboard":
    df = read_table(sb, "v_sales_enriched")
    if df.empty:
        st.info("No sales data yet. Upload ERP files first.")
        st.stop()

    df["txn_date"] = pd.to_datetime(df["txn_date"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df, missing_usd_conversion = add_usd_equivalent(df)

    with st.sidebar:
        countries = sorted(df["country_name"].dropna().unique().tolist()) if "country_name" in df else []
        managers = sorted(df["current_manager"].dropna().unique().tolist()) if "current_manager" in df else []
        selected_countries = st.multiselect("Country", countries)
        selected_managers = st.multiselect("Current Manager", managers)

    view = df.copy()
    if selected_countries:
        view = view[view["country_name"].isin(selected_countries)]
    if selected_managers:
        view = view[view["current_manager"].isin(selected_managers)]

    sales = view[view["source_type"].eq("SALES")].copy()
    foc = view[view["source_type"].eq("BL_FOC")]
    devices = view[view.get("product_category", pd.Series(index=view.index, dtype=str)).eq("Device")]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Sales (KRW)", f"₩{sales['sales_amount_krw'].sum():,.0f}")
    c2.metric("Sales (USD eq.)", f"${sales['sales_amount_usd'].sum():,.0f}")
    c3.metric("Sales rows", f"{len(sales):,}")
    c4.metric("FOC Qty", f"{foc['qty'].sum():,.0f}")
    c5.metric("Device Qty", f"{devices['qty'].sum():,.0f}")

    st.caption(
        "USD equivalent uses ERP transaction/accounting FX, not today's exchange rate. "
        "USD rows use the original foreign-currency sales amount. Non-USD rows are converted "
        "from booked KRW using the ERP USD/KRW rate on the same date; if unavailable, the "
        "same month's median ERP USD/KRW rate is used."
    )
    if missing_usd_conversion:
        st.warning(
            f"{missing_usd_conversion:,} non-USD sales rows could not be converted to USD "
            "because no ERP USD/KRW reference rate was available for the transaction date/month."
        )

    st.subheader("Currency Check")
    currency_summary = (
        sales.groupby("currency", dropna=False)
        .agg(
            rows=("currency", "size"),
            native_sales=("sales_amount_fc", "sum"),
            booked_krw=("sales_amount_krw", "sum"),
            usd_equivalent=("sales_amount_usd", "sum"),
        )
        .reset_index()
        .sort_values("booked_krw", ascending=False)
    )
    st.dataframe(currency_summary, use_container_width=True, hide_index=True)

    st.subheader("Country Summary")
    qty_summary = (
        view.groupby(["country_name", "current_manager"], dropna=False)
        .agg(total_qty=("qty", "sum"))
        .reset_index()
    )
    sales_summary = (
        sales.groupby(["country_name", "current_manager"], dropna=False)
        .agg(
            sales_krw=("sales_amount_krw", "sum"),
            sales_usd=("sales_amount_usd", "sum"),
        )
        .reset_index()
    )
    summary = (
        qty_summary.merge(
            sales_summary,
            on=["country_name", "current_manager"],
            how="left",
        )
        .fillna({"sales_krw": 0, "sales_usd": 0})
        .sort_values("sales_krw", ascending=False)
    )
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.subheader("Recent Data")
    show_cols = [c for c in [
        "txn_date", "country_name", "customer_name", "item_name", "currency", "fx_rate",
        "qty", "sales_amount_fc", "sales_amount_krw", "sales_amount_usd", "shipment_type",
        "current_manager", "manager_at_sale"
    ] if c in view]
    st.dataframe(
        view.sort_values("txn_date", ascending=False)[show_cols].head(300),
        use_container_width=True,
        hide_index=True,
    )

elif page == "ERP Upload":
    st.header("ERP Upload")
    st.write("Upload the two ERP exports. The app applies the company rule automatically: Sales rows with blank 기타출고구분 + BL rows with 기타출고구분.")
    sales_file = st.file_uploader("수출매출품목조회", type=["xlsx", "xls"], key="sales")
    bl_file = st.file_uploader("수출BL품목조회", type=["xlsx", "xls"], key="bl")

    if sales_file and bl_file:
        sales_raw = read_erp_excel(sales_file)
        bl_raw = read_erp_excel(bl_file)
        combined, mappings = combine_company_rule(sales_raw, bl_raw)

        missing_country = int(combined["raw_country"].isna().sum())
        st.success(f"Prepared {len(combined):,} rows. Missing country: {missing_country:,}")
        st.caption("Detected mapping")
        st.json(mappings)
        st.dataframe(combined.head(100), use_container_width=True, hide_index=True)

        if missing_country:
            st.warning("Some rows have no country. Add/confirm the country column mapping before production use.")

        if st.button("Upload to Supabase", type="primary"):
            file_hash = hash_uploads(sales_file, bl_file)
            batch_id, count = insert_sales_batch(
                sb, combined,
                source_file=f"{sales_file.name} + {bl_file.name}",
                file_hash=file_hash,
            )
            st.success(f"Uploaded {count:,} rows. Batch: {batch_id}")

elif page == "Country Manager":
    st.header("Country Manager Master")
    st.write("Upload only the current assignment changes. Previous assignments are closed automatically, so historical ownership remains available.")
    st.code("Country,Manager,Effective From\nGhana,Haena,2026-01-01\nBotswana,Haena,2026-01-01", language="text")
    file = st.file_uploader("Manager master (.xlsx or .csv)", type=["xlsx", "csv"], key="manager")
    if file:
        if file.name.lower().endswith(".csv"):
            m = pd.read_csv(file)
        else:
            m = pd.read_excel(file)
        normalized = {str(c).strip().lower(): c for c in m.columns}
        country_col = normalized.get("country") or normalized.get("국가")
        manager_col = normalized.get("manager") or normalized.get("담당자")
        effective_col = normalized.get("effective from") or normalized.get("valid from") or normalized.get("시작일")
        if not all([country_col, manager_col, effective_col]):
            st.error("Required columns: Country, Manager, Effective From")
        else:
            preview = pd.DataFrame({
                "country": m[country_col].astype(str).str.strip(),
                "manager": m[manager_col].astype(str).str.strip(),
                "effective_from": pd.to_datetime(m[effective_col], errors="coerce"),
            }).dropna()
            st.dataframe(preview, use_container_width=True, hide_index=True)
            if st.button("Apply manager changes", type="primary"):
                changes = apply_manager_assignments(sb, preview.to_dict("records"))
                st.success(f"Applied {len(changes)} assignments.")
                st.dataframe(pd.DataFrame(changes, columns=["Country", "Manager", "Result"]), hide_index=True)

    st.subheader("Current history table")
    history = sb.table("country_manager_history").select(
        "valid_from,valid_to,country_master(country_name),manager_master(manager_name)"
    ).order("valid_from", desc=True).execute().data or []
    if history:
        flat = [{
            "Country": x.get("country_master", {}).get("country_name"),
            "Manager": x.get("manager_master", {}).get("manager_name"),
            "Valid From": x.get("valid_from"),
            "Valid To": x.get("valid_to"),
        } for x in history]
        st.dataframe(pd.DataFrame(flat), use_container_width=True, hide_index=True)

elif page == "Product Master":
    st.header("Product Master")
    st.write("Manage Device / Reagent / Control / Accessory classification separately from ERP files.")
    st.code("item_code,item_name,product_category,product_group\nAF10,AFIAS-10,Device,AFIAS\n...,HbA1c Neo,Reagent,Diabetes", language="text")
    file = st.file_uploader("Product master (.xlsx or .csv)", type=["xlsx", "csv"], key="product")
    if file:
        p = pd.read_csv(file) if file.name.lower().endswith(".csv") else pd.read_excel(file)
        p.columns = [str(c).strip() for c in p.columns]
        required = {"item_code", "product_category"}
        if not required.issubset(p.columns):
            st.error("Required columns: item_code, product_category")
        else:
            st.dataframe(p.head(100), use_container_width=True, hide_index=True)
            if st.button("Update product master", type="primary"):
                n = upsert_products(sb, p.to_dict("records"))
                st.success(f"Updated {n:,} products.")
