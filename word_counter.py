"""
word_counter.py

Small dependency-free word counting helpers for Writers Bloc.
Supports .docx, .txt, .md/.markdown, .rtf, and plain pasted/manual text.
Does not support old binary .doc files.
"""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Tuple

SUPPORTED_EXTENSIONS = {".docx", ".txt", ".md", ".markdown", ".rtf"}
WORD_RE = re.compile(r"\b[\w]+(?:['\u2019-][\w]+)*\b", re.UNICODE)


def clean_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>#|]", " ", text)
    return text


def clean_rtf(text: str) -> str:
    text = re.sub(r"\\'[0-9a-fA-F]{2}", " ", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return text


def count_words(text: str) -> int:
    if not text:
        return 0
    return len(WORD_RE.findall(text))


def text_from_docx_bytes(data: bytes) -> str:
    parts_to_read = [
        "word/document.xml",
        "word/footnotes.xml",
        "word/endnotes.xml",
    ]
    output = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for part in parts_to_read:
            if part not in zf.namelist():
                continue
            xml_bytes = zf.read(part)
            root = ET.fromstring(xml_bytes)
            for node in root.iter():
                tag = node.tag.rsplit("}", 1)[-1]
                if tag == "t" and node.text:
                    output.append(node.text)
                elif tag in {"tab", "br", "cr", "p"}:
                    output.append(" ")
    return "".join(output)


def text_from_bytes(filename: str, data: bytes) -> Tuple[str, str]:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext or 'no extension'}")
    if ext == ".docx":
        try:
            return text_from_docx_bytes(data), ext
        except Exception as exc:
            raise ValueError(f"Could not read .docx file: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    if ext in {".md", ".markdown"}:
        text = clean_markdown(text)
    elif ext == ".rtf":
        text = clean_rtf(text)
    return text, ext


def count_words_from_file_bytes(filename: str, data: bytes) -> int:
    text, _ext = text_from_bytes(filename, data)
    return count_words(text)


def is_supported_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS
