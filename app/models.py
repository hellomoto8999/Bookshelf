import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime, Table, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from .db import Base

book_tags = Table(
    "book_tags",
    Base.metadata,
    Column("book_id", ForeignKey("books.id"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False, index=True)


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
    language = Column(String(10), nullable=True)
    cover_id = Column(String(500), nullable=True)
    description = Column(Text, nullable=True)
    current_page = Column(String(50), nullable=True)
    total_pages = Column(String(50), nullable=True)
    rating = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.utcnow())
    updated_at = Column(DateTime, default=lambda: datetime.datetime.utcnow(), onupdate=lambda: datetime.datetime.utcnow())

    tags = relationship("Tag", secondary=book_tags, backref="books", order_by="Tag.name")
