import pandas as pd
from sqlalchemy.orm import Session
from .models import Book

SHEET1_MAP = {
    "book title": "title",
    "author": "author",
    "release date": "release_date",
    "worth reading?": "worth_reading",
    "status": "reading_status",
    "year read": "year_read",
    "book location": "location",
    "where i read it": "location",
    "book location (o where i read it)": "location",
}

SHEET2_MAP = {
    "title": "title",
    "isbn": "isbn",
    "description": "description",
    "author": "author",
    "rating": "rating",
    "notes": "notes",
    "currentpage": "current_page",
    "totalpages": "total_pages",
    "current page": "current_page",
    "total pages": "total_pages",
    "readingstatus": "reading_status",
    "reading status": "reading_status",
}

FIELDS = [
    "title", "author", "release_date", "worth_reading", "reading_status", "year_read",
    "location", "isbn", "description", "current_page", "total_pages", "rating", "notes"
]


def normalize_value(value):
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    if text.endswith('.0') and text.replace('.0', '').isdigit():
        return text[:-2]
    return text


def map_columns(df, mapping):
    renamed = {}
    for col in df.columns:
        key = str(col).strip().lower()
        if key in mapping:
            renamed[col] = mapping[key]
    if not renamed:
        return pd.DataFrame(columns=FIELDS)
    out = df.rename(columns=renamed)
    cols = [c for c in out.columns if c in FIELDS]
    out = out[cols].copy()
    for field in FIELDS:
        if field not in out.columns:
            out[field] = None
    return out[FIELDS]


def import_excel(path: str, db: Session):
    workbook = pd.read_excel(path, sheet_name=None)
    combined = []
    for _, df in workbook.items():
        if df.empty:
            continue
        normalized_cols = {str(c).strip().lower() for c in df.columns}
        if "book title" in normalized_cols:
            combined.append(map_columns(df, SHEET1_MAP))
        elif "title" in normalized_cols:
            combined.append(map_columns(df, SHEET2_MAP))
    if not combined:
        raise ValueError("Nessun foglio compatibile trovato nel file Excel.")
    data = pd.concat(combined, ignore_index=True)
    imported = 0
    skipped = 0
    for _, row in data.iterrows():
        item = {field: normalize_value(row.get(field)) for field in FIELDS}
        if not item.get("title"):
            skipped += 1
            continue
        db.add(Book(**item))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}
