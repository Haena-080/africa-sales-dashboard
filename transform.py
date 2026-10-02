import hashlib
import re
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd


COLUMN_CANDIDATES = {
    "txn_date": ["매출일", "BL 일자", "BL일자", "출고일", "일자"],
    "hq": ["본부"],
    "department": ["부서"],
    "original_sales_rep": ["영업담당자", "담당자"],
    "raw_country": ["국가", "국가명", "Country", "COUNTRY"],
    "customer_name": ["거래처", "Buyer", "BUYER", "거래처명", "판매처", "판매처명"],
    "invoice_no": ["INVOICE NO", "INVOICE NO.", "Invoice No", "Invoice No."],
    "item_code": ["품번", "품목코드", "Item Code", "ITEM CODE"],
    "item_name": ["품명", "품목명", "Item Name", "ITEM NAME"],
    "spec": ["규격", "Spec", "SPEC"],
    "sales_unit": ["판매단위", "단위", "Unit", "UNIT"],
    "currency": ["통화", "Currency", "CURRENCY"],
    "fx_rate": ["환율", "Exchange Rate", "EXCHANGE RATE"],
    "qty": ["수량", "출고수량", "매출수량", "QTY", "Qty"],
    "sales_amount_fc": ["판매금액", "외화판매금액", "외화금액", "Sales Amount"],
    "sales_amount_krw": ["원화판매금액", "원화금액", "KRW 판매금액", "Sales Amount(KRW)"],
    "lot_no": ["LOTNo", "LOT No", "LOT NO", "Lot No"],
    "other_shipment_type": ["기타출고구분(New)", "기타출고구분", "기타 출고 구분"],
}

TARGET_DEPARTMENTS = {"아프리카팀"}

CUSTOMER_COUNTRY_OVERRIDES = {
    "Quilaban,S,A_GUINÉ-BISSAU": "Guinea-Bissau",
}

COUNTRY_ALIASES = {
    "CABO VERDE": "Cape Verde",
}


def _clean_col(v) -> str:
    if pd.isna(v):
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


def _detect_header_row(file_obj, sheet_name=0, max_scan_rows: int = 12) -> int:
    preview = pd.read_excel(file_obj, sheet_name=sheet_name, header=None, nrows=max_scan_rows)
    best_row, best_score = 0, -1
    markers = {"품번", "품명", "수량", "INVOICE NO", "거래처", "Buyer", "매출일", "BL 일자"}
    for i in range(len(preview)):
        values = {_clean_col(x) for x in preview.iloc[i].tolist()}
        score = sum(1 for m in markers if m in values)
        if score > best_score:
            best_row, best_score = i, score
    return best_row


def read_erp_excel(file_obj, sheet_name=0) -> pd.DataFrame:
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    header_row = _detect_header_row(file_obj, sheet_name=sheet_name)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    df = pd.read_excel(file_obj, sheet_name=sheet_name, header=header_row)
    df.columns = [_clean_col(c) for c in df.columns]
    df = df.dropna(how="all").copy()
    return df


def detect_mapping(columns: Iterable[str]) -> Dict[str, Optional[str]]:
    cols = {_clean_col(c): c for c in columns}
    mapping: Dict[str, Optional[str]] = {}
    for canonical, candidates in COLUMN_CANDIDATES.items():
        hit = None
        for candidate in candidates:
            if candidate in cols:
                hit = cols[candidate]
                break
        mapping[canonical] = hit
    return mapping


def _series_or_blank(df: pd.DataFrame, col: Optional[str]):
    if col and col in df.columns:
        return df[col]
    return pd.Series([None] * len(df), index=df.index)


def _infer_country(customer_name):
    if customer_name is None or pd.isna(customer_name):
        return None
    customer = _clean_col(customer_name)
    if not customer:
        return None

    if customer in CUSTOMER_COUNTRY_OVERRIDES:
        country = CUSTOMER_COUNTRY_OVERRIDES[customer]
    elif ";" in customer:
        country = customer.rsplit(";", 1)[1].strip()
    else:
        return None

    return COUNTRY_ALIASES.get(country, country)


def canonicalize(df: pd.DataFrame, source_type: str) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
    mapping = detect_mapping(df.columns)
    out = pd.DataFrame(index=df.index)
    for canonical in COLUMN_CANDIDATES:
        out[canonical] = _series_or_blank(df, mapping.get(canonical))

    total_mask = pd.Series(False, index=df.index)
    for candidate in [mapping.get("hq"), mapping.get("customer_name"), mapping.get("item_name")]:
        if candidate and candidate in df.columns:
            total_mask = total_mask | df[candidate].astype(str).str.strip().str.upper().eq("TOTAL")
    out = out.loc[~total_mask].copy()

    out["txn_date"] = pd.to_datetime(out["txn_date"], errors="coerce").dt.date
    for c in ["qty", "fx_rate", "sales_amount_fc", "sales_amount_krw"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in ["raw_country", "customer_name", "invoice_no", "item_code", "item_name", "spec",
              "sales_unit", "currency", "lot_no", "other_shipment_type", "original_sales_rep",
              "department", "hq"]:
        out[c] = out[c].where(out[c].notna(), None)
        out[c] = out[c].map(lambda x: _clean_col(x) if x is not None else None)
        out[c] = out[c].replace({"": None, "nan": None, "None": None})

    inferred = out["customer_name"].map(_infer_country)
    out["raw_country"] = out["raw_country"].where(out["raw_country"].notna(), inferred)
    out["raw_country"] = out["raw_country"].map(
        lambda x: COUNTRY_ALIASES.get(x, x) if x is not None else None
    )

    other = out["other_shipment_type"].fillna("").astype(str).str.strip()
    if source_type == "SALES":
        out = out.loc[other.eq("")].copy()
        out["shipment_type"] = "Sales"
    elif source_type == "BL_FOC":
        out = out.loc[~other.eq("")].copy()
        out["shipment_type"] = out["other_shipment_type"].fillna("Other")
    else:
        raise ValueError("source_type must be SALES or BL_FOC")

    out["source_type"] = source_type
    return out.reset_index(drop=True), mapping


def combine_company_rule(sales_df: pd.DataFrame, bl_df: pd.DataFrame) -> Tuple[pd.DataFrame, dict]:
    sales, sales_map = canonicalize(sales_df, "SALES")
    bl, bl_map = canonicalize(bl_df, "BL_FOC")
    combined = pd.concat([sales, bl], ignore_index=True)

    if TARGET_DEPARTMENTS:
        combined = combined.loc[combined["department"].isin(TARGET_DEPARTMENTS)].copy()

    signature_cols = [
        "txn_date", "raw_country", "customer_name", "invoice_no", "item_code", "item_name",
        "qty", "sales_amount_fc", "sales_amount_krw", "lot_no", "other_shipment_type", "source_type"
    ]
    sig = combined[signature_cols].fillna("").astype(str).agg("|".join, axis=1)
    occurrence = sig.groupby(sig).cumcount().astype(str)
    combined["transaction_key"] = [
        hashlib.sha256(f"{s}|{n}".encode("utf-8")).hexdigest() for s, n in zip(sig, occurrence)
    ]
    return combined.reset_index(drop=True), {"sales": sales_map, "bl": bl_map}
