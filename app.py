import hmac

import pandas as pd
import streamlit as st

from db import get_client, upload_sales_report
from transform import read_sales_report


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
        return get_client(
            st.secrets["SUPABASE_URL"],
            st.secrets["SUPABASE_SECRET_KEY"],
        )
    except Exception:
        st.error(
            "Supabase connection is not configured. "
            "Check SUPABASE_URL and SUPABASE_SECRET_KEY in Streamlit Secrets."
        )
        st.stop()


def read_table(sb, table: str, order_col: str, page_size: int = 1000) -> pd.DataFrame:
    rows = []
    start = 0

    while True:
        batch = (
            sb.table(table)
            .select("*")
            .order(order_col)
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


def load_dashboard_data(sb) -> pd.DataFrame:
    sales = read_table(sb, "sales_monthly", "id")
    if sales.empty:
        return sales

    customers = read_table(sb, "customer_master", "customer_code")
    products = read_table(sb, "product_catalog", "item_code")

    sales["sales_month"] = pd.to_datetime(sales["sales_month"], errors="coerce")
    for col in ["qty", "sales_usd", "sales_krw"]:
        sales[col] = pd.to_numeric(sales[col], errors="coerce").fillna(0)

    if not customers.empty:
        keep = [
            c for c in
            ["customer_code", "customer_name", "country", "region"]
            if c in customers.columns
        ]
        sales = sales.merge(
            customers[keep],
            on="customer_code",
            how="left",
        )

    if not products.empty:
        keep = [
            c for c in
            ["item_code", "item_name", "platform", "category", "product_group"]
            if c in products.columns
        ]
        sales = sales.merge(
            products[keep],
            on="item_code",
            how="left",
        )

    sales["year"] = sales["sales_month"].dt.year
    sales["month"] = sales["sales_month"].dt.month
    return sales


require_app_password()
sb = sb_client()

st.title("Africa Sales DB")
st.caption("Sales Report → Monthly Sales DB → Dashboard")

page = st.sidebar.radio(
    "Menu",
    ["Sales Report Upload", "Dashboard"],
)


if page == "Dashboard":
    try:
        df = load_dashboard_data(sb)
    except Exception as exc:
        st.error("새 Sales DB를 아직 읽지 못했습니다. 먼저 Sales Report Upload에서 2025 파일을 업로드해주세요.")
        st.caption("Supabase에서 customer_master, product_catalog, sales_monthly 테이블이 생성되어 있는지도 확인해주세요.")
        st.stop()

    if df.empty:
        st.info("아직 Sales Report 데이터가 없습니다. 먼저 Sales Report Upload에서 파일을 업로드해주세요.")
        st.stop()

    with st.sidebar:
        years = sorted(df["year"].dropna().astype(int).unique().tolist(), reverse=True)
        selected_year = st.selectbox("Year", years)

        year_df = df[df["year"].eq(selected_year)].copy()

        countries = sorted(year_df["country"].dropna().astype(str).unique().tolist())
        selected_countries = st.multiselect("Country", countries)

        customers = sorted(year_df["customer_name"].dropna().astype(str).unique().tolist())
        selected_customers = st.multiselect("Customer", customers)

        platforms = sorted(year_df["platform"].dropna().astype(str).unique().tolist())
        selected_platforms = st.multiselect("Platform", platforms)

    view = year_df.copy()

    if selected_countries:
        view = view[view["country"].isin(selected_countries)]
    if selected_customers:
        view = view[view["customer_name"].isin(selected_customers)]
    if selected_platforms:
        view = view[view["platform"].isin(selected_platforms)]

    foc_mask = view["sales_usd"].eq(0) & view["qty"].gt(0)
    category_text = view.get(
        "category",
        pd.Series("", index=view.index, dtype="object"),
    ).fillna("").astype(str)
    device_mask = category_text.str.contains("기기", na=False)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sales (USD)", "$" + f"{view['sales_usd'].sum():,.2f}")
    c2.metric("Sales (KRW)", f"₩{view['sales_krw'].sum():,.0f}")
    c3.metric("Total Qty", f"{view['qty'].sum():,.0f}")
    c4.metric("Device Qty", f"{view.loc[device_mask, 'qty'].sum():,.0f}")

    st.caption(
        "USD 매출은 Sales Report의 월별 외화금액을 그대로 사용합니다. "
        "환율 재계산은 하지 않습니다."
    )

    monthly = (
        view.groupby("sales_month", as_index=False)
        .agg(
            sales_usd=("sales_usd", "sum"),
            sales_krw=("sales_krw", "sum"),
            qty=("qty", "sum"),
        )
        .sort_values("sales_month")
    )

    st.subheader("Monthly Sales")
    if not monthly.empty:
        chart_data = monthly.set_index("sales_month")[["sales_usd"]]
        st.line_chart(chart_data)

    st.subheader("Country Summary")
    country_summary = (
        view.groupby("country", dropna=False)
        .agg(
            sales_usd=("sales_usd", "sum"),
            sales_krw=("sales_krw", "sum"),
            qty=("qty", "sum"),
        )
        .reset_index()
        .sort_values("sales_usd", ascending=False)
    )
    st.dataframe(
        country_summary,
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("FOC Summary")
    foc = view.loc[foc_mask].copy()
    if foc.empty:
        st.write("FOC 출고가 없습니다.")
    else:
        foc_summary = (
            foc.groupby(["country", "customer_name"], dropna=False)
            .agg(foc_qty=("qty", "sum"))
            .reset_index()
            .sort_values("foc_qty", ascending=False)
        )
        st.dataframe(
            foc_summary,
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Product Summary")
    product_summary = (
        view.groupby(
            ["item_code", "item_name", "platform", "category"],
            dropna=False,
        )
        .agg(
            sales_usd=("sales_usd", "sum"),
            qty=("qty", "sum"),
        )
        .reset_index()
        .sort_values("sales_usd", ascending=False)
    )
    st.dataframe(
        product_summary,
        use_container_width=True,
        hide_index=True,
    )


elif page == "Sales Report Upload":
    st.header("Sales Report Upload")
    st.write(
        "월별 수량 / 외화금액(USD) / 원화금액이 포함된 Sales Report를 업로드하세요. "
        "거래처는 거래처번호, 제품은 품번을 기준으로 저장합니다."
    )

    file = st.file_uploader(
        "Sales Report (.xlsx)",
        type=["xlsx"],
        key="sales_report",
    )

    if file:
        try:
            monthly, meta = read_sales_report(file)
        except Exception as exc:
            st.error(f"Sales Report를 읽지 못했습니다: {exc}")
            st.stop()

        st.success(
            f"{meta['year']}년 데이터 확인 완료 · "
            f"{meta['monthly_rows']:,} monthly rows · "
            f"{meta['customer_count']:,} customers · "
            f"{meta['item_count']:,} products"
        )

        c1, c2, c3 = st.columns(3)
        c1.metric("Sales (USD)", "$" + f"{meta['sales_usd']:,.2f}")
        c2.metric("Sales (KRW)", f"₩{meta['sales_krw']:,.0f}")
        c3.metric("Months", f"{len(meta['months_detected'])}")

        st.caption(
            "Invoice와 LOT은 저장하지 않습니다. "
            "동일한 거래처번호 + 품번 + 월 데이터는 하나로 합산됩니다."
        )

        st.subheader("Preview")
        preview_cols = [
            "sales_month", "country", "customer_name", "customer_code",
            "item_name", "item_code", "platform", "category",
            "qty", "sales_usd", "sales_krw",
        ]
        st.dataframe(
            monthly[preview_cols].head(200),
            use_container_width=True,
            hide_index=True,
        )

        if st.button("Upload to Supabase", type="primary"):
            with st.spinner("Uploading Sales Report..."):
                result = upload_sales_report(
                    sb,
                    monthly,
                    source_file=file.name,
                )

            st.success(
                f"완료! "
                f"Customers {result['customers']:,} · "
                f"Products {result['products']:,} · "
                f"Monthly sales {result['sales_rows']:,}"
            )
            st.info("이제 왼쪽 메뉴의 Dashboard에서 결과를 확인하면 됩니다.")
