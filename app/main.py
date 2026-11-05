from pathlib import Path
import os
import re
import json
import threading
import time
from difflib import SequenceMatcher
from io import BytesIO
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode
import pandas as pd
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, func, asc, desc
from sqlalchemy.orm import Session
from .db import Base, engine, get_db, SessionLocal
from .models import Book, Tag

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
    if "ko" in s:
        return "ko"
    if "nok" in s or "vendere" in s or "regalare" in s or "venduto" in s or "orrendo" in s:
        return "other"
    return "other"


LOCATION_CANON = {
    "mi": "MI",
    "ba": "BA",
    "ebook": "Ebook",
    "sold": "Sold",
    "to buy": "To Buy",
    "mi for sale": "MI - For Sale",
    "mi - for sale": "MI - For Sale",
    "for sale": "MI - For Sale",
}


def normalize_location(v):
    v = clean(v)
    if not v:
        return None
    key = re.sub(r"\s+", " ", v).strip().lower()
    return LOCATION_CANON.get(key, re.sub(r"\s+", " ", v).strip())


def cover_url(book):
    v = (book.cover_id or "").strip()
    if v:
        if re.fullmatch(r"\d+", v):
            return f"/cover/id/{v}"
        return v
    isbn = re.sub(r"[^0-9Xx]", "", book.isbn or "")
    if isbn:
        return f"/cover/isbn/{isbn}"
    return ""


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


def get_tags(db: Session):
    return [t.name for t in db.query(Tag).order_by(Tag.name.asc()).all()]


def parse_tags(value):
    out = []
    seen = set()
    for part in (value or "").split(","):
        name = part.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def set_book_tags(db: Session, book, value):
    book.tags = []
    for name in parse_tags(value):
        tag = db.query(Tag).filter(func.lower(Tag.name) == name.lower()).first()
        if not tag:
            tag = Tag(name=name)
            db.add(tag)
            db.flush()
        book.tags.append(tag)


def apply_filters(query, q="", reading_bucket="", year_read="", status="", location="", tag=""):
    if q:
        query = query.filter(or_(Book.title.ilike(f"%{q}%"), Book.author.ilike(f"%{q}%")))
    if reading_bucket:
        query = query.filter(Book.reading_bucket == reading_bucket)
    if year_read:
        query = query.filter(Book.year_read == year_read)
    if status:
        query = query.filter(Book.status_raw == status)
    if location:
        query = query.filter(Book.location == location)
    if tag:
        query = query.filter(Book.tags.any(Tag.name == tag))
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
            "Tags": ", ".join(t.name for t in b.tags),
            "Worth Reading": b.worth_reading,
            "ISBN": b.isbn,
            "Language": b.language or "",
            "Description": b.description,
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
                worth_reading=pick(data, "Worth reading?", "worth_reading"),
                status_raw=status_raw,
                reading_bucket=normalize_bucket(status_raw),
                year_read=(yr or None) if yr != "-" else None,
                location=normalize_location(pick(data, "Book location (o Where I Read It)", "location")),
                isbn=pick(data, "isbn"),
                description=pick(data, "description"),
            ))
            added += 1
    db.commit()
    return added, skipped


def sort_toggle(current_by, current_dir, target):
    if current_by == target:
        return "desc" if current_dir == "asc" else "asc"
    return "asc"


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODELS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]

LANG_INSTRUCTION = {
    "it": "in italiano",
    "en": "in inglese",
    "auto": "in modo opportuno: usa la lingua del libro (quella del titolo e dell'autore, se comprensibile), altrimenti in italiano",
}

SYSTEM_PROMPT = (
    "Sei un grande esperto di letteratura. Ti vengono dati il titolo e l'autore di un romanzo. "
    "Scrivi una trama concisa ma precisa del libro, {lang}. "
    "Rispondi SOLO con la trama, in 3-6 frasi, senza ripetere il titolo, "
    "senza preamboli, senza citazioni e senza elenchi di personaggi."
)


def groq_plot(title, author, lang, api_key, model):
    system = SYSTEM_PROMPT.format(lang=LANG_INSTRUCTION.get(lang, LANG_INSTRUCTION["it"]))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Titolo: {title}\nAutore: {author}"},
        ],
        "temperature": 0.4,
        "max_tokens": 600,
    }
    req = UrlRequest(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        },
    )
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("reasoning") or "").strip()
    text = re.sub(r"\s+", " ", text).strip()
    return text[:4000] or None


