"""Runbook and operational-history search over the knowledge documents synced from S3.

The knowledge bucket holds the seed runbooks and the documents uploaded per workload. "지식 동기화" in the Web UI
writes a manifest; the runtime downloads the listed documents into KNOWLEDGE_DIR and this module ranks
heading-level sections with BM25, so no embedding model or vector store is needed.
"""
import json
import math
import os
import re
from collections import Counter
from pathlib import Path

KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/tmp/incident/knowledge"))
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log"}
TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:/-]*|[가-힣]+")


def _tokens(text):
    return TOKEN.findall(text.lower())


def _sections(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix != ".md":
        yield path.stem, text
        return
    title, lines = path.stem, []
    for line in text.splitlines():
        if line.startswith("#") and lines:
            yield title, "\n".join(lines)
            title, lines = line.lstrip("#").strip() or title, []
        lines.append(line)
    if lines:
        yield title, "\n".join(lines)


def _corpus():
    if not KNOWLEDGE_DIR.is_dir():
        return []
    chunks = []
    for path in sorted(KNOWLEDGE_DIR.rglob("*")):
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            for title, text in _sections(path):
                chunks.append({"source": str(path.relative_to(KNOWLEDGE_DIR)), "title": title, "text": text,
                               "terms": Counter(_tokens(title + " " + text))})
    return chunks


def search_knowledge(query: str) -> str:
    """Search runbooks, system specs and uploaded workload documents for the given incident description.

    Args:
        query: Incident description or keywords (alarm name, resource type, symptom)
    """
    chunks = _corpus()
    if not chunks:
        return "Knowledge base is empty. Upload documents and run the knowledge sync."
    terms = set(_tokens(query))
    average = sum(sum(c["terms"].values()) for c in chunks) / len(chunks)
    frequency = Counter(term for c in chunks for term in set(c["terms"]))
    scored = []
    for chunk in chunks:
        length, score = sum(chunk["terms"].values()), 0.0
        for term in terms:
            count = chunk["terms"].get(term, 0)
            if count:
                idf = math.log(1 + (len(chunks) - frequency[term] + 0.5) / (frequency[term] + 0.5))
                score += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)
    return json.dumps([{"source": c["source"], "title": c["title"], "text": c["text"][:1500], "score": round(s, 3)}
                       for s, c in scored[:5]], ensure_ascii=False)


KNOWLEDGE = [search_knowledge]
