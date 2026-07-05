"""
Core RAG (Retrieval-Augmented Generation) infrastructure for the
trading book library.

Design choices, and why:
- Embeddings: sentence-transformers, running locally (same philosophy
  as Ollama for the LLM - free, no per-book API cost, runs fine on
  your M4). Model: "all-MiniLM-L6-v2" - small (~80MB), fast on CPU,
  a well-established default for this kind of semantic search task.
- Vector store: Chroma, file-based/embedded (like SQLite for vectors -
  no separate server process to run, persists to disk in db/chroma/).
- Chunking: fixed-size character chunks with overlap. Simple and
  robust across very different book formatting/structure - a more
  "aware" chunker (by heading, by paragraph) would need per-book
  tuning that doesn't scale to "drop in 100 PDFs and go."
- OCR (Tesseract, via pytesseract + pdf2image): handles scanned/
  image-only pages, and also OCRs embedded diagrams/charts on normal
  text pages to catch labels/text that live inside an image rather
  than the page's text layer. Requires the `tesseract` and `poppler`
  system packages (brew install tesseract poppler) - the Python
  packages alone (pytesseract, pdf2image) are just wrappers around
  these system binaries and won't work without them installed.

IMPORTANT: this is built for personally-owned PDFs for private research
use. It stores full chunk text locally on your machine to make
retrieval work - it does not redistribute, publish, or share book
content anywhere. When the LLM answers using retrieved passages, it's
instructed (system prompt) to paraphrase and cite, not quote at length -
good practice regardless of the source being personal copies.
"""

import io
import logging
import os
import re
import signal
import socket
from contextlib import contextmanager
from pathlib import Path


class _TimeoutError(Exception):
    pass


@contextmanager
def _timeout(seconds: int):
    """
    Hard timeout for a block of code, using SIGALRM (Unix/macOS only -
    fine for this project). Added after real testing showed a single
    corrupted PDF page could make the OCR rendering step (which shells
    out to poppler) hang for minutes with zero progress output,
    indistinguishable from a true infinite hang without a hard limit.
    """
    def _handler(signum, frame):
        raise _TimeoutError(f"Timed out after {seconds}s")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

import chromadb
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
from pypdf import PdfReader
from ebooklib import epub
import ebooklib
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rag_core")


def _has_internet(host: str = "8.8.8.8", port: int = 53, timeout: float = 1.5) -> bool:
    """
    Quick connectivity check: tries to open a socket to Google's public
    DNS (always-on, fast to fail if there's no route) with a short
    timeout. Used once at startup to decide: use the model's normal
    online behavior when internet is available, or force offline mode
    when it isn't - rather than always forcing one or the other.
    """
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except OSError:
        return False


_ONLINE = _has_internet()

if _ONLINE:
    logger.info("Internet detected - embedding model will use normal online behavior "
                 "(e.g. checking Hugging Face for cache freshness).")
else:
    # No internet: force offline mode so the model loads from the local
    # cache instead of trying (and failing/retrying) to reach the network.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    # CONFIRMED BUG (via real testing, WiFi fully off): transformers'
    # find_adapter_config_file() checks for an optional PEFT adapter
    # config on every model load, and ignores BOTH offline env vars
    # above for this specific check - it still attempts a network
    # request (and retries 5x on failure) even with both set. Patching
    # it out is the only fix that actually worked in real testing.
    # Only applied when we've already detected no internet, since when
    # online this check just succeeds normally and doesn't need patching.
    try:
        import transformers.utils.peft_utils as _peft_utils
        _peft_utils.find_adapter_config_file = lambda *args, **kwargs: None
        logger.info("No internet detected - forcing offline mode and patching the known adapter-config bug.")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not patch adapter-config check ({e}) - offline loading may still attempt network access.")

from sentence_transformers import SentenceTransformer  # noqa: E402 - must import after the offline setup above

BOOKS_DIR = Path(__file__).parent.parent / "books"
CHROMA_DIR = Path(__file__).parent.parent / "db" / "chroma"

CHUNK_SIZE = 1000       # characters per chunk
CHUNK_OVERLAP = 150     # overlap so an idea split across a chunk boundary isn't lost entirely
MIN_TEXT_LEN_BEFORE_OCR_FALLBACK = 20  # below this, treat a page as likely scanned/image-only
MAX_IMAGE_READ_FAILURES_PER_BOOK = 5   # circuit breaker for corrupted PDFs (see extract_pdf_text)