def generate_summary(title, author, lang, api_key):
    if not api_key:
        return None
    lang_key = (lang or "").strip().lower()
    if lang_key not in LANG_INSTRUCTION:
        lang_key = "auto"
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                text = groq_plot(title, author, lang_key, api_key, model)
                if text:
                    return text
                break
            except HTTPError as exc:
                if exc.code == 429:
                    time.sleep(5)
                    continue
                if exc.code == 401:
                    return None
                break
            except Exception:
                break
    return None


TRANSLATE_SYSTEM = (
    "Sei un traduttore. Traduci in {lang} il testo fornito dall'utente, "
    "mantenendo fedelmente il significato e senza aggiungere o togliere informazioni. "
    "Rispondi SOLO con la traduzione, senza preamboli, virgolette o commenti."
)


def groq_translate(text, target, api_key, model):
    system = TRANSLATE_SYSTEM.format(lang=LANG_INSTRUCTION.get(target, LANG_INSTRUCTION["it"]))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }
    req = UrlRequest(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        },
    )
    with urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    out = (msg.get("content") or msg.get("reasoning") or "").strip()
    out = re.sub(r"\s+", " ", out).strip()
    return out[:4000] or None


def translate_summary(text, target, api_key):
    if not api_key or not text:
        return None
    lang_key = (target or "").strip().lower()
    if lang_key not in LANG_INSTRUCTION:
        lang_key = "it"
    for model in GROQ_MODELS:
        for attempt in range(3):
            try:
                out = groq_translate(text, lang_key, api_key, model)
                if out:
                    return out
                break
            except HTTPError as exc:
                if exc.code == 429:
                    time.sleep(5)
                    continue
                if exc.code == 401:
                    return None
                break
            except Exception:
                break
    return None


@app.get("/api/book-lookup")
def book_lookup(title: str = "", author: str = "", lang: str = "auto"):
    title = (title or "").strip()
    author = (author or "").strip()
    if not title:
        return {"success": False, "error": "Missing title."}
    if not author:
        return {"success": False, "error": "Missing author."}
    want = gb_lang(lang)
    api_key = os.environ.get("GROQ_API_KEY")
    desc = None
    for strict in (False, True):
        try:
            desc = google_books_description(title, author, strict=strict, lang=want)
        except Exception:
            desc = None
        if desc:
            break
    if desc:
        return {"success": True, "description": desc, "lang": want or "auto", "source": "Google Books"}
    if want:
        foreign = None
        try:
            foreign = google_books_foreign_description(title, author, want)
        except Exception:
            foreign = None
        if foreign:
            translated = translate_summary(foreign, want.lower(), api_key)
            if translated:
                return {"success": True, "description": translated, "lang": want, "source": "AI translation"}
    return {"success": False, "error": "No matching description found."}


def _extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("risposta senza JSON")
    return json.loads(text[start : end + 1])


RECALL_SYSTEM = (
    "Sei l'assistente di una libreria personale. L'utente ha una lista dei libri che possiede/ha letto, "
    "con id, titolo, autore e, quando presente, un breve riassunto. "
    "L'utente scrive un dettaglio, un frammento o un ricordo vago di uno di questi libri. "
    "Devi individuare il/i libro/i più probabili tra quelli in elenco. "
    "Rispondi SOLO con un oggetto json valido (senza fence né commenti) di questa forma: "
    '{"matches": [{"id": <id numerico>, "reason": "..."}]} '
    "Metti al massimo 3 match, dal più al meno probabile; near pochi match anche solo 1. "
    "reason spiega in 1 frase, nella lingua usata dall'utente, perché potrebbe essere quel libro. "
    "Regola fondamentale: non forzare le corrispondenze. Se un libro non è riconosciuto con ragionevole "
    "certezza escludilo; meglio nessun match che corrispondenze inventate. "
    "Cerca i dettagli nel riassunto (trama) se presente, altrimenti valuta solo titolo e autore. "
    'Se nessun libro della lista è plausibile restituisci {"matches": []}.'
)


def groq_recall(question, library_text, api_key, model):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": RECALL_SYSTEM},
            {"role": "user", "content": f"Libreria:\n{library_text}\n\nRicordo dell'utente:\n{question}"},
        ],
        "temperature": 0.2,
        "max_tokens": 600,
    }
    req = UrlRequest(
        GROQ_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        },
    )
    with urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        text = (msg.get("reasoning") or "").strip()
    obj = _extract_json(text)
    return obj.get("matches", []) or []


