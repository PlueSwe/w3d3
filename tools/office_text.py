#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
office_text.py — textextraktion ur Word-dokument utan externa beroenden.

SIRIS publicerar äldre beslut (huvudsakligen 2003–2010) som Word i stället för
PDF. De är fullvärdiga handlingar och måste kunna kopplas till ärende, vilket
kräver att diarienumret går att läsa ur innehållet.

Ambitionsnivån är medvetet låg: det räcker att hitta 'Dnr SI YYYY:NNNN'.
Detta är inte en fullständig Word-tolk och duger inte till fulltextindexering.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import zipfile

# Läsbara byte-intervall för strängextraktion ur binära Word-filer.
_LATIN_RUN = re.compile(rb"[\x20-\x7e\xa0-\xff]{4,}")
_UTF16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")

_DOCX_PARTS = (
    # Sidhuvudet först: där står diarienumret i Word-besluten.
    "word/header1.xml",
    "word/header2.xml",
    "word/header3.xml",
    "word/document.xml",
)


def extract_docx(path: str) -> tuple[str, str]:
    """
    Text ur .docx. Filen är en ZIP där word/document.xml bär brödtexten och
    word/headerN.xml bär sidhuvudet. Taggar strippas och styckebrytningar
    bevaras, så att sidhuvudets Dnr-rad inte klistras ihop med brödtexten.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            parts = [z.read(n).decode("utf-8", "replace")
                     for n in _DOCX_PARTS if n in names]
            if not parts:
                return "", "docx_no_document_xml"
            xml = "\n".join(parts)
    except Exception as exc:
        return "", f"docx_error: {type(exc).__name__}"

    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab[^>]*/>", " ", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    text = (xml.replace("&amp;", "&")
               .replace("&lt;", "<")
               .replace("&gt;", ">")
               .replace("&quot;", '"')
               .replace("&apos;", "'"))
    return text, "docx" if text.strip() else "docx_empty"


def extract_doc(path: str) -> tuple[str, str]:
    """
    Text ur Word 97–2003 (.doc).

    Formatet är en OLE2-container som standardbiblioteket inte kan tolka.
    Finns `antiword` i PATH används den. Annars plockas läsbara
    teckensekvenser ut — Word lagrar text både som cp1252 och som UTF-16LE,
    så båda prövas. Det räcker för att hitta diarienumret.
    """
    if shutil.which("antiword"):
        try:
            r = subprocess.run(["antiword", path], capture_output=True, timeout=30)
            text = r.stdout.decode("utf-8", "replace")
            if text.strip():
                return text, "antiword"
        except Exception:
            pass

    try:
        with open(path, "rb") as f:
            data = f.read(4 << 20)
    except OSError as exc:
        return "", f"read_error: {exc}"

    out: list[str] = []
    for chunk in _UTF16_RUN.findall(data):
        out.append(chunk.decode("utf-16-le", "replace"))
    for chunk in _LATIN_RUN.findall(data):
        out.append(chunk.decode("cp1252", "replace"))
    text = "\n".join(out)
    return text, "doc_strings" if text.strip() else "doc_empty"


def extract_office(path: str) -> tuple[str, str] | None:
    """Returnerar (text, status) för Word-filer, None för andra format."""
    low = path.lower()
    if low.endswith(".docx"):
        return extract_docx(path)
    if low.endswith(".doc"):
        return extract_doc(path)
    return None