_embedder = None  # lazy-loaded - loading the model takes a moment, don't pay that cost on every import


def get_embedder() -> SentenceTransformer:
    """
    Loads the embedding model. Online/offline mode is decided once at
    module load time based on real connectivity (see _ONLINE check
    above) - online uses normal Hugging Face behavior, offline forces
    cache-only loading plus a targeted bug patch (see comment above).
    This function's try/except is just a safety net for the rare case
    where the connectivity check was wrong (e.g. internet dropped
    between the check and this call) or this is genuinely the very
    first run with no cached model yet.
    """
    global _embedder
    if _embedder is None:
        try:
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            logger.info("Model not cached locally yet - downloading (needs internet, one-time)...")
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
    return _embedder


def get_collection():
    """Returns the Chroma collection, creating it (and the on-disk
    store) if it doesn't exist yet.

    anonymized_telemetry=False disables Chroma's default behavior of
    sending anonymous usage pings to PostHog on every client startup -
    a second, separate source of network calls independent of the
    Hugging Face embedding-model check fixed in get_embedder() above.
    This is a well-documented thing people run into with Chroma;
    disabling it here makes the whole RAG pipeline fully offline after
    the embedding model's one-time download."""
    from chromadb.config import Settings

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
    return client.get_or_create_collection(name="trading_books")


def _ocr_image(image: Image.Image) -> str:
    """Run OCR on a single image, never raising - a failed OCR attempt
    on one image/page shouldn't abort ingesting the rest of the book."""
    try:
        return pytesseract.image_to_string(image).strip()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"OCR failed on an image: {e}")
        return ""


def extract_pdf_text(pdf_path: Path) -> str:
    """
    Extract text from a PDF, with two OCR fallbacks layered on top of
    normal text extraction:

    1. Scanned/image-only pages: if a page's normal text extraction
       comes back nearly empty, render that page as an image and OCR
       the whole page. Catches books (or chapters) that are just
       scanned photos of printed pages, with no real text layer.
    2. Embedded diagrams/charts: even on normal text pages, any
       embedded images (chart screenshots, annotated diagrams) are
       separately OCR'd - this catches labels/text WITHIN a diagram
       that pypdf's normal text extraction never sees at all, since it
       only reads the text layer, not image content.

    Both are best-effort: OCR failures on individual pages/images are
    logged and skipped, never crash the whole ingestion run.

    Circuit breaker: some PDFs (especially re-saved/compressed copies
    from certain download sites) have heavily corrupted internal object
    references. Each corrupted image reference makes pypdf attempt slow
    internal recovery - found via real testing that this can make a
    single bad book take a very long time (had to be manually
    interrupted). After MAX_IMAGE_READ_FAILURES_PER_BOOK failures,
    embedded-image OCR is disabled for the rest of THIS book (text-layer
    extraction, which works fine independently, continues normally).
    """
    reader = PdfReader(str(pdf_path))
    page_texts = []
    image_read_failures = 0
    skip_image_ocr = False

    for page_num, page in enumerate(reader.pages):
        if page_num % 10 == 0:
            logger.info(f"  Processing page {page_num + 1}/{len(reader.pages)} of {pdf_path.name}...")

        # page.extract_text() itself can raise on malformed content
        # (confirmed via real testing: a PdfReadError on a broken
        # inline image embedded directly in the page's content stream
        # crashed the whole ingestion run). Catch it here and treat
        # this page as empty text - the scanned-page OCR fallback right
        # below will then try to recover it via page image rendering
        # instead, same as it already does for genuinely blank pages.
        try:
            text = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Normal text extraction crashed on page {page_num + 1} of {pdf_path.name}: {e}. "
                             f"Falling back to OCR for this page.")
            text = ""

        # Fallback 1: page looks scanned (suspiciously little/no text) -
        # render the whole page as an image and OCR it. Wrapped in a
        # hard 30s timeout: confirmed via real testing that a corrupted
        # PDF can make poppler's rendering (convert_from_path shells out
        # to it) hang for minutes with zero progress output on a single
        # bad page, indistinguishable from a true infinite hang without
        # this limit.
        if len(text.strip()) < MIN_TEXT_LEN_BEFORE_OCR_FALLBACK:
            try:
                with _timeout(30):
                    rendered_pages = convert_from_path(
                        str(pdf_path), first_page=page_num + 1, last_page=page_num + 1
                    )
                if rendered_pages:
                    ocr_text = _ocr_image(rendered_pages[0])
                    if ocr_text:
                        text = ocr_text
            except _TimeoutError:
                logger.warning(f"Page-level OCR fallback TIMED OUT (>30s) on page {page_num + 1} "
                                 f"of {pdf_path.name} - skipping this page's OCR, moving on.")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Page-level OCR fallback failed on page {page_num + 1} of {pdf_path.name}: {e}")

        # Fallback 2: OCR any embedded images on this page too (catches
        # text/labels inside diagrams and charts, even on pages that
        # otherwise have a normal, already-extracted text layer).
        image_texts = []
        if not skip_image_ocr:
            try:
                for img in page.images:
                    try:
                        pil_image = Image.open(io.BytesIO(img.data))
                        ocr_text = _ocr_image(pil_image)
                        if ocr_text:
                            image_texts.append(ocr_text)
                    except Exception:
                        continue  # a single undecodable embedded image shouldn't stop the rest
            except Exception as e:  # noqa: BLE001
                image_read_failures += 1
                logger.warning(f"Reading embedded images failed on page {page_num + 1} of {pdf_path.name}: {e}")
                if image_read_failures >= MAX_IMAGE_READ_FAILURES_PER_BOOK:
                    skip_image_ocr = True
                    logger.warning(
                        f"{image_read_failures} image-read failures in {pdf_path.name} - "
                        f"this PDF likely has corrupted internal structure (common with some "
                        f"re-saved/downloaded copies). Disabling embedded-image OCR for the rest "
                        f"of this book to avoid a very slow run; normal text extraction continues."
                    )

        combined = text + ("\n" + "\n".join(image_texts) if image_texts else "")
        page_texts.append(combined)

    return "\n".join(page_texts)