@app.get("/api/book-recall")
def book_recall(q: str = ""):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return {"success": False, "error": "Groq key not configured on the server."}
    q = (q or "").strip()
    if not q:
        return {"success": False, "error": "Enter a detail to search."}
    q = q[:600]
    db = SessionLocal()
    try:
        books = db.query(Book).order_by(Book.title.asc()).all()
        bymap = {}
        desc_map = {}
        lines = []
        for b in books:
            desc = (b.description or "").strip().replace("\n", " ")
            if len(desc) > 160:
                long_desc = desc
                desc = desc[:160] + "…"
            else:
                long_desc = desc
            bymap[b.id] = (b.title, b.author or "")
            desc_map[b.id] = long_desc
            lines.append(f"{b.id}: {b.title} — {b.author or '?'}" + (f" Trama: {desc}" if desc else ""))
        library_text = "\n".join(lines)
    finally:
        db.close()
    last_error = None
    for model in ("qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"):
        try:
            matches = groq_recall(q, library_text, api_key, model)
            out = []
            for m in matches:
                try:
                    mid = int(m.get("id"))
                except (TypeError, ValueError):
                    continue
                if mid not in bymap:
                    continue
                title, author = bymap[mid]
                snippet = desc_map.get(mid, "")
                if snippet:
                    snippet = snippet[:140] + ("…" if len(snippet) > 140 else "")
                out.append({
                        "id": mid,
                        "title": title,
                        "author": author,
                        "reason": str(m.get("reason", "")),
                        "snippet": snippet,
                    })
            return {"success": True, "matches": out[:3]}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = f"Groq HTTP {exc.code}: {body}"
            if exc.code in (401, 403):
                return {"success": False, "error": last_error}
            if exc.code == 429:
                return {
                    "success": False,
                    "error": "Groq is temporarily rate-limited: try again in a few tens of seconds.",
                }
            continue
        except Exception as exc:
            last_error = f"Error: {exc}"
            continue
    return {"success": False, "error": last_error or "Could not query the model."}


BATCH = {"running": False, "kind": None, "total": 0, "done": 0, "ok": 0, "fail": 0, "error": None, "errors": []}
BATCH_LOCK = threading.Lock()

OL_SEARCH_URL = "https://openlibrary.org/search.json"
OL_COVER_BY_ID = "https://covers.openlibrary.org/b/id/{}-S.jpg"
OL_COVER_BY_ISBN = "https://covers.openlibrary.org/b/isbn/{}-S.jpg"
COVER_CACHE = Path(__file__).resolve().parent / ".cover_cache"
COVER_CACHE.mkdir(parents=True, exist_ok=True)


def fetch_cover(url):
    req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (BookshelfApp; personal use)"})
    with urlopen(req, timeout=15) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.lower().startswith("image/"):
            return None
        return resp.read()


