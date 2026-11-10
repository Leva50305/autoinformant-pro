#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StopListMaker v11 — исправленный генератор route.txt для OMSI 1 / OMSI 2 / OpenOMSI.

Важные принципы:
- HOF разбирается по именованным секциям, а не по предположениям о строках.
- [addbusstop_list] (OMSI 2) и [addbusstop] (классический формат) обрабатываются отдельно.
- В [infosystem_trip] сохраняются код, описание, код конечной точки и поле номера линии.
- Числовые ссылки на остановки разрешаются через глобальные остановки, когда возможно.
- Буквы на табло зависят от скрипта матрицы автобуса. HOF global_strings даёт коды
  для особых групп линий, но сам по себе не задаёт универсальное соответствие кода букве.
- Папки считаются собственными только при наличии в .stoplistmaker.json.
- Чужие папки никогда не перезаписываются и не удаляются.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from typing import Callable, Iterable, Optional

APP_TITLE = "StopListMaker v11 — исправленный HOF parser"
MARKER_FILENAME = ".stoplistmaker.json"
MARKER_VERSION = 1


# ---------------------------------------------------------------------------
# Windows DPI / paths / encoding
# ---------------------------------------------------------------------------

def fix_dpi() -> None:
    """Try to make Tkinter DPI-aware on Windows. Safe to call on other platforms."""
    if os.name != "nt":
        return

    # Windows 10+ per-monitor awareness. Keep older API fallbacks for Windows 7/8.
    try:
        fn = ctypes.windll.user32.SetProcessDpiAwarenessContext
        if fn(ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return
    except Exception:
        pass

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def get_base_dir() -> str:
    """Executable folder for frozen builds; script folder when run from Python."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _encoding_score(text: str, encoding: str) -> int:
    """Heuristic among ANSI encodings, following OpenOMSI's language-aware approach."""
    cyrillic = sum(1 for ch in text if "\u0400" <= ch <= "\u052f")
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z") or ch in "äöüÄÖÜßłŁąĄćĆęĘńŃśŚźŹżŻčČřŘžŽ")
    replacement = text.count("\ufffd")
    # Russian CP1251 typically creates runs of 3+ Cyrillic letters. A German/Polish
    # CP1252/1250 byte decoded as CP1251 may produce isolated Cyrillic glyphs, but not
    # Russian-looking runs (the distinction used by openOMSI's codepage detector).
    long_cyrillic_runs = sum(len(match.group(0)) for match in re.finditer(r"[\u0400-\u052f]{3,}", text))
    score = 3 * cyrillic + latin - 100 * replacement
    if cyrillic and long_cyrillic_runs < max(3, int(cyrillic * 0.65)):
        score -= 20 + 2 * cyrillic
    # A Western/Central-European file decoded as CP1251 can contain a few accidental
    # Cyrillic characters next to ordinary Latin letters. Russian text usually has a
    # higher Cyrillic-to-Latin ratio over the whole file.
    if cyrillic and latin and cyrillic < latin * 0.65:
        score -= 20
    if encoding == "cp866" and cyrillic:
        score -= 5
    if encoding == "cp1252" and cyrillic:
        score -= 12
    if encoding == "cp1251" and latin and not cyrillic:
        score -= 4
    # Prefer CP1250 when the characteristic Polish/Czech letters are present.
    if encoding == "cp1250" and any(ch in text for ch in "łŁąĄśŚźŹżŻčČřŘžŽ"):
        score += 10
    return score


def read_text(filepath: str) -> Optional[str]:
    """Read HOF text with BOM/UTF-8 support and practical ANSI fallbacks."""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError:
        raise

    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16")

    # Always attempt strict UTF-8 before a single-byte encoding. This prevents the
    # classic bug where UTF-8 files were silently garbled by CP1251 (which never fails).
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    candidates: list[tuple[int, str, str]] = []
    for encoding in ("cp1251", "cp1252", "cp1250", "cp866", "latin-1"):
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        candidates.append((_encoding_score(text, encoding), encoding, text))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][2]


# ---------------------------------------------------------------------------
# HOF structural parser
# ---------------------------------------------------------------------------

@dataclass
class HofSection:
    name: str
    start: int
    end: int
    payload: list[str]


def _trim_hof_line(line: str) -> str:
    """OMSI trims trailing spaces/tabs/quotes in HOF lines; keep leading data intact."""
    return line.rstrip(" \t\r\n\"")


def _section_name(line: str) -> Optional[str]:
    """Return a section name only for a standalone keyword at column zero.

    The format documentation notes that indented pseudo-keywords can occur in help
    text/comments. They must not accidentally start a real section.
    """
    if not line.startswith("[") or not line.endswith("]"):
        return None
    if "\t" in line or "\n" in line or "\r" in line:
        return None
    name = line[1:-1].strip()
    if not name or "[" in name or "]" in name:
        return None
    return name.casefold()


def _build_sections(content: str) -> tuple[list[str], list[HofSection]]:
    lines = [_trim_hof_line(line) for line in content.splitlines()]
    if lines and lines[0].startswith("\ufeff"):
        lines[0] = lines[0].lstrip("\ufeff")

    headers: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        name = _section_name(line)
        if name is not None:
            headers.append((idx, name))

    sections: list[HofSection] = []
    for pos, (start, name) in enumerate(headers):
        end = headers[pos + 1][0] if pos + 1 < len(headers) else len(lines)
        sections.append(HofSection(name, start, end, lines[start + 1:end]))
    return lines, sections


def _find_sections(sections: Iterable[HofSection], name: str) -> list[HofSection]:
    wanted = name.casefold()
    return [section for section in sections if section.name == wanted]


def _first_nonempty_index(lines: list[str]) -> Optional[int]:
    for idx, line in enumerate(lines):
        if line.strip():
            return idx
    return None


def _parse_int(value: str) -> Optional[int]:
    try:
        text = value.strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return None
        return int(text)
    except (TypeError, ValueError):
        return None


def _read_declared_integer(sections: list[HofSection], name: str) -> Optional[int]:
    """Read bare HOF parameters such as ``stringcount_busstop``.

    Important: these are NOT section headers and must not be looked up as
    ``[stringcount_busstop]``. OMSI stores them as an ordinary line followed by
    an integer, usually inside the preceding [servicetrip] block.
    """
    wanted = name.casefold()
    for section in sections:
        payload = section.payload
        for idx, raw in enumerate(payload):
            if raw.strip().casefold() != wanted:
                continue
            for value_line in payload[idx + 1:]:
                if not value_line.strip():
                    continue
                value = _parse_int(value_line)
                if value is not None and 0 <= value <= 999:
                    return value
                break
    return None

