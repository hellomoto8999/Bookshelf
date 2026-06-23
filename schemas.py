from pydantic import BaseModel, Field
from typing import Optional

class BookForm(BaseModel):
    title: str = Field(..., min_length=1)
    author: Optional[str] = None
    release_date: Optional[str] = None
    worth_reading: Optional[str] = None
    reading_status: Optional[str] = None
    year_read: Optional[str] = None
    location: Optional[str] = None
    isbn: Optional[str] = None
    description: Optional[str] = None
    current_page: Optional[str] = None
    total_pages: Optional[str] = None
    rating: Optional[str] = None
    notes: Optional[str] = None