@app.get("/cover/id/{cover_id}")
def proxy_cover_id(cover_id: str):
    cid = re.sub(r"[^0-9]", "", cover_id)
    if not cid:
        return Response(status_code=404)
    p = COVER_CACHE / f"{cid}.jpg"
    if not p.exists():
        try:
            data = fetch_cover(OL_COVER_BY_ID.format(cid))
            if not data:
                return Response(status_code=404)
            p.write_bytes(data)
        except Exception:
            return Response(status_code=404)
    return Response(content=p.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/cover/isbn/{isbn}")
def proxy_cover_isbn(isbn: str):
    isbn = isbn.strip().upper()
    if not isbn or re.search(r"[^0-9Xx]", isbn):
        return Response(status_code=404)
    p = COVER_CACHE / f"isbn-{isbn}.jpg"
    if not p.exists():
        try:
            data = fetch_cover(OL_COVER_BY_ISBN.format(isbn))
            if not data:
                return Response(status_code=404)
            p.write_bytes(data)
        except Exception:
            return Response(status_code=404)
    return Response(content=p.read_bytes(), media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


def openlibrary_search(title, author="", limit=3):
    q = (title + " " + author).strip() if author else title.strip()
    url = OL_SEARCH_URL + "?" + urlencode({"q": q, "limit": str(limit), "fields": "title,author_name,cover_i,isbn"})
    req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (BookshelfApp; personal use)"})
    data = json.loads(urlopen(req, timeout=10).read().decode("utf-8"))
    for doc in data.get("docs", []):
        if doc.get("cover_i") or doc.get("isbn"):
            return doc
    return None


def gb_lang(language):
    lang = (language or "").strip().upper()
    return lang if lang in ("IT", "EN") else None


def _google_books_items(title, author="", strict=True, lang=None):
    if strict:
        q = 'intitle:"{}"'.format(title.replace('"', " ").strip())
        if author:
            q += ' inauthor:"{}"'.format(author.replace('"', " ").strip())
    else:
        clean = re.sub(r"\s+", " ", title).strip().lower()
        clean = re.sub(r"[^\w\s-]", " ", clean)
        q = re.sub(r"\s+", " ", clean).strip()
        if author:
            q += " " + re.sub(r"\s+", " ", author.strip())
    params = {"q": q, "maxResults": "10", "country": "IT"}
    if lang:
        params["langRestrict"] = lang
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY")
    if api_key:
        params["key"] = api_key
    url = "https://www.googleapis.com/books/v1/volumes?" + urlencode(params)
    for attempt in range(5):
        try:
            req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (BookshelfApp; personal use)"})
            data = json.loads(urlopen(req, timeout=15).read().decode("utf-8"))
            break
        except HTTPError as exc:
            if exc.code in (429, 503) and attempt < 4:
                time.sleep(4)
                continue
            raise
    items = data.get("items", [])
    if strict:
        return items
    return items


def _volume_lang(item):
    vi = item.get("volumeInfo") or {}
    return (vi.get("language") or "").strip().lower()


def _volume_title(item):
    vi = item.get("volumeInfo") or {}
    return (vi.get("title") or "")


def _volume_authors(item):
    vi = item.get("volumeInfo") or {}
    return " ; ".join(vi.get("authors") or [])


def _author_match(author, item):
    qa = re.sub(r"\s+", " ", (author or "").strip().lower())
    if not qa:
        return False
    va = re.sub(r"\s+", " ", _volume_authors(item).strip().lower())
    if not va:
        return False
    if qa in va:
        return True
    return SequenceMatcher(None, qa, va).ratio() >= 0.85


def _title_ratio(title, item):
    return SequenceMatcher(None, title.lower(), _volume_title(item).lower()).ratio()


def google_books_description(title, author="", strict=True, lang=None):
    want = (lang or "").strip().lower() or None
    best = None
    best_ratio = -1.0
    for item in _google_books_items(title, author, strict, lang):
        vi = item.get("volumeInfo") or {}
        desc = (vi.get("description") or "").strip()
        if not desc:
            continue
        if want is not None and _volume_lang(item) != want:
            continue
        ratio = _title_ratio(title, item)
        if not strict and ratio < 0.75:
            continue
        if ratio > best_ratio:
            best_ratio = ratio
            best = desc
    return best


def google_books_foreign_description(title, author, want):
    want = (want or "").strip().lower()
    for item in _google_books_items(title, author, False, None):
        vi = item.get("volumeInfo") or {}
        desc = (vi.get("description") or "").strip()
        if not desc:
            continue
        if not _author_match(author, item):
            continue
        if want and _volume_lang(item) == want:
            continue
        return desc
    return None


def google_books_search(title, author="", strict=True):
    for item in _google_books_items(title, author, strict):
        vi = item.get("volumeInfo") or {}
        imgs = vi.get("imageLinks") or {}
        thumb = imgs.get("thumbnail") or imgs.get("smallThumbnail")
        if not thumb:
            continue
        thumb = thumb.replace("http://", "https://")
        isbn = None
        for ident in vi.get("industryIdentifiers", []):
            if ident.get("type") in ("ISBN_13", "ISBN_10"):
                isbn = ident.get("identifier")
                break
        return {"thumbnail": thumb, "isbn": isbn}
    return None


def ol_search_title_only(title, limit=4):
    url = OL_SEARCH_URL + "?" + urlencode(
        {"q": title, "limit": str(limit), "fields": "title,cover_i,isbn,author_name"})
    req = UrlRequest(url, headers={"User-Agent": "Mozilla/5.0 (BookshelfApp; personal use)"})
    data = json.loads(urlopen(req, timeout=15).read().decode("utf-8"))
    scored = []
    for d in data.get("docs", []):
        dt = (d.get("title") or "").strip()
        if not dt:
            continue
        ratio = SequenceMatcher(None, title.lower(), dt.lower()).ratio()
        if ratio >= 0.5:
            scored.append((ratio, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored]


def _fetch_summary(db, book, api_key):
    reason = None
    desc = None
    want = gb_lang(book.language)
    for strict in (False, True):
        try:
            desc = google_books_description(book.title, book.author or "", strict=strict, lang=want)
        except HTTPError as exc:
            reason = f"Google Books: HTTP {exc.code}"
        except Exception:
            reason = reason or "Network error to Google Books"
        if desc:
            break
    if not desc:
        # fallback: solo titolo, ma con autore verificato (evita match sbagliati)
        try:
            candidates = _google_books_items(book.title, "", strict=False, lang=want)
        except Exception:
            candidates = []
        for item in candidates:
            vi = item.get("volumeInfo") or {}
            text = (vi.get("description") or "").strip()
            if not text:
                continue
            if want and _volume_lang(item) != want.lower():
                continue
            if book.author and not _author_match(book.author, item):
                continue
            desc = text
            reason = None
            break
    if not desc and want:
        foreign = None
        try:
            foreign = google_books_foreign_description(book.title, book.author or "", want)
        except Exception:
            foreign = None
        if foreign and api_key:
            try:
                translated = translate_summary(foreign, want.lower(), api_key)
                if translated:
                    desc = translated
                    reason = None
            except Exception:
                reason = reason or "AI translation failed"
    if desc:
        book.description = desc
        return True, None
    return False, (reason or "No matching description found")


def _fetch_cover(db, book):
    reason = None
    cover = None
    try:
        doc = openlibrary_search(book.title, book.author or "")
    except HTTPError as exc:
        reason = f"Open Library: HTTP {exc.code}"
    except Exception:
        reason = "Network error to Open Library"
    else:
        if doc:
            isbns = doc.get("isbn") or []
            if not book.isbn and isbns:
                book.isbn = str(isbns[0])[:100]
            if doc.get("cover_i"):
                cover = str(doc["cover_i"])
    if cover is None:
        try:
            cands = ol_search_title_only(book.title)
            for cand in cands:
                isbns = cand.get("isbn") or []
                if not book.isbn and isbns:
                    book.isbn = str(isbns[0])[:100]
                if cand.get("cover_i"):
                    cover = str(cand["cover_i"])
                    break
        except Exception:
            pass
    if cover is None:
        gb = None
        try:
            gb = google_books_search(book.title, book.author or "")
        except Exception:
            gb = None
        if gb is None:
            try:
                gb = google_books_search(book.title, book.author or "", strict=False)
            except Exception:
                gb = None
        if gb:
            if not book.isbn and gb.get("isbn"):
                book.isbn = gb["isbn"][:100]
            if gb.get("thumbnail"):
                cover = gb["thumbnail"]
    if cover:
        book.cover_id = cover[:500]
        return True, None
    return False, (reason or "Not found on Open Library or Google Books")


def _run_batch(kind):
    api_key = os.environ.get("GROQ_API_KEY")
    db = SessionLocal()
    try:
        if kind == "trame":
            books = (
                db.query(Book).filter(or_(Book.description.is_(None), Book.description == ""))
                .order_by(Book.title.asc()).all()
            )
        else:
            books = (
                db.query(Book)
                .filter(or_(Book.cover_id.is_(None), Book.cover_id == ""))
                .order_by(Book.title.asc()).all()
            )
        with BATCH_LOCK:
            BATCH.update(kind=kind, total=len(books), done=0, ok=0, fail=0, error=None, errors=[])
        for book in books:
            try:
                if kind == "trame":
                    succeeded, reason = _fetch_summary(db, book, api_key)
                else:
                    succeeded, reason = _fetch_cover(db, book)
            except Exception as exc:
                try:
                    db.rollback()
                except Exception:
                    pass
                succeeded, reason = False, f"Error: {type(exc).__name__}"
            if succeeded:
                try:
                    db.commit()
                    with BATCH_LOCK:
                        BATCH["ok"] += 1
                except Exception:
                    db.rollback()
                    with BATCH_LOCK:
                        BATCH["fail"] += 1
                        if len(BATCH["errors"]) < 250:
                            BATCH["errors"].append({"title": book.title, "reason": reason or "Save failed"})
            else:
                with BATCH_LOCK:
                    BATCH["fail"] += 1
                    if len(BATCH["errors"]) < 250:
                        BATCH["errors"].append({"title": book.title, "reason": reason or "Unresolved"})
            with BATCH_LOCK:
                BATCH["done"] += 1
            time.sleep(6 if kind == "trame" else 0.3)
            if BATCH["done"] % 10 == 0:
                db.rollback()
    except Exception as exc:
        with BATCH_LOCK:
            BATCH["error"] = str(exc)
    finally:
        try:
            db.close()
        except Exception:
            pass
        with BATCH_LOCK:
            BATCH["running"] = False
            BATCH["kind"] = None


def _fill_new_book(book_id):
    """Recupera cover e trama del singolo libro appena aggiunto (in background)."""
    api_key = os.environ.get("GROQ_API_KEY")
    db = SessionLocal()
    try:
        book = db.get(Book, book_id)
        if not book:
            return
        if not (book.cover_id or "").strip():
            try:
                _fetch_cover(db, book)
            except Exception:
                pass
        if not (book.description or "").strip():
            try:
                _fetch_summary(db, book, api_key)
            except Exception:
                pass
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


def _start_batch(kind):
    with BATCH_LOCK:
        if BATCH["running"]:
            return {"success": False, "error": "Batch già in corso."}
        BATCH.update(running=True, kind=kind, total=0, done=0, ok=0, fail=0, error=None, errors=[])
    threading.Thread(target=_run_batch, args=(kind,), daemon=True).start()
    return {"success": True}


def _batch_status():
    with BATCH_LOCK:
        return dict(BATCH)


@app.get("/autofill", response_class=HTMLResponse)
def autofill_page(request: Request, db: Session = Depends(get_db)):
    missing = (
        db.query(func.count(Book.id)).filter(or_(Book.description.is_(None), Book.description == ""))
        .scalar() or 0
    )
    return templates.TemplateResponse(
        "autofill.html",
        {"request": request, "stats": get_stats(db), "missing": missing, "batch": dict(BATCH)},
    )


@app.post("/autofill/start")
def autofill_start():
    return _start_batch("trame")


@app.get("/autofill/status")
def autofill_status():
    return _batch_status()


@app.get("/coverfill", response_class=HTMLResponse)
def coverfill_page(request: Request, db: Session = Depends(get_db)):
    missing = (
        db.query(func.count(Book.id))
        .filter(or_(Book.cover_id.is_(None), Book.cover_id == ""))
        .filter(or_(Book.isbn.is_(None), Book.isbn == ""))
        .scalar() or 0
    )
    return templates.TemplateResponse(
        "coverfill.html",
        {"request": request, "stats": get_stats(db), "missing": missing, "batch": dict(BATCH)},
    )


@app.post("/coverfill/start")
def coverfill_start():
    return _start_batch("copertine")


@app.get("/coverfill/status")
def coverfill_status():
    return _batch_status()


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", location: str = "", tag: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    query = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status, location, tag), sort_by, sort_dir)
    books = query.all()
    years = [y[0] for y in db.query(Book.year_read).filter(Book.year_read.is_not(None), Book.year_read != "").distinct().order_by(Book.year_read.desc()).all()]
    locations = [l[0] for l in db.query(Book.location).filter(Book.location.is_not(None), Book.location != "").distinct().order_by(Book.location.asc()).all()]
    statuses = [s[0] for s in db.query(Book.status_raw).filter(Book.status_raw.is_not(None), Book.status_raw != "").distinct().order_by(Book.status_raw.asc()).all()]
    return templates.TemplateResponse("index.html", {
        "request": request, "books": books, "q": q, "reading_bucket": reading_bucket, "year_read": year_read,
        "status": status, "location": location, "tag": tag, "years": years, "locations": locations, "statuses": statuses,
        "tags": get_tags(db), "stats": get_stats(db),
        "sort_by": sort_by, "sort_dir": sort_dir, "sort_toggle": sort_toggle, "cover_url": cover_url,
    })


@app.get("/api/books")
def api_books(q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", location: str = "", tag: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    books = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status, location, tag), sort_by, sort_dir).all()
    rows = [{
        "id": b.id,
        "title": b.title,
        "author": b.author or "",
        "reading_bucket": b.reading_bucket or "other",
        "year_read": b.year_read or "",
        "location": b.location or "",
        "tags": [t.name for t in b.tags],
        "cover": cover_url(b),
        "language": b.language or "",
        "description": (b.description or ""),
    } for b in books]
    return {"success": True, "total": len(rows), "books": rows}


@app.get("/export/csv")
def export_csv(q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", location: str = "", tag: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    books = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status, location, tag), sort_by, sort_dir).all()
    df = pd.DataFrame(export_rows(books))
    stream = BytesIO(df.to_csv(index=False).encode("utf-8-sig"))
    return StreamingResponse(stream, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=bookshelf-filtered.csv"})


@app.get("/export/xlsx")
def export_xlsx(q: str = "", reading_bucket: str = "", year_read: str = "", status: str = "", location: str = "", tag: str = "", sort_by: str = "title", sort_dir: str = "asc", db: Session = Depends(get_db)):
    books = apply_sort(apply_filters(db.query(Book), q, reading_bucket, year_read, status, location, tag), sort_by, sort_dir).all()
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

    status_rows = db.query(Book.reading_bucket, func.count(Book.id)).group_by(Book.reading_bucket).order_by(func.count(Book.id).desc()).all()
    status_labels = [r[0].replace("_", " ").title() if r[0] else "Unknown" for r in status_rows]
    status_values = [r[1] for r in status_rows]

    location_rows = db.query(Book.location, func.count(Book.id)).filter(Book.location.is_not(None), Book.location != "").group_by(Book.location).order_by(func.count(Book.id).desc()).all()
    location_labels = [r[0] for r in location_rows]
    location_values = [r[1] for r in location_rows]

    author_rows = db.query(Book.author, func.count(Book.id)).filter(Book.author.is_not(None), Book.author != "").group_by(Book.author).order_by(func.count(Book.id).desc()).limit(10).all()
    top_authors = [{"name": a, "count": c} for a, c in author_rows]

    return templates.TemplateResponse("statistics.html", {
        "request": request, "stats": get_stats(db),
        "chart_labels": chart_labels, "chart_values": chart_values,
        "status_labels": status_labels, "status_values": status_values,
        "location_labels": location_labels, "location_values": location_values,
        "top_authors": top_authors,
    })


@app.get("/books/new", response_class=HTMLResponse)
def new_book(request: Request, db: Session = Depends(get_db)):
    locations = [l[0] for l in db.query(Book.location).filter(Book.location.is_not(None), Book.location != "").distinct().order_by(Book.location.asc()).all()]
    return templates.TemplateResponse("form.html", {"request": request, "book": None, "stats": get_stats(db), "locations": locations, "tags": get_tags(db)})


@app.post("/books/new")
def create_book(title: str = Form(...), author: str = Form(""), worth_reading: str = Form(""), status_raw: str = Form(""), year_read: str = Form(""), location: str = Form(""), tags: str = Form(""), language: str = Form(""), isbn: str = Form(""), description: str = Form(""), db: Session = Depends(get_db)):
    book = Book(title=title.strip(), author=clean(author), worth_reading=clean(worth_reading), status_raw=clean(status_raw), reading_bucket=normalize_bucket(status_raw), year_read=clean(year_read), location=normalize_location(location), language=clean(language), isbn=clean(isbn), description=clean(description))
    db.add(book)
    set_book_tags(db, book, tags)
    db.commit()
    if not (book.cover_id or "").strip() or not (book.description or "").strip():
        threading.Thread(target=_fill_new_book, args=(book.id,), daemon=True).start()
    return RedirectResponse(url="/", status_code=303)


@app.get("/books/{book_id}/edit", response_class=HTMLResponse)
def edit_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    locations = [l[0] for l in db.query(Book.location).filter(Book.location.is_not(None), Book.location != "").distinct().order_by(Book.location.asc()).all()]
    return templates.TemplateResponse("form.html", {"request": request, "book": db.get(Book, book_id), "stats": get_stats(db), "locations": locations, "tags": get_tags(db)})


@app.post("/books/{book_id}/edit")
def update_book(book_id: int, title: str = Form(...), author: str = Form(""), worth_reading: str = Form(""), status_raw: str = Form(""), year_read: str = Form(""), location: str = Form(""), tags: str = Form(""), language: str = Form(""), isbn: str = Form(""), description: str = Form(""), db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    book.title = title.strip(); book.author = clean(author); book.worth_reading = clean(worth_reading)
    book.status_raw = clean(status_raw); book.reading_bucket = normalize_bucket(status_raw); book.year_read = clean(year_read); book.location = normalize_location(location)
    book.language = clean(language); book.isbn = clean(isbn); book.description = clean(description)
    set_book_tags(db, book, tags)
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
