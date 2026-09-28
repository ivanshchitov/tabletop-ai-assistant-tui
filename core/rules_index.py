"""Local PDF indexing and retrieval for tabletop game rules."""

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from . import config

EMBEDDING_DIMENSIONS = 384
FIXED_CHUNK_SIZE = 1400
FIXED_CHUNK_OVERLAP = 200
STRUCTURED_CHUNK_SIZE = 1800
DEFAULT_RESULT_LIMIT = 3
MIN_RELEVANCE = 0.10
HEADING_RE = re.compile(
    r"^(?:(?i:chapter|part|section)\s+[\wIVX.-]+(?::\s*.{2,})?|\d+(?:\.\d+)*\.?\s+.{2,}|"
    r"[A-Z][A-Z\s&:,.()'-]{3,})$"
)
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
QUERY_SYNONYMS = {
    "дорога": "road route",
    "дороги": "road roads route routes",
    "строить": "build buildable",
    "построить": "build",
    "строится": "build",
    "карта": "card map",
    "карты": "cards maps",
    "ресурс": "resource",
    "ресурсы": "resources",
    "ход": "turn",
    "хода": "turn",
    "игрок": "player",
    "игрока": "player",
    "игроки": "players",
    "очки": "points score",
    "очко": "point score",
    "бросить": "roll",
    "бросок": "roll dice",
}


class RulesIndexError(Exception):
    """User-facing issue reading or rebuilding the rules index."""


@dataclass(frozen=True)
class PageText:
    number: int
    text: str


@dataclass(frozen=True)
class Chunk:
    source: str
    title: str
    section: str
    chunk_id: str
    text: str
    strategy: str


@dataclass(frozen=True)
class SearchResult:
    source: str
    title: str
    section: str
    chunk_id: str
    text: str
    score: float
    strategy: str


@dataclass(frozen=True)
class IndexReport:
    files: int
    pages: int
    chunks: Dict[str, int]
    words: int


def index_path() -> Path:
    return Path(config.RULES_INDEX_FILE)


def index_exists(database: Path) -> bool:
    if not database.is_file():
        return False
    try:
        with sqlite3.connect(str(database)) as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
            ).fetchone()
            return row is not None and connection.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def extract_sections(page: PageText) -> List[Tuple[str, str]]:
    """Split a PDF page into heading/body pairs, inheriting a page label if needed."""
    sections: List[Tuple[str, str]] = []
    heading = ""
    body: List[str] = []
    first_lines_remaining = 2
    for raw_line in page.text.splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        title_case_heading = (
            first_lines_remaining > 0
            and len(line) <= 60
            and not line.endswith((".", "?", "!", ":", ";"))
            and re.match(r"^[A-Z][a-zA-Z0-9]*(?:\s+[A-Z][a-zA-Z0-9]*){0,6}$", line)
        )
        if len(line) <= 100 and (HEADING_RE.match(line) or title_case_heading):
            if body:
                sections.append((heading or "Page {}".format(page.number), " ".join(body)))
                body = []
            heading = line
        else:
            body.append(line)
        first_lines_remaining -= 1
    if body:
        sections.append((heading or "Page {}".format(page.number), " ".join(body)))
    if not sections and page.text.strip():
        sections.append(("Page {}".format(page.number), page.text.strip()))
    return sections


