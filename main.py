from pathlib import Path
import re
from io import BytesIO
import pandas as pd
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, func, asc, desc
from sqlalchemy.orm import Session
from .db import Base, engine, get_db
from .models import Book

Base.metadata.create_all(bind=engine)

app = FastAPI(title="Bookshelf App")
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

SORT_MAP = {
    "title": Book.title,
    "author": Book.author,
    "reading_state": Book.reading_bucket,
    "year_read": Book.year_read,
    "status": Book.status_raw,
    "location": Book.location,
}


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    if not s or s.lower() in {"nan", "none", "null"}:
        return None
    return s


def normalize_bucket(status):
    s = (clean(status) or "").lower().replace("à", "a")
    if not s:
        return "other"
    if "in lettura" in s or "reading" in s:
        return "reading"
    if "letto" in s or s == "read":
        return "read"
    if "da leggere" in s or "not read" in s or "da comprare" in s or "to buy" in s:
        return "not_read"
    if "nok" in s or "vendere" in s or "regalare" in s or "venduto" in s or "orrendo" in s:
        return "other"
    return "other"


def pick(row, *names):
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lower:
            return clean(lower[n.lower()])
    return None


def get_stats(db: Session):
    total = db.query(func.count(Book.id)).scalar() or 0
    return {
        "total": total,
        "read": db.query(func.count(Book.id)).filter(Book.reading_bucket == "read").scalar() or 0,
        "not_read": db.query(func.count(Book.id)).filter(Book.reading_bucket == "not_read").scalar() or 0,
        "reading": db.query(func.count(Book.id)).filter(Book.reading_bucket == "reading").scalar() or 0,
        "other": db.query(func.count(Book.id)).filter(Book.reading_bucket == "other").scalar() or 0,
    }


def apply_filters(query, q="", reading_bucket="", year_read="", status=""):
    if q:
        query = query.filter(or_(Book.title.ilike(f"%{q}%"), Book.author.ilike(f"%{q}%")))
    if reading_bucket:
        query = query.filter(Book.reading_bucket == reading_bucket)
    if year_read:
        query = query.filter(Book.year_read == year_read)
    if status:
        query = query.filter(Book.status_raw == status)
    return query


def apply_sort(query, sort_by="title", sort_dir="asc"):
    col = SORT_MAP.get(sort_by, Book.title)
    direction = desc if sort_dir == "desc" else asc
    return query.order_by(direction(col), asc(Book.title))


def export_rows(books):
    rows = []
    for b in books:
        rows.append({
            "Title": b.title,
            "Author": b.author,
            "Reading State": b.reading_bucket,
            "Year Read": b.year_read,
            "Status": b.status_raw,
            "Location": b.location,
            "Release Date": b.release_date,
            "Worth Reading": b.worth_reading,
            "ISBN": b.isbn,
            "Description": b.description,
            "Current Page": b.current_page,
            "Total Pages": b.total_pages,
            "Rating": b.rating,
            "Notes": b.notes,
        })
    return rows


def import_excel(file_path: Path, db: Session):
    added = 0
    skipped = 0
    xls = pd.read_excel(file_path, sheet_name=None)
    for _, df in xls.items():
        df = df.fillna("")
        for _, row in df.iterrows():
            data = row.to_dict()
            title = pick(data, "Book Title", "title")
            if not title:
                continue
            author = pick(data, "Author", "author")
            existing = db.query(Book).filter(func.lower(Book.title) == title.lower())
            if author:
                existing = existing.filter(func.lower(func.coalesce(Book.author, "")) == author.lower())
            else:
                existing = existing.filter(or_(Book.author.is_(None), Book.author == ""))
            if existing.first():
                skipped += 1
                continue
            status_raw = pick(data, "Status", "reading_status")
            yr = pick(data, "Year Read", "year_read")
            db.add(Book(
                title=title,
                author=author,
                release_date=pick(data, "Release Date", "release_date"),
                worth_reading=pick(data, "Worth reading?", "worth_reading"),
                status_raw=status_raw,
                reading_bucket=normalize_bucket(status_raw),
                year_read=(yr or None) if yr != "-" else None,
                location=pick(data, "Book location (o Where I Read It)", "location"),
                isbn=pick(data, "isbn"),
                description=pick(data, "description"),
                current_page=pick(data, "current_page"),
                total_pages=pick(data, "total_pages"),
                rating=pick(data, "rating"),
                notes=pick(data, "notes")
            ))
            added += 1
    db.commit()
    return added, skipped