def _read_global_strings(sections: list[HofSection]) -> list[str]:
    """Read [global_strings]: count line followed by that many positional strings.

    Empty values matter because indices 3/4/5 are the special codes for 900/800/500
    line groups. Therefore empty rows after the count are deliberately preserved.
    """
    matches = _find_sections(sections, "global_strings")
    if not matches:
        return []
    payload = matches[0].payload
    count_idx = _first_nonempty_index(payload)
    if count_idx is None:
        return []
    count = _parse_int(payload[count_idx])
    if count is None or count < 0:
        return []
    values = payload[count_idx + 1:count_idx + 1 + count]
    # HOF lines are trimmed at the right by _trim_hof_line; preserve left padding in
    # display strings, but remove surrounding whitespace for folder/code globals.
    return [value for value in values]


@dataclass
class StopRecord:
    name: str
    index: Optional[int] = None
    source: str = ""


def _split_tabbed_stop_row(line: str) -> tuple[Optional[int], str]:
    """Parse standard OMSI 2 stop row or a common indexed table variant."""
    cells = line.split("\t")
    first = cells[0].strip() if cells else ""
    second = cells[1].strip() if len(cells) > 1 else ""
    first_int = _parse_int(first)

    # Official [addbusstop_list] rows have the stop identifier in column 1.
    # A few exporter variants use index<TAB>identifier<TAB>tail; recognize that
    # only when a nonnumeric second cell is present.
    if first_int is not None and second and _parse_int(second) is None:
        return first_int, second
    return None, first


def _parse_global_addbusstop_list(sections: list[HofSection]) -> list[StopRecord]:
    """Parse OMSI 2 [addbusstop_list].

    Per the HOF format, each line is ``identifier<TAB>display-string...``. The
    first cell is the stop identifier; the remaining cells are presentation
    variants, not extra stops. A few community exporters produce an indexed
    ``index<TAB>identifier<TAB>...`` table; detect that only at block level when
    virtually all first cells are integers.
    """
    records: list[StopRecord] = []
    for section in _find_sections(sections, "addbusstop_list"):
        rows: list[tuple[str, list[str]]] = []
        for raw in section.payload:
            if raw.strip().casefold() == "[end]":
                break
            if not raw.strip():
                continue
            cells = raw.split("\t")
            first = cells[0].strip().strip('"')
            if first:
                rows.append((first, [cell.strip().strip('"') for cell in cells[1:]]))
        if not rows:
            continue

        integer_firsts = sum(_parse_int(first) is not None for first, _ in rows)
        indexed_variant = (
            integer_firsts >= max(2, int(len(rows) * 0.90))
            and any(rest and rest[0] and _parse_int(rest[0]) is None for _, rest in rows)
        )
        for position, (first, rest) in enumerate(rows):
            if indexed_variant and rest and rest[0] and _parse_int(rest[0]) is None:
                index = _parse_int(first)
                name = rest[0]
            else:
                index = None
                name = first
            if name:
                records.append(StopRecord(name=name, index=index, source="addbusstop_list"))
    return records

def _parse_classic_addbusstop(
    sections: list[HofSection],
    string_count: Optional[int],
    log_fn: Optional[Callable[[str], None]] = None,
) -> list[StopRecord]:
    """Parse classic OMSI 1 [addbusstop] records.

    In the standard format each [addbusstop] starts one record: first line is
    the internal stop identifier; the following stringcount_busstop values are
    display/announcement strings. They are NOT separate stop names.

    A known legacy/exporter variant writes ``index, identifier, display strings``
    in one block. It is recognized when the first value is an integer and the
    second is text; the first value then supplies an explicit index.
    """
    records: list[StopRecord] = []
    for section in _find_sections(sections, "addbusstop"):
        payload = section.payload
        # Preserve positional rows but ignore leading/empty separator lines.
        first_idx = _first_nonempty_index(payload)
        if first_idx is None:
            continue
        first = payload[first_idx].strip().strip('"')
        if not first or _section_name(first) is not None:
            continue

        explicit_index: Optional[int] = None
        name = first
        # Indexed legacy record: index + stop name + string tail. If a real
        # exporter writes this form, its numeric identifier is distinguishable.
        second_idx = first_idx + 1
        while second_idx < len(payload) and not payload[second_idx].strip():
            second_idx += 1
        if _parse_int(first) is not None and second_idx < len(payload):
            second = payload[second_idx].strip().strip('"')
            if second and _parse_int(second) is None and not second.startswith('['):
                explicit_index = _parse_int(first)
                name = second

        if name:
            records.append(StopRecord(name=name, index=explicit_index, source="addbusstop"))

    # Older templates sometimes omit stringcount_busstop. Since each standard
    # [addbusstop] block is still one record, we can parse identifiers safely.
    if records and string_count is None and log_fn:
        log_fn("  ⚠ Не найден параметр stringcount_busstop; распознал первые строки стандартных [addbusstop]-записей. Число строковых вариантов отображения неизвестно.")
    return records

def _count_termini(sections: list[HofSection]) -> int:
    """Count destination records, for diagnosing old HOFs without route data."""
    classic = len(_find_sections(sections, "addterminus")) + len(_find_sections(sections, "addterminus_allexit"))
    list_count = 0
    for section_name in ("addterminus_list",):
        for section in _find_sections(sections, section_name):
            for row in section.payload:
                if row.strip() and row.strip().casefold() != "[end]":
                    list_count += 1
    return classic + list_count


def _make_stop_index_map(records: list[StopRecord]) -> dict[int, str]:
    """Use explicit indices where present; otherwise use OMSI's zero-based row order."""
    result: dict[int, str] = {}
    for position, record in enumerate(records):
        idx = record.index if record.index is not None else position
        if idx not in result and record.name:
            result[idx] = record.name
    return result


