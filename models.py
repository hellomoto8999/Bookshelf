from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func
from .db import Base

class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(255), nullable=False, index=True)
    author = Column(String(255), nullable=True, index=True)
    release_date = Column(String(50), nullable=True)
    worth_reading = Column(String(100), nullable=True)
    status_raw = Column(String(255), nullable=True, index=True)
    reading_bucket = Column(String(50), nullable=True, index=True)
    year_read = Column(String(20), nullable=True, index=True)
    location = Column(String(255), nullable=True)
    isbn = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    current_page = Column(String(50), nullable=True)
    total_pages = Column(String(50), nullable=True)
    rating = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
