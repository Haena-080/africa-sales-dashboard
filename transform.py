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
    customer = _clean_col(customer_name)
    if not customer:
        return None

    if customer in CUSTOMER_COUNTRY_OVERRIDES:
        country = CUSTOMER_COUNTRY_OVERRIDES[customer]
    elif ";" in customer:
        # Examples: "Alos Paraklet Healthcare Ltd.; Ghana" -> "Ghana"
        country = customer.rsplit(";", 1)[1].strip()
    else:
        return None

    return COUNTRY_ALIASES.get(country, country)


def canonicalize(df: pd.DataFrame, source_type: str) -> Tuple[pd.DataFrame, Dict[str, Optional[str]]]:
    mapping = detect_mapping(df.columns)
    out = pd.DataFrame(index=df.index)
    for canonical in COLUMN_CANDIDATES:
        out[canonical] = _series_or_blank(df, mapping.get(canonical))

    # Remove report total rows wherever they appear.
    total_mask = pd.Series(False, index=df.index)
    for candidate in [mapping.get("hq"), mapping.get("customer_name"), mapping.get("item_name")]:
        if candidate and candidate in df.columns:
            total_mask = total_mask | df[candidate].astype(str).str.strip().str.upper().eq("TOTAL")
    out = out.loc[~total_mask].copy()

    # Normalize values and types.
    out["txn_date"] = pd.to_datetime(out["txn_date"], errors="coerce").dt.date
    for c in ["qty", "fx_rate", "sales_amount_fc", "sales_amount_krw"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in ["raw_country", "customer_name", "invoice_no", "item_code", "item_name", "spec",
              "sales_unit", "currency", "lot_no", "other_shipment_type", "original_sales_rep",
              "department", "hq"]:
        out[c] = out[c].where(out[c].notna(), None)
        out[c] = out[c].map(lambda x: _clean_col(x) if x is not None else None)
        out[c] = out[c].replace({"": None, "nan": None, "None": None})

    # ERP export has no standalone country column in the supplied 2025 files.
    # Use it when present; otherwise infer from the customer/Buyer name.
    inferred = out["customer_name"].map(_infer_country)
    out["raw_country"] = out["raw_country"].where(out["raw_country"].notna(), inferred)
    out["raw_country"] = out["raw_country"].map(
        lambda x: COUNTRY_ALIASES.get(x, x) if x is not None else None
    )

    other = out["other_shipment_type"].fillna("").astype(str).str.strip()
    if source_type == "SALES":
        # Company rule: sales source contributes only rows without 기타출고구분.
        out = out.loc[other.eq("")].copy()
        out["shipment_type"] = "Sales"
    elif source_type == "BL_FOC":
        # Company rule: BL contributes only 기타출고 rows.
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

    # Africa Sales Dashboard: keep only Africa team rows.
    if TARGET_DEPARTMENTS:
        combined = combined.loc[combined["department"].isin(TARGET_DEPARTMENTS)].copy()

    # Stable key across re-uploads. cumcount protects legitimate identical lines in one file.
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