def _dedupe_names(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = name.strip()
        if not cleaned:
            continue
        key = re.sub(r"\s+", " ", cleaned).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def _parse_route_stop_rows(payload: list[str], log_fn: Optional[Callable[[str], None]] = None) -> list[str]:
    """Read [infosystem_busstop_list] without swallowing comments/trailers.

    The first data row in the standard HOF layout is the number of following
    stops. Some HOF Creator exports append human-readable comments after those
    stops, so exact equality between count and physical non-empty rows is not a
    valid requirement. Once a plausible declared count is present, it controls
    the stop rows; trailing notes are ignored.
    """
    rows: list[str] = []
    for raw in payload:
        line = raw.strip()
        if not line or line.casefold() == "[end]":
            continue
        cells = line.split("\t")
        values = [cell.strip().strip('"') for cell in cells]
        values = [value for value in values if value != ""]
        if not values:
            continue
        # Exporter-specific row variant: index<TAB>stop identifier.
        if len(values) >= 2 and _parse_int(values[0]) is not None and _parse_int(values[1]) is None:
            rows.append(values[1])
        else:
            rows.append(values[0])

    if not rows:
        return []

    declared = _parse_int(rows[0])
    # In OMSI's documented layout this is the required number-of-stops line.
    # Prefer the declared size even if optional explanatory rows follow it.
    if declared is not None and 0 <= declared <= 1000:
        available = len(rows) - 1
        if declared <= available:
            return rows[1:1 + declared]
        if log_fn:
            log_fn(
                f"  ⚠ В [infosystem_busstop_list] объявлено {declared} остановок, "
                f"но доступно только {available}; использую имеющиеся строки."
            )
        return rows[1:]

    # If no plausible count exists, tolerate an exporter variant containing only
    # stop identifiers. Keep the values verbatim for numeric-index resolution later.
    return rows

def _resolve_numeric_stop_indices(stops: list[str], index_map: dict[int, str], global_records: list[StopRecord],
                                  log_fn: Optional[Callable[[str], None]] = None) -> list[str]:
    """Resolve numeric references, primarily using the zero-based OMSI order.

    If and only if the route contains a reference equal to the number of global stops
    and has no zero reference, the index set cannot be zero-based; use one-based as a
    compatibility fallback for that nonstandard exporter variant.
    """
    if not stops or not index_map:
        return stops
    numeric = [_parse_int(stop) for stop in stops]
    numeric_values = [n for n in numeric if n is not None]
    # One-based indexing is only a compatibility fallback for plain ordered lists.
    # If the HOF/exporter supplied explicit indices, those indices win verbatim.
    has_explicit_indices = any(record.index is not None for record in global_records)
    one_based_variant = (
        not has_explicit_indices
        and bool(numeric_values)
        and 0 not in numeric_values
        and bool(global_records)
        and max(numeric_values) == len(global_records)
        and all(1 <= n <= len(global_records) for n in numeric_values)
    )

    if one_based_variant and log_fn:
        log_fn("  ⚠ В списке остановок обнаружены индексы 1..N без нуля; применён совместимый с экспортёрами вариант индексации с единицы.")

    resolved: list[str] = []
    for stop, value in zip(stops, numeric):
        if value is None:
            resolved.append(stop)
            continue
        idx = value - 1 if one_based_variant else value
        if idx in index_map:
            resolved.append(index_map[idx])
        elif value in index_map:
            # Keep a useful fallback for explicitly indexed rows where sequential order
            # and the explicit index are different.
            resolved.append(index_map[value])
        else:
            resolved.append(stop)
    return resolved


def _split_route_code(code: str) -> tuple[str, str]:
    """Return (line-prefix, two-digit course) when the last two digits are a course."""
    text = str(code or "").strip()
    if re.fullmatch(r"\d{3,}", text):
        return text[:-2], text[-2:]
    return "", ""

def _compact_route_label(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def _is_placeholder_label(value: str) -> bool:
    """Reject obvious template placeholders, not real route labels."""
    compact = _compact_route_label(value).upper()
    if not compact:
        return True
    if compact in {"X", "XX", "XXX", "XXXX", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "UNBEKANNT", "-", "?"}:
        return True
    # Maps often use XXX06 / XXX01 as an explicit placeholder in the description.
    # It is not a passenger-facing line number and must not become a folder name.
    if re.fullmatch(r"X{2,}\d{0,4}[A-Z]?", compact):
        return True
    return False


def _is_explicit_route_label(value: str) -> bool:
    """True only for a compact, explicit line label such as A031, T02 or 12A."""
    compact = _compact_route_label(value)
    if not compact or _is_placeholder_label(compact):
        return False
    return bool(
        re.fullmatch(r"[A-Za-zА-ЯЁа-яё]{1,3}\d{1,4}[A-Za-zА-ЯЁа-яё]?", compact)
        or re.fullmatch(r"\d{1,4}[A-Za-zА-ЯЁа-яё]", compact)
    )


def _join_label_and_course(label: str, course_part: str) -> str:
    """Combine a bare prefix with a route suffix without duplicating full labels.

    Examples: ``A03`` + ``01`` -> ``A031``; ``T`` + ``02`` -> ``T02``;
    explicit full labels ``A031`` and ``T02`` remain unchanged.
    """
    label = _compact_route_label(label)
    if not label:
        return ""
    if not course_part:
        return label.upper() if label.isascii() else label

    if re.fullmatch(r"[A-Za-zА-ЯЁа-яё]{1,3}", label):
        # A bare prefix like T needs the two-digit course intact: T + 02 = T02.
        result = label + course_part
        return result.upper() if result.isascii() else result

    match = re.fullmatch(r"([A-Za-zА-ЯЁа-яё]{1,3})(\d{1,4})", label)
    if match:
        digits = match.group(2)
        if len(digits) >= 3 or digits.endswith(course_part) or digits.endswith(str(int(course_part))):
            return label.upper() if label.isascii() else label
        result = label + str(int(course_part))
        return result.upper() if result.isascii() else result

    if re.fullmatch(r"\d{1,4}[A-Za-zА-ЯЁа-яё]", label):
        return label.upper() if label.isascii() else label
    return label.upper() if label.isascii() else label


def _make_display_name(
    code: str,
    description: str,
    line_label: str,
    global_strings: Optional[list[str]] = None,
) -> str:
    """Derive a display/folder name without turning placeholders into route names.

    Source priority:
      1. An explicit, non-placeholder line label from the fourth trip field.
      2. A description only when it consists of an explicit label/prefix itself.
      3. Classic numeric fallback: a five-digit route code becomes NNN-NN.
         Other codes are preserved intact, matching the original v8 behaviour.

    global_strings contains the HOF's IBIS codes for special line groups (900/800/500),
    not a universal code-to-letter map. A letter is used only when the HOF explicitly
    contains it; the actual letter mapping is vehicle/matrix-script-specific.
    """
    clean_code = str(code or "").strip()
    line_text = _compact_route_label(str(line_label or ""))
    description_text = str(description or "").strip()
    description_compact = _compact_route_label(description_text)
    line_part, course_part = _split_route_code(clean_code)

    # Ignore placeholders such as XXX, XXX01, XXX06 before checking letter-only labels.
    valid_line = bool(line_text) and not _is_placeholder_label(line_text)
    if valid_line:
        if _is_explicit_route_label(line_text):
            joined = _join_label_and_course(line_text, course_part)
            if joined:
                return joined
        if re.fullmatch(r"[A-Za-zА-ЯЁа-яё]{1,3}", line_text):
            joined = _join_label_and_course(line_text, course_part)
            if joined:
                return joined
        if re.fullmatch(r"\d{1,4}", line_text):
            # The trip field repeats the passenger-facing line number. Do not
            # manufacture a prefix from the description when this is numerical.
            if course_part:
                return f"{line_text}-{course_part}"
            return line_text

    # Only trust a description if the whole description is a compact label or prefix.
    if description_compact and not _is_placeholder_label(description_compact):
        if _is_explicit_route_label(description_compact):
            # A description such as A03 can be a prefix; combine it with the course.
            match = re.fullmatch(r"([A-Za-zА-ЯЁа-яё]{1,3})(\d{1,3})", description_compact)
            if match and course_part and not description_compact.endswith(course_part):
                joined = _join_label_and_course(description_compact, course_part)
                if joined:
                    return joined
            return description_compact.upper() if description_compact.isascii() else description_compact
        if re.fullmatch(r"[A-Za-zА-ЯЁа-яё]{1,3}", description_compact) and course_part:
            joined = _join_label_and_course(description_compact, course_part)
            if joined:
                return joined

    digits = re.sub(r"\D", "", clean_code)
    if len(digits) == 5:
        return f"{digits[:3]}-{digits[3:]}"
    # Preserve legacy short/irregular codes rather than guessing how to split them.
    return digits or clean_code or "unknown"

def _parse_trip_fields(section: HofSection) -> dict[str, str]:
    payload = list(section.payload)
    # Ignore leading empty lines before the code, but retain an empty description if it
    # is genuinely the next parameter line.
    start = _first_nonempty_index(payload)
    if start is None:
        return {"code": "", "description": "", "destination_code": "", "line_label": ""}
    fields = payload[start:]
    # Remove only trailing blank separator lines; internal empties are positional.
    while fields and not fields[-1].strip():
        fields.pop()
    if not fields:
        return {"code": "", "description": "", "destination_code": "", "line_label": ""}

    first = fields[0]
    if "\t" in first:
        tab_fields = first.split("\t")
        code = tab_fields[0].strip()
        if len(tab_fields) > 1:
            rest = [cell.strip() for cell in tab_fields[1:]]
            while len(rest) < 3:
                rest.append("")
            description = rest[0]
            destination_code = rest[1]
            line_label = rest[2]
            return {"code": code, "description": description, "destination_code": destination_code, "line_label": line_label}

    code = fields[0].strip()
    description = fields[1].strip() if len(fields) > 1 else ""
    destination_code = fields[2].strip() if len(fields) > 2 else ""
    line_label = fields[3].strip() if len(fields) > 3 else ""
    return {"code": code, "description": description, "destination_code": destination_code, "line_label": line_label}


def _parse_trip_sections(sections: list[HofSection], global_stops: list[StopRecord], global_strings: list[str], log_fn: Optional[Callable[[str], None]] = None) -> list[dict]:
    trip_sections = _find_sections(sections, "infosystem_trip")
    stop_list_sections = _find_sections(sections, "infosystem_busstop_list")
    if not trip_sections:
        return []

    # Resolve references using explicit IDs where present, and row order otherwise.
    stop_index_map = _make_stop_index_map(global_stops)
    trips: list[dict] = []
    for trip_section in trip_sections:
        fields = _parse_trip_fields(trip_section)
        code = fields["code"]
        if not code:
            if log_fn:
                log_fn(f"  ⚠ Пустой код рейса в секции на строке {trip_section.start + 1}; рейс пропущен.")
            continue

        # Find the first stop-list section after this trip and before the next trip.
        next_trip_start = min((s.start for s in trip_sections if s.start > trip_section.start), default=10**12)
        list_section = next((s for s in stop_list_sections if trip_section.start < s.start < next_trip_start), None)
        raw_stops = _parse_route_stop_rows(list_section.payload, log_fn) if list_section else []
        resolved_stops = _resolve_numeric_stop_indices(raw_stops, stop_index_map, global_stops, log_fn)
        unresolved_numeric = [value for value in resolved_stops if _parse_int(value) is not None]
        if unresolved_numeric and log_fn:
            log_fn(
                f"  ⚠ Рейс {code}: не удалось разрешить числовые ссылки на остановки "
                f"{', '.join(unresolved_numeric)}. В HOF отсутствует соответствующая глобальная запись "
                "или индекс выходит за её пределы; исходные значения сохранены."
            )

        description = fields["description"]
        line_label = fields["line_label"]
        display_name = _make_display_name(code, description, line_label, global_strings)
        trip = {
            "code": code,
            "description": description,
            "destination_code": fields["destination_code"],
            "line_label": line_label,
            "display_name": display_name,
            "stops": [stop.strip() for stop in resolved_stops if stop.strip()],
            "raw_stop_refs": raw_stops,
            "has_stop_list": list_section is not None,
            "section_line": trip_section.start + 1,
            "global_strings": list(global_strings),
        }
        trips.append(trip)
        if list_section is None and log_fn:
            log_fn(f"  ⚠ Рейс {code}: после [infosystem_trip] не найден [infosystem_busstop_list].")
        elif not trip["stops"] and log_fn:
            log_fn(f"  ⚠ Рейс {code}: список остановок пуст или не распознан.")
    return trips


def parse_hof(filepath: str, log_fn: Optional[Callable[[str], None]] = None) -> tuple[list[dict], list[str]]:
    """Return (trips, unique_stops) after parsing both old and new HOF structures."""
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    try:
        content = read_text(filepath)
        if content is None:
            log(f"  ✗ Не удалось декодировать HOF: {filepath}")
            return [], []
        if not content.strip():
            log(f"  ✗ HOF пустой: {filepath}")
            return [], []

        _, sections = _build_sections(content)
        if not sections:
            log("  ✗ В файле не найдено ни одной секции HOF вида [keyword].")
            return [], []

        busstop_string_count = _read_declared_integer(sections, "stringcount_busstop")
        global_strings = _read_global_strings(sections)
        modern_records = _parse_global_addbusstop_list(sections)
        classic_records = _parse_classic_addbusstop(sections, busstop_string_count, log)

        # Prefer the OMSI 2 indexed/order-preserving list for reference resolution; merge
        # additional classic records only when their names are not already present.
        if modern_records:
            global_records = list(modern_records)
            known = {re.sub(r"\s+", " ", record.name.strip()).casefold() for record in global_records}
            for record in classic_records:
                key = re.sub(r"\s+", " ", record.name.strip()).casefold()
                if key not in known:
                    global_records.append(StopRecord(record.name, None, record.source))
                    known.add(key)
        else:
            global_records = list(classic_records)

        trips = _parse_trip_sections(sections, global_records, global_strings, log)
        all_names: list[str] = [record.name for record in global_records]
        for trip in trips:
            all_names.extend(trip.get("stops", []))

        # Independently collect every stop-list section as recovery path. This can
        # recover readable stop names even if trip metadata is missing or malformed.
        index_map = _make_stop_index_map(global_records)
        trip_list_sections = _find_sections(sections, "infosystem_busstop_list")
        for stop_section in trip_list_sections:
            names_from_section = _parse_route_stop_rows(stop_section.payload, log)
            all_names.extend(_resolve_numeric_stop_indices(names_from_section, index_map, global_records, log))

        all_stops = _dedupe_names(all_names)
        terminus_count = _count_termini(sections)
        if not trips and not all_stops:
            if terminus_count:
                log(
                    f"  ⚠ Найдено конечных пунктов: {terminus_count}, но нет ни [infosystem_trip], "
                    "ни [infosystem_busstop_list], ни определений остановок [addbusstop]/[addbusstop_list]."
                )
                log(
                    "  ⛔ Из этого HOF невозможно восстановить маршруты и последовательности остановок: "
                    "в файле записаны только конечные пункты (destination/terminus). "
                    "Нужен другой HOF с маршрутными списками или источник данных расписания."
                )
            else:
                log("  ⚠ HOF не содержит распознанных маршрутов или списка остановок.")
        elif not trips and all_stops:
            log("  ⚠ Список остановок найден, но [infosystem_trip] отсутствует: папки маршрутов создать не из чего.")

        if global_strings:
            def global_value(index: int) -> str:
                return global_strings[index].strip() if index < len(global_strings) else ""
            log(f"  [global_strings]: объявлено {len(global_strings)} значений.")
            log(
                "  Коды особых групп (не являются буквами сами по себе): "
                f"900={global_value(3) or '—'}, 800={global_value(4) or '—'}, 500={global_value(5) or '—'}"
            )
        else:
            log("  [global_strings]: секция отсутствует (допустимо для OMSI 1).")

        log(f"  Глобальные остановки: OMSI 2 [addbusstop_list] — {len(modern_records)}, классический [addbusstop] — {len(classic_records)}.")
        log(f"  Рейсов: {len(trips)}")
        log(f"  Рейсов с остановками: {sum(1 for trip in trips if trip.get('stops'))} / {len(trips)}")
        log(f"  Итого уникальных остановок: {len(all_stops)}")
        if trips:
            for trip in trips[:8]:
                log(
                    f"  Маршрут: код={trip.get('code', '')!r}; "
                    f"описание={trip.get('description', '')!r}; "
                    f"поле линии={trip.get('line_label', '')!r}; "
                    f"имя папки={trip.get('display_name', '')!r}; "
                    f"остановок={len(trip.get('stops', []))}"
                )
            if len(trips) > 8:
                log(f"  … ещё {len(trips) - 8} рейсов не показаны в диагностике.")
        return trips, all_stops

    except Exception as exc:
        log(f"  ✗ Ошибка парсинга HOF: {exc}")
        log(traceback.format_exc())
        return [], []


def parse_hof_files(hof_paths: list[str], log_fn: Optional[Callable[[str], None]] = None,
                    base_dir: Optional[str] = None) -> tuple[list[dict], list[str]]:
    """Parse and merge one or more HOF files for one map directory.

    Maps and map packs may contain several depot-specific HOFs. Parsing only the first
    filename silently hides routes from the other depots, so scan all files, union stop
    names and collapse only trips with the same route code *and* same ordered stop list.
    A same-code trip with a different stop sequence is kept as a distinct variation.
    """
    merged_trips: list[dict] = []
    merged_stops: list[str] = []
    trip_lookup: dict[tuple[str, tuple[str, ...]], int] = {}

    for hof_path in hof_paths:
        if base_dir:
            try:
                source_name = os.path.relpath(hof_path, base_dir)
            except ValueError:
                source_name = os.path.basename(hof_path)
        else:
            source_name = os.path.basename(hof_path)
        if log_fn:
            log_fn(f"\n═══ Сканируем HOF: {source_name} ═══")
        parsed_trips, stops = parse_hof(hof_path, log_fn)
        merged_stops.extend(stops)

        for trip in parsed_trips:
            item = dict(trip)
            item["source_hof"] = source_name
            item["source_hofs"] = [source_name]
            code_key = re.sub(r"\s+", "", str(item.get("code", ""))).casefold()
            stop_key = tuple(
                re.sub(r"\s+", " ", str(stop).strip()).casefold()
                for stop in item.get("stops", [])
            )
            signature = (code_key, stop_key)

            if signature in trip_lookup:
                existing_idx = trip_lookup[signature]
                existing = merged_trips[existing_idx]
                if source_name not in existing.get("source_hofs", []):
                    existing.setdefault("source_hofs", []).append(source_name)
                if log_fn:
                    log_fn(
                        f"  ↳ Дубликат рейса {item.get('code', '?')} из {source_name}: "
                        "тот же код и тот же список остановок; повтор объединён."
                    )
                # Prefer metadata that has an explicit real line label, while preserving
                # all source filenames. Descriptions like XXX06 are placeholders.
                def quality(route: dict) -> int:
                    label = str(route.get("line_label", "")).strip()
                    description = str(route.get("description", "")).strip()
                    score = 0
                    if _is_explicit_route_label(label):
                        score += 8
                    elif re.fullmatch(r"[A-Za-zА-ЯЁа-яё]{1,3}", _compact_route_label(label)) and not _is_placeholder_label(label):
                        score += 6
                    if _is_explicit_route_label(description):
                        score += 4
                    if label and re.fullmatch(r"\d{1,4}", label):
                        score += 2
                    if description and not _is_placeholder_label(description):
                        score += 1
                    return score
                if quality(item) > quality(existing):
                    sources = list(existing.get("source_hofs", []))
                    item["source_hofs"] = sources
                    item["source_hof"] = sources[0] if sources else source_name
                    merged_trips[existing_idx] = item
                continue

            trip_lookup[signature] = len(merged_trips)
            merged_trips.append(item)

    return merged_trips, _dedupe_names(merged_stops)


# ---------------------------------------------------------------------------
# Safe route generation and ownership marker
# ---------------------------------------------------------------------------

def sanitize_folder_name(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip().rstrip(". ")
    value = re.sub(r"\s+", " ", value)
    if value in {"", ".", ".."}:
        return "unknown"
    # Windows reserves these names even when followed by an extension.
    stem = value.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "_" + value
    return value[:150]


def format_folder_name(trip: dict, naming_style: str) -> str:
    code = str(trip.get("code") or "unknown").strip()
    if naming_style == "raw":
        return sanitize_folder_name(code or "unknown")
    return sanitize_folder_name(str(trip.get("display_name") or code or "unknown"))


def make_route_folder_names(trips: list[dict], naming_style: str) -> list[str]:
    """Deterministic names; append code only if two different trips collide."""
    base_names = [format_folder_name(trip, naming_style) for trip in trips]
    counts: dict[str, int] = {}
    for name in base_names:
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1

    used: set[str] = set()
    result: list[str] = []
    for idx, (trip, base) in enumerate(zip(trips, base_names)):
        candidate = base
        if counts.get(base.casefold(), 0) > 1 or candidate.casefold() in used:
            code = sanitize_folder_name(str(trip.get("code") or f"trip{idx + 1}"))
            candidate = sanitize_folder_name(f"{base}__{code}")
            serial = 2
            while candidate.casefold() in used:
                candidate = sanitize_folder_name(f"{base}__{code}_{serial}")
                serial += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result


def _read_marker(marker_path: str) -> dict:
    if not os.path.isfile(marker_path):
        return {"version": MARKER_VERSION, "created_routes": []}
    try:
        with open(marker_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("marker must be a JSON object")
        routes = data.get("created_routes", [])
        if not isinstance(routes, list):
            routes = []
        clean_routes = [item for item in routes if isinstance(item, str) and item]
        data["version"] = data.get("version", MARKER_VERSION)
        data["created_routes"] = list(dict.fromkeys(clean_routes))
        return data
    except Exception:
        # A damaged marker must not cause folders to be treated as ours. Return an
        # empty ownership list to preserve the most important rule: don't touch unknowns.
        return {"version": MARKER_VERSION, "created_routes": []}


def _write_marker_atomic(marker_path: str, marker: dict) -> None:
    directory = os.path.dirname(marker_path)
    fd, temp_path = tempfile.mkstemp(prefix=".stoplistmaker-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(marker, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, marker_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def generate_routes(map_dir: str, trips: list[dict], naming_style: str, overwrite_mine: bool,
                    log_fn: Callable[[str], None]) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    marker_path = os.path.join(map_dir, MARKER_FILENAME)
    marker = _read_marker(marker_path)
    created_routes = marker.setdefault("created_routes", [])
    owned_names = {str(name).casefold() for name in created_routes if isinstance(name, str)}
    names = make_route_folder_names(trips, naming_style)

    created: list[str] = []
    updated: list[str] = []
    existed: list[str] = []
    skipped: list[str] = []
    empty_stops: list[str] = []

    for trip, folder_name in zip(trips, names):
        code = str(trip.get("code") or "").strip()
        if not code:
            continue
        folder_path = os.path.join(map_dir, folder_name)
        is_ours = folder_name.casefold() in owned_names
        exists = os.path.exists(folder_path)

        if exists and not is_ours:
            skipped.append(folder_name)
            log_fn(f"  [ЧУЖОЙ]  {folder_name} — пропущен, папка не отмечена в маркере")
            continue

        if exists and is_ours and not overwrite_mine:
            existed.append(folder_name)
            log_fn(f"  [ЕСТЬ]   {folder_name} — перезапись отключена")
            continue

        try:
            # exist_ok doesn't remove any contents. We only write route.txt in an owned
            # folder or in a folder that did not exist before this run.
            os.makedirs(folder_path, exist_ok=True)
            announcements_dir = os.path.join(folder_path, "Анонсы")
            os.makedirs(announcements_dir, exist_ok=True)

            route_txt = os.path.join(folder_path, "route.txt")
            stops = [str(stop).strip() for stop in trip.get("stops", []) if str(stop).strip()]
            with open(route_txt, "w", encoding="utf-8-sig", newline="\r\n") as f:
                for stop in stops:
                    f.write(stop + "\n")

            if folder_name.casefold() not in owned_names:
                created_routes.append(folder_name)
                owned_names.add(folder_name.casefold())
            # Persist ownership immediately after each successfully written route, not
            # just at the end: interrupted generation then leaves fewer untracked folders.
            try:
                _write_marker_atomic(marker_path, marker)
            except Exception as marker_exc:
                log_fn(f"  [ПРЕДУПР.] Не удалось обновить маркер для {folder_name}: {marker_exc}")

            if not stops:
                empty_stops.append(folder_name)
                log_fn(f"  [ПУСТО]  {folder_name} ({code}) — 0 остановок")
            elif exists:
                updated.append(folder_name)
                log_fn(f"  [ОБНОВ.] {folder_name} ({code}) — {len(stops)} ост.")
            else:
                created.append(folder_name)
                log_fn(f"  [СОЗДАН] {folder_name} ({code}) — {len(stops)} ост.")
        except Exception as exc:
            log_fn(f"  [ОШИБКА] {folder_name}: {exc}")
            log_fn(traceback.format_exc())

    # Even if no route was generated, preserve the existing marker. If there are no
    # previous markers and no route was created, don't create an empty file needlessly.
    if created_routes:
        try:
            _write_marker_atomic(marker_path, marker)
        except Exception as exc:
            log_fn(f"  [ОШИБКА] Не удалось сохранить {MARKER_FILENAME}: {exc}")
            log_fn(traceback.format_exc())

    return created, updated, existed, skipped, empty_stops


def discover_hof_files(map_dir: str) -> list[str]:
    """Find HOFs in a map root and in conventional nested Hof/Hofs folders.

    Some map packs, notably depot-based releases, ship the HOF files in a dedicated
    ``Hof/<depot>/`` tree rather than directly beside ``global.cfg``. We search those
    conventional containers recursively without walking every scenery/object folder.
    """
    found: dict[str, str] = {}
    try:
        for entry in os.scandir(map_dir):
            if entry.is_file() and entry.name.casefold().endswith(".hof"):
                full = os.path.abspath(entry.path)
                found[os.path.normcase(full)] = full
    except OSError:
        return []

    conventional_dirs = {"hof", "hofs", "depot", "depots", "hof files", "hof-files"}
    try:
        top_entries = list(os.scandir(map_dir))
    except OSError:
        top_entries = []

    for entry in top_entries:
        if not entry.is_dir() or entry.name.casefold() not in conventional_dirs:
            continue
        for root, dirs, files in os.walk(entry.path):
            # Avoid hidden/system directories and generated route folders if a pack has
            # an unusual nested path. The search stays within an explicit HOF container.
            dirs[:] = [name for name in dirs if not name.startswith(".")]
            for name in files:
                if name.casefold().endswith(".hof"):
                    full = os.path.abspath(os.path.join(root, name))
                    found[os.path.normcase(full)] = full

    return sorted(found.values(), key=lambda path: os.path.relpath(path, map_dir).casefold())


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

class StopListMakerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1180x860")
        self.minsize(920, 660)

        self.base_dir = get_base_dir()
        self.maps_dir = os.path.join(self.base_dir, "maps")
        os.makedirs(self.maps_dir, exist_ok=True)

        self.maps_data: dict[str, dict] = {}
        self.map_iid_to_name: dict[str, str] = {}
        self.naming_style = tk.StringVar(value="pretty")
        self.overwrite_mine = tk.BooleanVar(value=True)

        self._build_ui()
        self.naming_style.trace_add("write", self._on_naming_style_changed)
        self.refresh_maps()

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=5)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="🔄 Обновить", command=self.refresh_maps).pack(side="left", padx=2)
        ttk.Button(toolbar, text="📁 Импорт HOF...", command=self.import_hof).pack(side="left", padx=2)
        ttk.Label(toolbar, text=f"maps: {self.maps_dir}", foreground="#666").pack(side="right", padx=5)

        main = ttk.PanedWindow(self, orient="vertical")
        main.pack(fill="both", expand=True, padx=5, pady=5)

        tree_frame = ttk.Frame(main)
        main.add(tree_frame, weight=5)
        ttk.Label(tree_frame, text="Карты и рейсы:", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        tree_container = ttk.Frame(tree_frame)
        tree_container.pack(fill="both", expand=True, pady=(3, 0))
        self.tree = ttk.Treeview(tree_container, show="tree", selectmode="browse")
        self.tree.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        vsb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.tag_configure("map", font=("Segoe UI", 10, "bold"), background="#E8E8E8")
        self.tree.tag_configure("new", foreground="#0055CC")
        self.tree.tag_configure("ours", foreground="#008800")
        self.tree.tag_configure("foreign", foreground="#CC0000")
        self.tree.tag_configure("empty", foreground="#BB8800")
        self.tree.tag_configure("no_hof", foreground="#999999", font=("Segoe UI", 10, "italic"))

        settings = ttk.LabelFrame(main, text="Настройки", padding=6)
        main.add(settings, weight=0)
        row1 = ttk.Frame(settings)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="Имя папки:").pack(side="left", padx=(0, 6))
        ttk.Radiobutton(row1, text="Понятное из HOF (A031, T02; иначе 019-02)",
                        variable=self.naming_style, value="pretty").pack(side="left", padx=3)
        ttk.Radiobutton(row1, text="Сырой код (01902)", variable=self.naming_style,
                        value="raw").pack(side="left", padx=3)

        row2 = ttk.Frame(settings)
        row2.pack(fill="x", pady=2)
        ttk.Checkbutton(row2, text="Перезаписывать route.txt в моих уже созданных маршрутах",
                        variable=self.overwrite_mine).pack(side="left")
        ttk.Label(row2, text="Чужие папки не трогаем.", foreground="#666").pack(side="left", padx=15)

        actions = ttk.Frame(main, padding=5)
        main.add(actions, weight=0)
        ttk.Button(actions, text="✓ Сгенерировать выбранную карту",
                   command=self.generate_selected).pack(side="left", padx=2)
        ttk.Button(actions, text="✓ Сгенерировать ВСЕ карты",
                   command=self.generate_all).pack(side="left", padx=2)
        ttk.Button(actions, text="👁 Все остановки карты",
                   command=self.show_all_stops).pack(side="left", padx=(15, 2))

        log_frame = ttk.LabelFrame(self, text="Лог", padding=3)
        log_frame.pack(fill="both", expand=False, padx=5, pady=(0, 5))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=11, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    def log(self, message: str) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", str(message) + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
            self.update_idletasks()
        except Exception:
            print(message)

    def _show_exception(self, title: str, exc: BaseException) -> None:
        details = f"{exc}\n\n{traceback.format_exc()}"
        self.log(f"✗ {title}: {exc}")
        self.log(traceback.format_exc())
        try:
            messagebox.showerror(title, details, parent=self)
        except Exception:
            pass

    def _on_naming_style_changed(self, *_args) -> None:
        # Rebuild route labels so the tree always matches the selected folder naming mode.
        if hasattr(self, "tree"):
            self.refresh_maps(parse_again=False)

    def refresh_maps(self, parse_again: bool = True) -> None:
        try:
            for item in self.tree.get_children(""):
                self.tree.delete(item)
            self.map_iid_to_name.clear()
            if parse_again:
                # Re-scan below and rebuild maps_data.
                self.maps_data.clear()

            if not os.path.isdir(self.maps_dir):
                self.log(f"✗ Папка maps не найдена: {self.maps_dir}")
                return

            map_names = sorted(
                (entry.name for entry in os.scandir(self.maps_dir) if entry.is_dir()),
                key=str.casefold,
            )
            for map_idx, map_name in enumerate(map_names):
                map_path = os.path.join(self.maps_dir, map_name)
                hof_paths = discover_hof_files(map_path)
                hof_labels = [os.path.relpath(path, map_path) for path in hof_paths]

                if parse_again or map_name not in self.maps_data:
                    if not hof_paths:
                        data = {"hof_path": None, "hof_paths": [], "trips": [], "all_stops": [], "dir": map_path}
                    else:
                        if len(hof_paths) > 1:
                            self.log(
                                f"⚠ {map_name}: найдено несколько HOF ({len(hof_paths)}): "
                                f"{', '.join(hof_labels)}. Объединяю маршруты и остановки всех файлов."
                            )
                        combined_trips, combined_stops = parse_hof_files(hof_paths, self.log, base_dir=map_path)
                        data = {
                            "hof_path": hof_paths[0],
                            "hof_paths": hof_paths,
                            "trips": combined_trips,
                            "all_stops": combined_stops,
                            "dir": map_path,
                        }
                    self.maps_data[map_name] = data
                else:
                    data = self.maps_data[map_name]

                map_iid = f"map_iid_{map_idx}"
                self.map_iid_to_name[map_iid] = map_name
                trips = data.get("trips", [])
                stops = data.get("all_stops", [])
                if not data.get("hof_path"):
                    map_text = f"📁 {map_name}  (нет HOF)"
                    self.tree.insert("", "end", iid=map_iid, text=map_text, tags=("no_hof",))
                    continue

                if not trips and not stops:
                    map_text = f"📁 {map_name}  —  нет списков рейсов/остановок в HOF"
                elif not trips:
                    map_text = f"📁 {map_name}  —  0 рейсов / {len(stops)} остановок (нет [infosystem_trip])"
                else:
                    map_text = f"📁 {map_name}  —  {len(trips)} рейсов / {len(stops)} остановок"
                self.tree.insert("", "end", iid=map_iid, text=map_text, tags=("map",))
                marker = _read_marker(os.path.join(map_path, MARKER_FILENAME))
                created_routes = {str(item).casefold() for item in marker.get("created_routes", [])}
                folder_names = make_route_folder_names(trips, self.naming_style.get())

                for idx, (trip, folder_name) in enumerate(zip(trips, folder_names)):
                    code = trip.get("code", "?")
                    display = trip.get("display_name", code)
                    exists = os.path.exists(os.path.join(map_path, folder_name))
                    is_ours = folder_name.casefold() in created_routes
                    count = len(trip.get("stops", []))
                    if count == 0:
                        tag, status = "empty", "⚠ 0 остановок"
                    elif exists and is_ours:
                        tag, status = "ours", "✓ создан"
                    elif exists and not is_ours:
                        tag, status = "foreign", "⚠ чужой"
                    else:
                        tag, status = "new", "· новый"

                    # IIDs are built from ordinal positions rather than filenames/codes;
                    # they remain unique even when route codes repeat or include symbols.
                    route_iid = f"trip_iid_{map_idx}_{idx}"
                    extra = f" — {trip.get('description')}" if trip.get("description") else ""
                    source_hof = trip.get("source_hof", "")
                    if len(data.get("hof_paths", [])) > 1 and source_hof:
                        extra += f" [HOF: {source_hof}]"
                    self.tree.insert(map_iid, "end", iid=route_iid,
                                     text=f"    {display}   [код: {code}]   — {count} ост.   {status}{extra}",
                                     tags=(tag,))

            self.log(f"✓ Сканирование завершено. Карт: {len(self.maps_data)}")
        except Exception as exc:
            self._show_exception("Ошибка сканирования StopListMaker", exc)

    def import_hof(self) -> None:
        try:
            path = filedialog.askopenfilename(
                title="Выберите HOF файл",
                filetypes=[("HOF файлы", "*.hof"), ("Все файлы", "*.*")],
                parent=self,
            )
            if not path:
                return
            default_name = os.path.splitext(os.path.basename(path))[0]
            map_name = simpledialog.askstring("Имя карты", "Имя папки в maps/:",
                                              initialvalue=default_name, parent=self)
            if not map_name:
                return
            map_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", map_name).strip().rstrip(". ")
            if not map_name:
                messagebox.showerror("Ошибка", "Пустое имя папки карты.", parent=self)
                return

            map_dir = os.path.join(self.maps_dir, map_name)
            os.makedirs(map_dir, exist_ok=True)
            destination = os.path.join(map_dir, os.path.basename(path))
            if os.path.abspath(path) != os.path.abspath(destination):
                if os.path.exists(destination):
                    ok = messagebox.askyesno("Файл уже существует",
                                             f"Файл уже есть:\n{destination}\n\nПерезаписать HOF-файл?",
                                             parent=self)
                    if not ok:
                        return
                shutil.copy2(path, destination)
            self.log(f"✓ HOF импортирован: maps/{map_name}/{os.path.basename(path)}")
            self.refresh_maps()
        except Exception as exc:
            self._show_exception("Ошибка импорта HOF", exc)

    def _selected_map(self) -> Optional[str]:
        selection = self.tree.selection()
        if not selection:
            return None
        iid = selection[0]
        while iid:
            if iid in self.map_iid_to_name:
                return self.map_iid_to_name[iid]
            iid = self.tree.parent(iid)
        return None

    def generate_selected(self) -> None:
        try:
            map_name = self._selected_map()
            if not map_name:
                messagebox.showinfo("Информация", "Выберите карту или рейс внутри карты.", parent=self)
                return
            data = self.maps_data.get(map_name)
            if not data or not data.get("hof_path"):
                messagebox.showwarning("Нет HOF", f"Для карты «{map_name}» не найден HOF-файл.", parent=self)
                return
            self.log(f"\n═══ Генерация: {map_name} ═══")
            c, u, e, s, em = generate_routes(
                data["dir"], data["trips"], self.naming_style.get(), self.overwrite_mine.get(), self.log
            )
            self.log(f"Итог: создано {len(c)}, обновлено {len(u)}, уже было {len(e)}, чужих {len(s)}, пустых {len(em)}")
            self.refresh_maps(parse_again=False)
        except Exception as exc:
            self._show_exception("Ошибка генерации маршрутов", exc)

    def generate_all(self) -> None:
        try:
            if not self.maps_data:
                messagebox.showinfo("Информация", "В папке maps/ нет карт.", parent=self)
                return
            total_created = total_updated = total_existed = total_skipped = total_empty = 0
            for map_name, data in list(self.maps_data.items()):
                if not data.get("hof_path"):
                    continue
                self.log(f"\n═══ Генерация: {map_name} ═══")
                c, u, e, s, em = generate_routes(
                    data["dir"], data["trips"], self.naming_style.get(), self.overwrite_mine.get(), self.log
                )
                total_created += len(c)
                total_updated += len(u)
                total_existed += len(e)
                total_skipped += len(s)
                total_empty += len(em)
            self.log(
                f"\n═══ ВСЕГО: создано {total_created}, обновлено {total_updated}, "
                f"уже было {total_existed}, чужих {total_skipped}, пустых {total_empty} ═══"
            )
            self.refresh_maps(parse_again=False)
        except Exception as exc:
            self._show_exception("Ошибка генерации всех карт", exc)

    def show_all_stops(self) -> None:
        try:
            map_name = self._selected_map()
            if not map_name or map_name not in self.maps_data:
                messagebox.showinfo("Информация", "Выберите карту.", parent=self)
                return
            stops = self.maps_data[map_name].get("all_stops", [])
            if not stops:
                messagebox.showinfo(
                    "Остановки не найдены",
                    f"В выбранных HOF карты «{map_name}» нет распознанного списка остановок.\n\n"
                    "Если в логе указаны только конечные пункты [addterminus], эти данные не содержат последовательностей остановок. "
                    "Нужен HOF с [addbusstop]/[addbusstop_list] или [infosystem_busstop_list].",
                    parent=self,
                )
                return

            window = tk.Toplevel(self)
            window.title(f"Все остановки — {map_name}")
            window.geometry("760x700")
            window.minsize(540, 400)

            header = ttk.Frame(window, padding=6)
            header.pack(fill="x")
            ttk.Label(header, text=f"Карта: {map_name}  |  Уникальных остановок: {len(stops)}",
                      font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(header, text="Одна остановка на строку, без нумерации. Текст можно выделять и копировать.",
                      foreground="#666").pack(anchor="w")

            text = scrolledtext.ScrolledText(window, wrap="none", font=("Consolas", 10), undo=False)
            text.pack(fill="both", expand=True, padx=6, pady=6)
            text.insert("1.0", "\n".join(stops))

            button_row = ttk.Frame(window, padding=6)
            button_row.pack(fill="x")

            def save_stops() -> None:
                destination = filedialog.asksaveasfilename(
                    parent=window,
                    title="Сохранить список остановок",
                    defaultextension=".txt",
                    initialfile=f"{sanitize_folder_name(map_name)}_stops.txt",
                    filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")],
                )
                if not destination:
                    return
                try:
                    # Save the canonical unnumbered list (not any selection or formatting).
                    with open(destination, "w", encoding="utf-8-sig", newline="\r\n") as f:
                        for stop in stops:
                            f.write(stop + "\n")
                    self.log(f"✓ Сохранено {len(stops)} остановок: {destination}")
                    messagebox.showinfo("Сохранено", f"Список сохранён:\n{destination}", parent=window)
                except Exception as exc:
                    messagebox.showerror("Ошибка сохранения", f"{exc}\n\n{traceback.format_exc()}", parent=window)

            def copy_stops() -> None:
                try:
                    self.clipboard_clear()
                    self.clipboard_append("\n".join(stops))
                    self.update()
                    messagebox.showinfo("Готово", "Список остановок скопирован в буфер обмена.", parent=window)
                except Exception as exc:
                    messagebox.showerror("Ошибка копирования", f"{exc}\n\n{traceback.format_exc()}", parent=window)

            ttk.Button(button_row, text="💾 Сохранить...", command=save_stops).pack(side="left", padx=3)
            ttk.Button(button_row, text="📋 Копировать весь список", command=copy_stops).pack(side="left", padx=3)
            ttk.Button(button_row, text="Закрыть", command=window.destroy).pack(side="right", padx=3)
        except Exception as exc:
            self._show_exception("Ошибка списка остановок", exc)


def main() -> None:
    fix_dpi()
    app = StopListMakerApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_text = f"{exc}\n\n{traceback.format_exc()}"
        try:
            messagebox.showerror("Ошибка запуска StopListMaker", error_text)
        except Exception:
            pass
        try:
            sys.stderr.write(error_text + "\n")
        except Exception:
            pass