def sort_toggle(current_by, current_dir, target):
    if current_by == target:
        return "desc" if current_dir == "asc" else "asc"
    return "asc"


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    query = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status), sort_by, sort_dir)
    books = query.all()
    years = [y[0] for y in db.query(Book.year_read).filter(Book.year_read.is_not(None), Book.year_read != "").distinct().order_by(Book.year_read.desc()).all()]
    statuses = [s[0] for s in db.query(Book.status_raw).filter(Book.status_raw.is_not(None), Book.status_raw != "").distinct().order_by(Book.status_raw.asc()).all()]
    return templates.TemplateResponse("index.html", {
        "request": request, "books": books, "q": q, "reading_bucket": reading_bucket, "year_read": year_read,
        "status": status, "years": years, "statuses": statuses, "stats": get_stats(db),
        "sort_by": sort_by, "sort_dir": sort_dir, "sort_toggle": sort_toggle,
    })


@app.get("/export/csv")
def export_csv(q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    books = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status), sort_by, sort_dir).all()
    df = pd.DataFrame(export_rows(books))
    stream = BytesIO(df.to_csv(index=False).encode("utf-8-sig"))
    return StreamingResponse(stream, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=bookshelf-filtered.csv"})


@app.get("/export/xlsx")
def export_xlsx(q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    books = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status), sort_by, sort_dir).all()
    df = pd.DataFrame(export_rows(books))
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Books", index=False)
    output.seek(0)
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=bookshelf-filtered.xlsx"})


@app.get("/statistics", response_class=HTMLResponse)
def statistics(request: Request, db: Session = Depends(get_db)):
    rows = db.query(Book.year_read, func.count(Book.id)).filter(Book.reading_bucket == "read", Book.year_read.is_not(None), Book.year_read != "").group_by(Book.year_read).order_by(Book.year_read.asc()).all()
    chart_labels = [r[0] for r in rows]
    chart_values = [r[1] for r in rows]
    return templates.TemplateResponse("statistics.html", {"request": request, "stats": get_stats(db), "chart_labels": chart_labels, "chart_values": chart_values})


@app.get("/books/new", response_class=HTMLResponse)
def new_book(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("form.html", {"request": request, "book": None, "stats": get_stats(db)})


@app.post("/books/new")
def create_book(title: str = Form(...), author: str = Form(""), release_date: str = Form(""), worth_reading: str = Form(""), status_raw: str = Form(""), year_read: str = Form(""), location: str = Form(""), isbn: str = Form(""), description: str = Form(""), current_page: str = Form(""), total_pages: str = Form(""), rating: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    db.add(Book(title=title.strip(), author=clean(author), release_date=clean(release_date), worth_reading=clean(worth_reading), status_raw=clean(status_raw), reading_bucket=normalize_bucket(status_raw), year_read=clean(year_read), location=clean(location), isbn=clean(isbn), description=clean(description), current_page=clean(current_page), total_pages=clean(total_pages), rating=clean(rating), notes=clean(notes)))
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/books/{book_id}/edit", response_class=HTMLResponse)
def edit_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("form.html", {"request": request, "book": db.get(Book, book_id), "stats": get_stats(db)})


@app.post("/books/{book_id}/edit")
def update_book(book_id: int, title: str = Form(...), author: str = Form(""), release_date: str = Form(""), worth_reading: str = Form(""), status_raw: str = Form(""), year_read: str = Form(""), location: str = Form(""), isbn: str = Form(""), description: str = Form(""), current_page: str = Form(""), total_pages: str = Form(""), rating: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    book.title = title.strip(); book.author = clean(author); book.release_date = clean(release_date); book.worth_reading = clean(worth_reading)
    book.status_raw = clean(status_raw); book.reading_bucket = normalize_bucket(status_raw); book.year_read = clean(year_read); book.location = clean(location)
    book.isbn = clean(isbn); book.description = clean(description); book.current_page = clean(current_page); book.total_pages = clean(total_pages); book.rating = clean(rating); book.notes = clean(notes)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if book:
        db.delete(book)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("import.html", {"request": request, "message": None, "stats": get_stats(db)})


@app.post("/import", response_class=HTMLResponse)
async def import_page_post(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    imports_dir = Path("imports")
    imports_dir.mkdir(exist_ok=True)
    target = imports_dir / re.sub(r"[^A-Za-z0-9._-]", "_", file.filename)
    target.write_bytes(await file.read())
    added, skipped = import_excel(target, db)
    return templates.TemplateResponse("import.html", {"request": request, "message": f"Import completed: {added} added, {skipped} skipped as duplicates.", "stats": get_stats(db)})
