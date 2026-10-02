import re
from typing import Dict, Tuple

import pandas as pd


DIMENSION_NAMES = {
    "region": "지역",
    "country": "국가",
    "manager": "담당자",
    "customer_name": "거래처",
    "customer_code": "거래처번호",
    "category": "대분류",
    "product_group": "Level 3",
    "platform": "플랫폼",
    "item_name": "제품",
    "lot_no": "Lot No.",
    "item_code": "품번",
}


def _clean_text(value):
    if pd.isna(value):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _find_header_row(raw: pd.DataFrame) -> int:
    """Find the row containing the stable pivot headers."""
    for idx in range(min(len(raw), 80)):
        values = {_clean_text(v) for v in raw.iloc[idx].tolist()}
        if "거래처번호" in values and "품번" in values:
            return idx
    raise ValueError("헤더 행을 찾지 못했습니다. '거래처번호'와 '품번'이 있는 Sales Report인지 확인해주세요.")


def _find_year(raw: pd.DataFrame, header_row: int) -> int:
    """Find a four-digit year above the pivot header."""
    for r in range(header_row):
        for value in raw.iloc[r].tolist():
            text = _clean_text(value)
            if text and re.fullmatch(r"20\d{2}", text):
                return int(text)
    raise ValueError("Sales Report에서 연도를 찾지 못했습니다.")


def read_sales_report(file_obj) -> Tuple[pd.DataFrame, Dict]:
    """
    Convert the pivot-style Sales Report into normalized monthly rows.

    Key rules:
    - Customer identity = 거래처번호
    - Product identity = 품번
    - USD sales = 월별 외화금액 as supplied in the report (already USD-based)
    - Invoice / LOT are not used in the monthly fact table
    - Only leaf product rows with a 품번 are kept; subtotal/요약 rows are excluded
    """
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    raw = pd.read_excel(file_obj, sheet_name=0, header=None)
    raw = raw.dropna(how="all").reset_index(drop=True)

    header_row = _find_header_row(raw)
    year = _find_year(raw, header_row)

    header = [_clean_text(v) for v in raw.iloc[header_row].tolist()]
    month_row = [_clean_text(v) for v in raw.iloc[header_row - 1].tolist()]

    col_by_name = {}
    for idx, value in enumerate(header):
        if value:
            col_by_name.setdefault(value, idx)

    missing = [label for label in DIMENSION_NAMES.values() if label not in col_by_name]
    if missing:
        raise ValueError(f"필수 열이 없습니다: {', '.join(missing)}")

    dim_cols = {key: col_by_name[label] for key, label in DIMENSION_NAMES.items()}

    # Detect monthly 3-column groups robustly.
    # The report repeats [수량, 외화금액, 원화금액] for each month.
    # We prefer the explicit month row when available, but fall back to the
    # repeated metric pattern so merged/pivot headers do not break parsing.
    metric_groups = []
    current_group = {}

    for col_idx in range(dim_cols["item_code"] + 1, len(header)):
        metric = header[col_idx]
        metric_compact = metric.replace(" ", "") if metric else ""

        kind = None
        if "수량" in metric_compact:
            kind = "qty"
        elif "외화금액" in metric_compact:
            kind = "sales_usd"
        elif "원화금액" in metric_compact:
            kind = "sales_krw"

        if not kind:
            continue

        # A new qty column starts the next 3-column metric group.
        if kind == "qty" and current_group:
            if {"qty", "sales_usd", "sales_krw"}.issubset(current_group):
                metric_groups.append(current_group)
            current_group = {}

        current_group[kind] = col_idx

        if {"qty", "sales_usd", "sales_krw"}.issubset(current_group):
            metric_groups.append(current_group)
            current_group = {}

    # The first 12 complete triplets are Jan-Dec.
    # Any later triplet is the grand total and is intentionally ignored.
    if len(metric_groups) < 12:
        raise ValueError(
            f"월별 수량/외화금액/원화금액 묶음을 12개 찾지 못했습니다. "
            f"현재 {len(metric_groups)}개를 찾았습니다."
        )

    complete_months = {
        month: metric_groups[month - 1]
        for month in range(1, 13)
    }

    data = raw.iloc[header_row + 1:].copy().reset_index(drop=True)

    # Pivot reports suppress repeated hierarchy labels. Forward-fill them.
    hierarchical_keys = [
        "region", "country", "manager", "customer_name", "customer_code",
        "category", "product_group", "platform", "item_name",
    ]
    dims = pd.DataFrame(index=data.index)
    for key, col_idx in dim_cols.items():
        dims[key] = data.iloc[:, col_idx].map(_clean_text)

    for key in hierarchical_keys:
        dims[key] = dims[key].ffill()

    # 품번 is the leaf-level product key. Blank 품번 means subtotal/header row.
    leaf_mask = dims["item_code"].notna()
    data = data.loc[leaf_mask].reset_index(drop=True)
    dims = dims.loc[leaf_mask].reset_index(drop=True)

    monthly_parts = []
    for month, cols in sorted(complete_months.items()):
        part = dims[
            [
                "region", "country", "manager", "customer_name", "customer_code",
                "category", "product_group", "platform", "item_name", "item_code",
            ]
        ].copy()

        part["sales_month"] = pd.Timestamp(year=year, month=month, day=1).date()
        part["qty"] = pd.to_numeric(data.iloc[:, cols["qty"]], errors="coerce").fillna(0)
        part["sales_usd"] = pd.to_numeric(data.iloc[:, cols["sales_usd"]], errors="coerce").fillna(0)
        part["sales_krw"] = pd.to_numeric(data.iloc[:, cols["sales_krw"]], errors="coerce").fillna(0)

        # Skip months where the leaf product had no movement at all.
        movement = (
            part["qty"].ne(0)
            | part["sales_usd"].ne(0)
            | part["sales_krw"].ne(0)
        )
        monthly_parts.append(part.loc[movement])

    if not monthly_parts:
        raise ValueError("월별 매출/수량 데이터가 없습니다.")

    monthly = pd.concat(monthly_parts, ignore_index=True)

    # Clean keys before grouping.
    for col in [
        "region", "country", "manager", "customer_name", "customer_code",
        "category", "product_group", "platform", "item_name", "item_code",
    ]:
        monthly[col] = monthly[col].map(_clean_text)

    monthly = monthly[
        monthly["customer_code"].notna() & monthly["item_code"].notna()
    ].copy()

    # LOT is intentionally ignored. Multiple LOT rows become one customer/item/month row.
    group_keys = ["sales_month", "customer_code", "item_code"]
    agg = {
        "region": "first",
        "country": "first",
        "manager": "first",
        "customer_name": "first",
        "category": "first",
        "product_group": "first",
        "platform": "first",
        "item_name": "first",
        "qty": "sum",
        "sales_usd": "sum",
        "sales_krw": "sum",
    }
    monthly = monthly.groupby(group_keys, as_index=False, dropna=False).agg(agg)

    monthly["sales_usd"] = monthly["sales_usd"].round(2)
    monthly["sales_krw"] = monthly["sales_krw"].round(0)
    monthly["qty"] = monthly["qty"].round(3)

    meta = {
        "year": year,
        "header_excel_row": header_row + 1,
        "months_detected": sorted(complete_months),
        "monthly_rows": int(len(monthly)),
        "customer_count": int(monthly["customer_code"].nunique()),
        "item_count": int(monthly["item_code"].nunique()),
        "sales_usd": float(monthly["sales_usd"].sum()),
        "sales_krw": float(monthly["sales_krw"].sum()),
    }
    return monthly, meta
