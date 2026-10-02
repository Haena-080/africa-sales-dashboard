import hmac

import pandas as pd
import streamlit as st

from db import get_client, upload_business_plan, upload_sales_report
from transform import read_business_plan, read_sales_report


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
    ["Dashboard", "Sales Report Upload", "Business Plan Upload"],
)


if page == "Dashboard":
    try:
        df = load_dashboard_data(sb)
    except Exception:
        st.error("Sales DB를 읽지 못했습니다. Supabase 연결과 테이블을 확인해주세요.")
        st.stop()

    if df.empty:
        st.info("아직 Sales Report 데이터가 없습니다. 먼저 Sales Report Upload에서 파일을 업로드해주세요.")
        st.stop()

    try:
        plan_df = read_table(sb, "business_plan_monthly", "id")
    except Exception:
        plan_df = pd.DataFrame()

    if not plan_df.empty:
        plan_df["plan_month"] = pd.to_datetime(plan_df["plan_month"], errors="coerce")
        plan_df["plan_usd"] = pd.to_numeric(plan_df["plan_usd"], errors="coerce").fillna(0)
        plan_df["year"] = plan_df["plan_month"].dt.year
        plan_df["month"] = plan_df["plan_month"].dt.month

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

    current_sales = float(view["sales_usd"].sum())
    annual_plan = None
    ytd_plan = None
    prior_ytd = None
    latest_month = None

    if not view.empty:
        nonzero_months = (
            view.groupby("month")["sales_usd"].sum()
            .loc[lambda s: s.ne(0)]
            .index.tolist()
        )
        if nonzero_months:
            latest_month = max(nonzero_months)

    if selected_year == 2026 and not plan_df.empty:
        selected_plan = plan_df[plan_df["year"].eq(2026)].copy()
        annual_plan = float(selected_plan["plan_usd"].sum())

        if latest_month:
            ytd_plan = float(
                selected_plan[selected_plan["month"].le(latest_month)]["plan_usd"].sum()
            )
            prior = df[
                df["year"].eq(2025)
                & df["month"].le(latest_month)
            ].copy()
            if selected_countries:
                prior = prior[prior["country"].isin(selected_countries)]
            if selected_customers:
                prior = prior[prior["customer_name"].isin(selected_customers)]
            if selected_platforms:
                prior = prior[prior["platform"].isin(selected_platforms)]
            prior_ytd = float(prior["sales_usd"].sum())

    if selected_year == 2026 and annual_plan:
        achievement = current_sales / annual_plan * 100 if annual_plan else 0
        ytd_achievement = current_sales / ytd_plan * 100 if ytd_plan else None
        yoy = ((current_sales / prior_ytd) - 1) * 100 if prior_ytd else None
        remaining = max(annual_plan - current_sales, 0)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("2026 Sales", "$" + f"{current_sales:,.0f}")
        c2.metric("Annual Plan Achievement", f"{achievement:,.1f}%")
        c3.metric(
            "YTD Plan Achievement",
            f"{ytd_achievement:,.1f}%" if ytd_achievement is not None else "—",
            help=f"1월~{latest_month}월 계획 대비 실적" if latest_month else None,
        )
        c4.metric(
            "YoY Growth",
            f"{yoy:+,.1f}%" if yoy is not None else "—",
            help=f"2025년 1월~{latest_month}월 동기 대비" if latest_month else None,
        )

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("2026 Annual Plan", "$" + f"{annual_plan:,.0f}")
        c6.metric("Remaining to Plan", "$" + f"{remaining:,.0f}")
        c7.metric("Total Qty", f"{view['qty'].sum():,.0f}")
        c8.metric("Device Qty", f"{view.loc[device_mask, 'qty'].sum():,.0f}")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Sales (USD)", "$" + f"{current_sales:,.2f}")
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
        chart_data = monthly[["sales_month", "sales_usd"]].copy()

        if selected_year == 2026 and not plan_df.empty:
            plan_monthly = (
                plan_df[plan_df["year"].eq(2026)]
                .groupby("plan_month", as_index=False)
                .agg(plan_usd=("plan_usd", "sum"))
            )
            chart_data = chart_data.merge(
                plan_monthly,
                left_on="sales_month",
                right_on="plan_month",
                how="outer",
            )
            chart_data["sales_month"] = chart_data["sales_month"].fillna(chart_data["plan_month"])
            chart_data = (
                chart_data[["sales_month", "sales_usd", "plan_usd"]]
                .fillna(0)
                .sort_values("sales_month")
                .set_index("sales_month")
            )
            st.line_chart(chart_data.rename(columns={"sales_usd": "Actual", "plan_usd": "Plan"}))

            cumulative = chart_data.cumsum()
            st.subheader("Cumulative Progress")
            st.line_chart(cumulative.rename(columns={"Actual": "Actual YTD", "Plan": "Plan YTD"}))
        else:
            st.line_chart(
                chart_data.set_index("sales_month")[["sales_usd"]]
            )

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


elif page == "Business Plan Upload":
    st.header("Business Plan Upload")
    st.write(
        "2026 아프리카팀 사업계획 파일을 업로드하세요. "
        "월별 계획을 저장해 실제 매출과 목표 달성률을 비교합니다."
    )

    file = st.file_uploader(
        "2026 Business Plan (.xlsx)",
        type=["xlsx"],
        key="business_plan",
    )

    if file:
        try:
            plan, meta = read_business_plan(file)
        except Exception as exc:
            st.error(f"사업계획 파일을 읽지 못했습니다: {exc}")
            st.stop()

        st.success(
            f"{meta['year']}년 아프리카팀 사업계획 확인 완료 · "
            f"{meta['customers']:,} customers · "
            f"{meta['managers']:,} managers"
        )

        c1, c2 = st.columns(2)
        c1.metric("Annual Plan (USD)", "$" + f"{meta['annual_plan_usd']:,.2f}")
        c2.metric("Monthly Rows", f"{len(plan):,}")

        preview = (
            plan.groupby("plan_month", as_index=False)
            .agg(plan_usd=("plan_usd", "sum"))
        )
        st.dataframe(preview, use_container_width=True, hide_index=True)

        if st.button("Upload Business Plan to Supabase", type="primary"):
            with st.spinner("Uploading business plan..."):
                count = upload_business_plan(sb, plan, source_file=file.name)
            st.success(f"사업계획 {count:,} rows 업로드 완료.")
            st.info("Dashboard에서 2026년을 선택하면 달성률과 전년 대비 진척도를 볼 수 있습니다.")
