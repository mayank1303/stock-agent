"""
Ingest trading books (PDF or EPUB) into the local vector store for RAG
search.

Usage:
    1. Drop .pdf or .epub files into the books/ folder
    2. Run: python3 -m rag.ingest_books
    3. Re-run any time you add more books - already-ingested books are
       skipped (tracked by filename), so this is safe to re-run.

This can take a while for many/large books (extracting text + computing
embeddings for every chunk) - it's a one-time cost per book, not
something that runs on every query.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rag.rag_core import BOOKS_DIR, chunk_text, extract_book_text, get_collection, get_embedder

SUPPORTED_EXTENSIONS = (".pdf", ".epub")


def already_ingested(collection, book_name: str) -> bool:
    """Checks if this book's chunks are already in the collection, so
    re-running ingestion doesn't duplicate work or data."""
    existing = collection.get(where={"book": book_name}, limit=1)
    return len(existing["ids"]) > 0


def ingest_book(book_path: Path, collection, embedder) -> int:
    """Ingest one book (PDF or EPUB): extract, chunk, embed, store.
    Returns chunk count."""
    print(f"  Extracting text from {book_path.name}...")
    text = extract_book_text(book_path)

    if not text.strip():
        print(f"  WARNING: no extractable text found in {book_path.name} even after "
              f"OCR fallback. This file may be corrupted, empty, or in an unusual format. Skipping.")
        return 0

    chunks = chunk_text(text)
    print(f"  {len(chunks)} chunks. Computing embeddings...")

    embeddings = embedder.encode(chunks, show_progress_bar=True).tolist()

    ids = [f"{book_path.stem}_{i}" for i in range(len(chunks))]
    metadatas = [{"book": book_path.name, "chunk_index": i} for i in range(len(chunks))]

    collection.add(ids=ids, embeddings=embeddings, documents=chunks, metadatas=metadatas)
    return len(chunks)


if __name__ == "__main__":
    BOOKS_DIR.mkdir(parents=True, exist_ok=True)
    book_files = [
        f for f in BOOKS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not book_files:
        print(f"No .pdf or .epub files found in {BOOKS_DIR}/")
        print("Drop your trading books there, then re-run this script.")
        sys.exit(0)

    print(f"Found {len(book_files)} book(s) in {BOOKS_DIR}/")
    print("Loading embedding model (first run downloads ~80MB, one-time)...")
    embedder = get_embedder()
    collection = get_collection()

    total_new_chunks = 0
    for book_path in book_files:
        if already_ingested(collection, book_path.name):
            print(f"[SKIP] {book_path.name} - already ingested")
            continue

        print(f"[INGEST] {book_path.name}")
        count = ingest_book(book_path, collection, embedder)
        total_new_chunks += count
        print(f"  Done: {count} chunks added.\n")

    print(f"=== Ingestion complete. {total_new_chunks} new chunks added. ===")
    print(f"Total chunks in library: {collection.count()}")