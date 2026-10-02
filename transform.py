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

# This dashboard is for the Africa team only.
TARGET_DEPARTMENTS = {"아프리카팀"}

# ERP 거래처/Bayer 이름에 국가가 없는 예외만 이곳에서 관리합니다.
# 일반 거래처는 "거래처명; Country" 형식의 마지막 세미콜론 뒤를 자동으로 국가로 사용합니다.
CUSTOMER_COUNTRY_OVERRIDES = {
    "Quilaban,S,A_GUINÉ-BISSAU": "Guinea-Bissau",
}

# 같은 국가의 표기만 통일합니다. 필요 시 이후 추가 가능합니다.
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
    """Infer country from ERP customer name when there is no standalone country column."""
    if customer_name is None or pd.isna(customer_name):
        return None