def _slice_text_with_offsets(text: str, limit: int, overlap: int = 0) -> List[Tuple[int, str]]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [(0, text)]
    step = max(1, limit - overlap)
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundary = text.rfind(" ", start + limit // 2, end)
            if boundary > start:
                end = boundary
        piece = text[start:end].strip()
        if piece:
            pieces.append((start, piece))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return pieces


def _slice_text(text: str, limit: int, overlap: int = 0) -> List[str]:
    return [piece for _, piece in _slice_text_with_offsets(text, limit, overlap)]


def fixed_chunks(
    text: str,
    chunk_size: int = FIXED_CHUNK_SIZE,
    overlap: int = FIXED_CHUNK_OVERLAP,
    sections: Sequence[Tuple[int, str]] = (),
) -> List[Chunk]:
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Размер чанка должен быть больше перекрытия")
    result = []
    for index, (start, piece) in enumerate(
        _slice_text_with_offsets(text, chunk_size, overlap), 1
    ):
        section = "Page 1"
        for section_start, section_name in sections:
            if section_start > start:
                break
            section = section_name
        result.append(Chunk("", "", section, "fixed:{}".format(index), piece, "fixed"))
    return result


def structural_chunks(
    source: str,
    title: str,
    pages: Sequence[PageText],
    chunk_size: int = STRUCTURED_CHUNK_SIZE,
) -> List[Chunk]:
    chunks: List[Chunk] = []
    for page in pages:
        for section, text in extract_sections(page):
            for piece in _slice_text(text, chunk_size):
                chunks.append(
                    Chunk(source, title, section, "", piece, "structural")
                )
    return [
        Chunk(
            chunk.source,
            chunk.title,
            chunk.section,
            "{}:structural:{}".format(source, index),
            chunk.text,
            chunk.strategy,
        )
        for index, chunk in enumerate(chunks, 1)
    ]


def embed(text: str) -> List[float]:
    """Create a deterministic local signed-hash embedding over words and character trigrams."""
    tokens = [token.lower() for token in TOKEN_RE.findall(text)]
    expanded = list(tokens)
    for token in tokens:
        expanded.extend(QUERY_SYNONYMS.get(token, "").split())
    tokens = expanded
    features = list(tokens)
    features.extend("{} {}".format(left, right) for left, right in zip(tokens, tokens[1:]))
    for token in tokens:
        padded = "^{}$".format(token)
        features.extend(padded[index : index + 3] for index in range(max(0, len(padded) - 2)))
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        vector[bucket] += 1.0 if digest[4] & 1 else -1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def read_pdf(path: Path) -> Tuple[str, List[PageText]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RulesIndexError("Для чтения PDF установите зависимость pypdf.") from exc
    try:
        reader = PdfReader(str(path))
        title = (reader.metadata.title if reader.metadata else None) or path.stem
        pages = [PageText(index, page.extract_text() or "") for index, page in enumerate(reader.pages, 1)]
    except Exception as exc:
        raise RulesIndexError("{}: {}".format(path.name, exc)) from exc
    if not any(page.text.strip() for page in pages):
        raise RulesIndexError("{}: не удалось извлечь текст из PDF".format(path.name))
    return title, pages


def normalize_page_text(page: PageText) -> str:
    """Join visual PDF line breaks into readable text while retaining word boundaries."""
    return " ".join(" ".join(line.split()) for line in page.text.splitlines() if line.strip())


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE chunks ("
        "strategy TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL, "
        "section TEXT NOT NULL, chunk_id TEXT NOT NULL, content TEXT NOT NULL, "
        "embedding TEXT NOT NULL, PRIMARY KEY(strategy, chunk_id))"
    )
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def build_index(documents: Path, database: Path) -> IndexReport:
    files = sorted(path for path in documents.glob("*.pdf") if path.is_file())
    if not files:
        raise RulesIndexError("В каталоге нет PDF-документов: {}".format(documents))
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.parent / (database.name + ".tmp")
    temporary.unlink(missing_ok=True)
    counts = {"fixed": 0, "structural": 0}
    pages_count = 0
    word_count = 0
    try:
        with sqlite3.connect(str(temporary)) as connection:
            _create_schema(connection)
            for path in files:
                title, pages = read_pdf(path)
                pages_count += sum(bool(page.text.strip()) for page in pages)
                document_text = "\n\n".join(normalize_page_text(page) for page in pages)
                word_count += len(TOKEN_RE.findall(document_text))
                section_positions = []
                position = 0
                for page in pages:
                    page_text = normalize_page_text(page)
                    section_positions.append((position, "Page {}".format(page.number)))
                    position += len(page_text) + 2
                fixed = fixed_chunks(document_text, sections=section_positions)
                structured = structural_chunks(path.name, title, pages)
                for strategy, chunks in (("fixed", fixed), ("structural", structured)):
                    for chunk in chunks:
                        item = Chunk(
                            path.name,
                            title,
                            chunk.section or title,
                            "{}:{}:{}".format(path.name, strategy, counts[strategy] + 1),
                            chunk.text,
                            strategy,
                        )
                        vector = json.dumps(embed(item.text), separators=(",", ":"))
                        connection.execute(
                            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (strategy, item.source, item.title, item.section, item.chunk_id, item.text, vector),
                        )
                        counts[strategy] += 1
            connection.execute("INSERT INTO metadata VALUES ('files', ?)", (str(len(files)),))
            connection.commit()
        temporary.replace(database)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, RulesIndexError):
            raise
        raise RulesIndexError(str(exc)) from exc
    return IndexReport(len(files), pages_count, counts, word_count)


def search(
    database: Path,
    query: str,
    strategy: str = "structural",
    limit: int = DEFAULT_RESULT_LIMIT,
    minimum_score: float = MIN_RELEVANCE,
) -> List[SearchResult]:
    if strategy not in ("fixed", "structural"):
        raise ValueError("Неизвестная стратегия: {}".format(strategy))
    if not database.is_file():
        return []
    query_vector = embed(query)
    try:
        with sqlite3.connect(str(database)) as connection:
            rows = connection.execute(
                "SELECT source, title, section, chunk_id, content, embedding "
                "FROM chunks WHERE strategy=?",
                (strategy,),
            ).fetchall()
    except sqlite3.Error:
        return []
    results = []
    for source, title, section, chunk_id, content, vector_text in rows:
        score = cosine(query_vector, json.loads(vector_text))
        if score >= minimum_score:
            results.append(SearchResult(source, title, section, chunk_id, content, score, strategy))
    return sorted(results, key=lambda result: result.score, reverse=True)[:limit]


def compare(database: Path, query: str, limit: int = DEFAULT_RESULT_LIMIT) -> Dict[str, List[SearchResult]]:
    return {
        strategy: search(database, query, strategy=strategy, limit=limit, minimum_score=0.0)
        for strategy in ("fixed", "structural")
    }


def strategy_stats(database: Path) -> Dict[str, Dict[str, float]]:
    """Return stored chunk counts and mean character lengths for each strategy."""
    result = {}
    if not database.is_file():
        return {strategy: {"chunks": 0, "average_chars": 0.0} for strategy in ("fixed", "structural")}
    try:
        with sqlite3.connect(str(database)) as connection:
            for strategy in ("fixed", "structural"):
                count, average = connection.execute(
                    "SELECT COUNT(*), COALESCE(AVG(LENGTH(content)), 0) "
                    "FROM chunks WHERE strategy=?",
                    (strategy,),
                ).fetchone()
                result[strategy] = {"chunks": int(count), "average_chars": float(average)}
    except sqlite3.Error:
        return {strategy: {"chunks": 0, "average_chars": 0.0} for strategy in ("fixed", "structural")}
    return result


def corpus_dir() -> Path:
    return Path(config.RULES_DOCUMENTS_DIR)


def context_message(results: Iterable[SearchResult]) -> Optional[str]:
    matches = list(results)
    if not matches:
        return None
    blocks = [
        "[{title}; {section}; {chunk_id}]\n{text}".format(
            title=result.title,
            section=result.section,
            chunk_id=result.chunk_id,
            text=result.text,
        )
        for result in matches
    ]
    return (
        "Используй следующие найденные фрагменты правил как первичные источники. "
        "Если они не отвечают на вопрос, скажи об этом и не выдумывай цитату. "
        "После ответа укажи использованные источники.\n\n" + "\n\n".join(blocks)
    )