def extract_epub_text(epub_path: Path) -> str:
    """
    Extract text from an EPUB. EPUBs are ZIP archives of HTML content -
    ebooklib reads the structure, BeautifulSoup strips HTML tags to
    plain text. Unlike PDFs, EPUBs rarely have a "scanned page" problem
    (the format is inherently text/HTML-based), so no page-level OCR
    fallback is needed here - but embedded images (diagrams, charts)
    are still OCR'd for the same reason as PDFs: a diagram's labels/
    text live in the image, not in any text node ebooklib can read.
    """
    book = epub.read_epub(str(epub_path))
    section_texts = []

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content(), "html.parser")
            text = soup.get_text(separator=" ", strip=True)
            if text:
                section_texts.append(text)
        elif item.get_type() == ebooklib.ITEM_IMAGE:
            try:
                pil_image = Image.open(io.BytesIO(item.get_content()))
                ocr_text = _ocr_image(pil_image)
                if ocr_text:
                    section_texts.append(ocr_text)
            except Exception:
                continue  # an undecodable embedded image shouldn't stop the rest

    return "\n".join(section_texts)


def extract_book_text(book_path: Path) -> str:
    """
    Dispatches to the right extractor based on file extension. This is
    the function ingest_books.py should call - it doesn't need to know
    or care whether a given file is a PDF or EPUB.
    """
    suffix = book_path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(book_path)
    elif suffix == ".epub":
        return extract_epub_text(book_path)
    else:
        raise ValueError(f"Unsupported book format: {suffix} (supported: .pdf, .epub)")


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Simple fixed-size chunking with overlap. Collapses excessive
    whitespace first (PDF extraction often produces messy spacing)."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks


def search_books(query: str, top_k: int = 5) -> list[dict]:
    """
    Semantic search over the ingested book library. Returns the top_k
    most relevant chunks with their source book and a relevance score.

    Returns an empty list (not an error) if no books have been
    ingested yet - a valid, normal state before you've run ingest.py.
    """
    collection = get_collection()
    if collection.count() == 0:
        return []

    embedder = get_embedder()
    query_vector = embedder.encode(query).tolist()

    results = collection.query(query_embeddings=[query_vector], n_results=top_k)

    hits = []
    for i in range(len(results["ids"][0])):
        hits.append({
            "text": results["documents"][0][i],
            "book": results["metadatas"][0][i].get("book", "unknown"),
            "chunk_index": results["metadatas"][0][i].get("chunk_index"),
            "distance": results["distances"][0][i],  # lower = more relevant
        })
    return hits