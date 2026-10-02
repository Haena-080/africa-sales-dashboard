import io
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Dict, Tuple

import pandas as pd


XML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _q(tag: str) -> str:
    return f"{{{XML_NS}}}{tag}"


def _clean_text(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _to_float(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def _read_uploaded_bytes(file_obj) -> bytes:
    if hasattr(file_obj, "getvalue"):
        return file_obj.getvalue()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return file_obj.read()


def _pivot_cache_rows(xlsx_bytes: bytes):
    """
    Read the underlying pivot cache instead of the visible/collapsed pivot table.

    This is important because the Sales Report may have product groups collapsed.
    Reading only visible pivot rows can omit most cartridge sales even though the
    Excel grand total is correct.
    """
    with zipfile.ZipFile(io.BytesIO(xlsx_bytes)) as z:
        definition_name = "xl/pivotCache/pivotCacheDefinition1.xml"
        records_name = "xl/pivotCache/pivotCacheRecords1.xml"

        if definition_name not in z.namelist() or records_name not in z.namelist():
            raise ValueError("Pivot Cache 원본 데이터를 찾지 못했습니다.")

        definition = ET.fromstring(z.read(definition_name))
        cache_fields = definition.find(_q("cacheFields"))

        field_names = []
        shared_values = []

        for field in cache_fields:
            field_names.append(field.attrib.get("name"))
            shared_items = field.find(_q("sharedItems"))
            values = []

            if shared_items is not None:
                for child in shared_items:
                    tag = child.tag.split("}")[-1]
                    if tag == "m":
                        values.append(None)
                    else:
                        values.append(child.attrib.get("v"))

            shared_values.append(values)

        records_root = ET.fromstring(z.read(records_name))

        def resolve(field_index, node):
            tag = node.tag.split("}")[-1]

            if tag == "x":
                idx = int(node.attrib["v"])
                values = shared_values[field_index]
                return values[idx] if idx < len(values) else None
            if tag == "n":
                return _to_float(node.attrib.get("v"))
            if tag == "b":
                return node.attrib.get("v") == "1"
            if tag == "m":
                return None
            return node.attrib.get("v")

        for record in records_root:
            nodes = list(record)
            yield {
                field_names[i]: resolve(i, node)
                for i, node in enumerate(nodes)
                if i < len(field_names)
            }


def read_sales_report(file_obj) -> Tuple[pd.DataFrame, Dict]:
    """
    Normalize the Africa Sales Report into monthly rows.

    Rules
    -----
    - Read underlying Pivot Cache, so collapsed/expanded pivot state does not matter.
    - Africa scope = Region == '아프리카'.
    - Customer key = 거래처번호.
    - Product key = 품번.
    - USD sales = 외화금액 as supplied by this report (already USD-based per business rule).
    - Invoice and LOT are intentionally not stored.
    - Multiple source rows are aggregated to customer + item + month.
    """
    xlsx_bytes = _read_uploaded_bytes(file_obj)
    source_rows = list(_pivot_cache_rows(xlsx_bytes))

    if not source_rows:
        raise ValueError("Sales Report 원본 데이터가 비어 있습니다.")

    required = {
        "품번", "품명", "고객", "거래처번호", "수량", "외화금액", "원화금액",
        "매출발생月", "Level 2", "Level 3", "Level 4", "Level 5",
        "Region", "수출국가", "담당자",
    }
    available = set(source_rows[0].keys())
    missing = sorted(required - available)
    if missing:
        raise ValueError("Sales Report 원본에 필요한 열이 없습니다: " + ", ".join(missing))

    africa_rows = [
        row for row in source_rows
        if _clean_text(row.get("Region")) == "아프리카"
    ]

    if not africa_rows:
        raise ValueError("Region='아프리카' 데이터를 찾지 못했습니다.")

    # Infer report year from transaction dates in the underlying source.
    year_counter = Counter()
    for row in africa_rows:
        text = _clean_text(row.get("마감일자/출고일자"))
        if text:
            match = re.match(r"(20\d{2})", text)
            if match:
                year_counter[int(match.group(1))] += 1

    if not year_counter:
        raise ValueError("Sales Report에서 연도를 확인하지 못했습니다.")

    year = year_counter.most_common(1)[0][0]

    normalized = []
    for row in africa_rows:
        month_value = row.get("매출발생月")
        try:
            month = int(float(month_value))
        except Exception:
            continue

        if not 1 <= month <= 12:
            continue

        customer_code = _clean_text(row.get("거래처번호"))
        item_code = _clean_text(row.get("품번"))
        if not customer_code or not item_code:
            continue

        source_date = _clean_text(row.get("마감일자/출고일자"))
        if source_date:
            match = re.match(r"(20\d{2})", source_date)
            if match and int(match.group(1)) != year:
                continue

        item_name = (
            _clean_text(row.get("Level 4"))
            or _clean_text(row.get("품명"))
        )

        normalized.append({
            "sales_month": pd.Timestamp(year=year, month=month, day=1).date(),
            "region": _clean_text(row.get("Region")),
            "country": _clean_text(row.get("수출국가")),
            "manager": _clean_text(row.get("담당자")),
            "customer_name": _clean_text(row.get("고객")),
            "customer_code": customer_code,
            "category": _clean_text(row.get("Level 2")),
            "product_group": _clean_text(row.get("Level 3")),
            "platform": _clean_text(row.get("Level 5")),
            "item_name": item_name,
            "item_code": item_code,
            "qty": _to_float(row.get("수량")),
            "sales_usd": _to_float(row.get("외화금액")),
            "sales_krw": _to_float(row.get("원화금액")),
        })

    if not normalized:
        raise ValueError("월별 매출 데이터를 생성하지 못했습니다.")

    df = pd.DataFrame(normalized)

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

    monthly = (
        df.groupby(group_keys, as_index=False, dropna=False)
        .agg(agg)
    )

    monthly["qty"] = monthly["qty"].round(3)
    monthly["sales_usd"] = monthly["sales_usd"].round(2)
    monthly["sales_krw"] = monthly["sales_krw"].round(0)

    meta = {
        "year": year,
        "months_detected": sorted(
            monthly["sales_month"].map(lambda d: d.month).unique().tolist()
        ),
        "source_rows": int(len(africa_rows)),
        "monthly_rows": int(len(monthly)),
        "customer_count": int(monthly["customer_code"].nunique()),
        "item_count": int(monthly["item_code"].nunique()),
        "sales_usd": float(monthly["sales_usd"].sum()),
        "sales_krw": float(monthly["sales_krw"].sum()),
        "qty": float(monthly["qty"].sum()),
    }

    return monthly, meta


COUNTRY_ALIASES = {
    "CABO VERDE": "Cape Verde",
    "CAPE VERDE": "Cape Verde",
    "GUINÉ-BISSAU": "Guinea-Bissau",
    "GUINE-BISSAU": "Guinea-Bissau",
}


def _normalize_country(value):
    text = _clean_text(value)
    if not text:
        return None
    upper = text.upper()
    return COUNTRY_ALIASES.get(upper, text)


def _country_from_customer(customer):
    text = _clean_text(customer)
    if not text:
        return None
    if "GUINÉ-BISSAU" in text.upper() or "GUINE-BISSAU" in text.upper():
        return "Guinea-Bissau"
    if ";" in text:
        return _normalize_country(text.rsplit(";", 1)[1].strip())
    return None


def read_business_plan(file_obj) -> Tuple[pd.DataFrame, Dict]:
    """
    Read the annual business-plan workbook into one row per manager/customer/month.
    Expected columns: 팀, 영업담당자, 고객, 1..12, 총합계.
    Only 아프리카팀 customer detail rows are kept; summary rows are excluded.
    """
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    df = pd.read_excel(file_obj, sheet_name=0)
    df.columns = [_clean_text(c) for c in df.columns]

    required = {"팀", "영업담당자", "고객"}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError("사업계획 파일에 필요한 열이 없습니다: " + ", ".join(missing))

    month_cols = {}
    for month in range(1, 13):
        candidates = [month, str(month), float(month)]
        hit = None
        for c in df.columns:
            if c in candidates or _clean_text(c) == str(month):
                hit = c
                break
        if hit is None:
            raise ValueError(f"{month}월 사업계획 열을 찾지 못했습니다.")
        month_cols[month] = hit

    df["팀"] = df["팀"].map(_clean_text).ffill()
    df["영업담당자"] = df["영업담당자"].map(_clean_text).ffill()
    df["고객"] = df["고객"].map(_clean_text)

    detail = df[
        df["팀"].eq("아프리카팀")
        & df["고객"].notna()
        & ~df["영업담당자"].fillna("").str.contains("요약")
        & ~df["고객"].fillna("").str.contains("요약")
    ].copy()

    if detail.empty:
        raise ValueError("아프리카팀 사업계획 상세행을 찾지 못했습니다.")

    # This workbook is the 2026 business plan. If a 4-digit year exists in the
    # filename later, the UI can pass another year; for the current template use 2026.
    year = 2026
    rows = []

    for _, rec in detail.iterrows():
        customer_name = _clean_text(rec["고객"])
        manager = _clean_text(rec["영업담당자"])
        country = _country_from_customer(customer_name)

        for month, col in month_cols.items():
            plan = pd.to_numeric(pd.Series([rec[col]]), errors="coerce").fillna(0).iloc[0]
            rows.append({
                "plan_month": pd.Timestamp(year=year, month=month, day=1).date(),
                "manager": manager,
                "customer_name": customer_name,
                "country": country,
                "plan_usd": float(plan),
            })

    plan_df = pd.DataFrame(rows)
    plan_df["plan_usd"] = pd.to_numeric(plan_df["plan_usd"], errors="coerce").fillna(0).round(2)

    meta = {
        "year": year,
        "customers": int(detail["고객"].nunique()),
        "managers": int(detail["영업담당자"].nunique()),
        "annual_plan_usd": float(plan_df["plan_usd"].sum()),
    }
    return plan_df, meta
