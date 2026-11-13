#!/usr/bin/env python3
"""Profile a CSV/TSV: shape, types, missing values, and row issues. Stdlib only."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Sequence, Tuple

__version__ = "1.0.0"

EMPTY_VALUES = {
    "",
    "na",
    "n/a",
    "null",
    "none",
    "nan",
    "#n/a",
    "#na",
    "-",
    ".",
}

BOOL_VALUES = {
    "true": True,
    "false": False,
    "yes": True,
    "no": False,
    "y": True,
    "n": False,
}

DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
)

DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
)

INT_RE = re.compile(r"[+-]?(0|[1-9]\d*)$")
FLOAT_RE = re.compile(
    r"[+-]?(?:"
    r"(?:0|[1-9]\d*)(?:\.\d+)?"
    r"|\.\d+"
    r")(?:[eE][+-]?\d+)?$"
)

UNIQUE_CAP = 50000
TOP_N = 3
SAMPLE_N = 3
DEFAULT_ROW_CAP = 200000


class UsageError(Exception):
    pass


def is_empty(value: str) -> bool:
    return value.strip().lower() in EMPTY_VALUES


def looks_like_leading_zero_code(value: str) -> bool:
    s = value.strip()
    if s.startswith(("+", "-")):
        s = s[1:]
    return len(s) > 1 and s.startswith("0") and s.isdigit()


def parse_int(value: str) -> Optional[int]:
    s = value.strip()
    if looks_like_leading_zero_code(s):
        return None
    if not INT_RE.fullmatch(s):
        return None
    try:
        return int(s, 10)
    except ValueError:
        return None


def parse_float(value: str) -> Optional[float]:
    s = value.strip()
    if looks_like_leading_zero_code(s.split(".", 1)[0] if "." in s and "e" not in s.lower() else s):
        # 01.5 is unusual; treat as float if the whole string is a plain decimal
        if not re.fullmatch(r"[+-]?\d+\.\d+", s):
            return None
    if parse_int(s) is not None:
        return float(int(s, 10))
    if not FLOAT_RE.fullmatch(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_offset_dt(value: str) -> Optional[datetime]:
    s = value.strip()
    if s.endswith("Z") or s.endswith("z"):
        core = s[:-1]
        for fmt in DATETIME_FORMATS:
            try:
                return datetime.strptime(core, fmt)
            except ValueError:
                continue
        return None
    m = re.fullmatch(
        r"(.*?)([+-])(\d{2}):?(\d{2})$",
        s,
    )
    if not m:
        return None
    core, sign, hh, mm = m.group(1), m.group(2), m.group(3), m.group(4)
    for fmt in DATETIME_FORMATS:
        try:
            dt = datetime.strptime(core, fmt)
        except ValueError:
            continue
        # keep as naive but accept the string as datetime; offset is not applied
        return dt
    return None


def parse_date_or_dt(value: str) -> Optional[Tuple[str, Any]]:
    s = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return "date", datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    for fmt in DATETIME_FORMATS:
        try:
            return "datetime", datetime.strptime(s, fmt)
        except ValueError:
            continue
    dt = _parse_offset_dt(s)
    if dt is not None:
        return "datetime", dt
    return None


def parse_bool(value: str) -> Optional[bool]:
    key = value.strip().lower()
    if key in BOOL_VALUES:
        return BOOL_VALUES[key]
    return None


def detect_encoding(data: bytes) -> Tuple[str, Optional[str]]:
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", "UTF-8 BOM"
    if data.startswith(b"\xff\xfe"):
        return "utf-16-le", "UTF-16 LE BOM"
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be", "UTF-16 BE BOM"
    try:
        data.decode("utf-8")
        return "utf-8", None
    except UnicodeDecodeError:
        pass
    try:
        data.decode("gb18030")
        return "gb18030", None
    except UnicodeDecodeError:
        pass
    return "latin-1", None


def detect_newlines(text: str) -> str:
    crlf = text.count("\r\n")
    # count leftover CR / LF after removing CRLF pairs
    stripped = text.replace("\r\n", "")
    cr = stripped.count("\r")
    lf = stripped.count("\n")
    kinds = []
    if crlf:
        kinds.append("CRLF")
    if lf:
        kinds.append("LF")
    if cr:
        kinds.append("CR")
    if not kinds:
        return "none"
    if len(kinds) == 1:
        return kinds[0]
    return "mixed (" + ", ".join(kinds) + ")"


def _delimiter_scores(sample: str) -> List[Tuple[int, str]]:
    candidates = [",", "\t", ";", "|"]
    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    if not lines:
        return [(0, ",")]
    scored = []
    for d in candidates:
        counts = [ln.count(d) for ln in lines]
        if max(counts) == 0:
            scored.append((0, d))
            continue
        # prefer a delimiter that appears the same number of times on most lines
        consistent = max(Counter(c for c in counts if c > 0).values()) if any(counts) else 0
        total = sum(counts)
        scored.append((consistent * 1000 + total, d))
    scored.sort(reverse=True)
    return scored


def sniff_delimiter(sample: str, forced: Optional[str] = None) -> Tuple[str, str]:
    if forced is not None:
        return forced, "set"
    # Excel "sep=" hint on the first line
    first = sample.splitlines()[0] if sample.splitlines() else ""
    m = re.match(r"sep=(.)\s*$", first, re.IGNORECASE)
    if m:
        return m.group(1), "sep= hint"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        return dialect.delimiter, "sniffed"
    except (csv.Error, TypeError):
        pass
    scores = _delimiter_scores(sample)
    delim = scores[0][1] if scores and scores[0][0] > 0 else ","
    how = "guessed" if scores and scores[0][0] > 0 else "default"
    return delim, how


def read_input(path: Optional[str], encoding_flag: Optional[str]) -> Tuple[str, str, Optional[str], int]:
    if path is None or path == "-":
        raw = sys.stdin.buffer.read()
        source = "stdin"
    else:
        if not os.path.exists(path):
            raise UsageError("file not found: %s" % path)
        if os.path.isdir(path):
            raise UsageError("not a file: %s" % path)
        with open(path, "rb") as f:
            raw = f.read()
        source = path
    if b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UsageError("file looks binary (found a NUL byte); csv-shape only reads text")
    if encoding_flag:
        enc, bom = encoding_flag, None
        if encoding_flag.lower() in {"utf-8-sig", "utf8-sig"}:
            enc, bom = "utf-8-sig", "UTF-8 BOM" if raw.startswith(b"\xef\xbb\xbf") else None
        try:
            text = raw.decode(enc)
        except LookupError:
            raise UsageError("unknown encoding: %s" % encoding_flag)
        except UnicodeDecodeError as exc:
            raise UsageError("cannot decode as %s: %s" % (encoding_flag, exc))
    else:
        enc, bom = detect_encoding(raw)
        text = raw.decode(enc)
    return text, enc, bom, len(raw)


def skip_sep_hint(text: str) -> Tuple[str, Optional[str]]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text, None
    first = lines[0].strip()
    m = re.match(r"sep=(.)\s*$", first, re.IGNORECASE)
    if m:
        return "".join(lines[1:]), m.group(1)
    return text, None


def parse_rows(text: str, delimiter: str) -> List[List[str]]:
    # keepends already stripped by csv
    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream, delimiter=delimiter)
    try:
        return [list(row) for row in reader]
    except csv.Error as exc:
        raise UsageError("CSV parse error: %s" % exc)


def infer_column(values: Sequence[str]) -> Dict[str, Any]:
    nonempty = [(i, v) for i, v in enumerate(values) if not is_empty(v)]
    empty_count = len(values) - len(nonempty)
    unique_vals = []
    unique_set = set()
    unique_capped = False
    for _, v in nonempty:
        if v not in unique_set:
            if len(unique_set) >= UNIQUE_CAP:
                unique_capped = True
                break
            unique_set.add(v)
            unique_vals.append(v)

    type_name = "empty"
    parsed: List[Any] = []
    mixed_notes: List[str] = []

    if nonempty:
        kinds = []
        parsed_map: Dict[str, List[Any]] = {
            "int": [],
            "float": [],
            "bool": [],
            "date": [],
            "datetime": [],
            "string": [],
        }
        # If any value keeps leading zeros (010000), treat digit-only
        # siblings as codes too so a postal/ID column stays string.
        force_codes = any(looks_like_leading_zero_code(raw) for _, raw in nonempty)
        for _, raw in nonempty:
            if force_codes and (
                parse_int(raw) is not None or looks_like_leading_zero_code(raw)
            ):
                parsed_map["string"].append(raw)
                kinds.append("string")
                continue
            iv = parse_int(raw)
            if iv is not None:
                parsed_map["int"].append(iv)
                parsed_map["float"].append(float(iv))
                kinds.append("int")
                continue
            fv = parse_float(raw)
            if fv is not None:
                parsed_map["float"].append(fv)
                kinds.append("float")
                continue
            dv = parse_date_or_dt(raw)
            if dv is not None:
                kind, val = dv
                parsed_map[kind].append(val)
                kinds.append(kind)
                continue
            bv = parse_bool(raw)
            if bv is not None:
                parsed_map["bool"].append(bv)
                kinds.append("bool")
                continue
            parsed_map["string"].append(raw)
            kinds.append("string")

        kind_set = set(kinds)
        if kind_set == {"int"}:
            type_name = "int"
            parsed = parsed_map["int"]
        elif kind_set <= {"int", "float"}:
            type_name = "float"
            parsed = parsed_map["float"]
        elif kind_set == {"bool"}:
            type_name = "bool"
            parsed = parsed_map["bool"]
        elif kind_set == {"date"}:
            type_name = "date"
            parsed = parsed_map["date"]
        elif kind_set <= {"date", "datetime"} and "datetime" in kind_set:
            # promote dates to datetime at midnight for min/max
            promoted = []
            for k, raw in zip(kinds, (v for _, v in nonempty)):
                got = parse_date_or_dt(raw)
                if got and got[0] == "date":
                    promoted.append(datetime.combine(got[1], datetime.min.time()))
                elif got:
                    promoted.append(got[1])
            type_name = "datetime"
            parsed = promoted
        elif kind_set == {"datetime"}:
            type_name = "datetime"
            parsed = parsed_map["datetime"]
        else:
            type_name = "string"
            parsed = [raw for _, raw in nonempty]
            interesting = kind_set - {"string"}
            if interesting:
                mixed_notes.append(
                    "mixed types: " + ", ".join(sorted(kind_set))
                )

    result: Dict[str, Any] = {
        "type": type_name,
        "count": len(values),
        "empty": empty_count,
        "unique": len(unique_set) if not unique_capped else UNIQUE_CAP,
        "unique_capped": unique_capped,
        "samples": unique_vals[:SAMPLE_N],
        "notes": mixed_notes,
    }

    if nonempty:
        freq = Counter(v for _, v in nonempty)
        result["top"] = [
            {"value": val, "count": n} for val, n in freq.most_common(TOP_N)
        ]
    else:
        result["top"] = []

    if type_name in {"int", "float", "date", "datetime"} and parsed:
        result["min"] = _jsonable(min(parsed))
        result["max"] = _jsonable(max(parsed))
    if type_name == "bool" and parsed:
        result["true"] = sum(1 for v in parsed if v)
        result["false"] = sum(1 for v in parsed if not v)
    return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, str)):
        return value
    return str(value)


def row_hash(row: Sequence[str]) -> str:
    blob = "\x1f".join(row).encode("utf-8", "surrogatepass")
    return hashlib.sha256(blob).hexdigest()


def profile_rows(
    rows: List[List[str]],
    has_header: bool,
    limit: Optional[int],
) -> Dict[str, Any]:
    issues: List[Dict[str, Any]] = []
    if not rows or (len(rows) == 1 and not any(cell.strip() for cell in rows[0])):
        return {
            "rows": 0,
            "columns": 0,
            "header": [],
            "truncated": False,
            "columns_profile": [],
            "issues": [{"kind": "empty", "message": "file has no data rows"}],
            "duplicates": 0,
        }

    header_row: List[str]
    data: List[List[str]]
    if has_header:
        header_row = rows[0]
        data = rows[1:]
    else:
        width = max((len(r) for r in rows), default=0)
        header_row = ["col_%d" % (i + 1) for i in range(width)]
        data = rows

    truncated = False
    if limit is not None and limit >= 0 and len(data) > limit:
        data = data[:limit]
        truncated = True

    expected = len(header_row)
    # pad/clip header names, ensure unique display names
    names = []
    seen = Counter()
    for i, raw in enumerate(header_row):
        name = raw.strip() or ("col_%d" % (i + 1))
        seen[name] += 1
        if seen[name] > 1:
            name = "%s__%d" % (name, seen[name])
        names.append(name)
    if len(header_row) != len(set(n.strip() or "#" for n in header_row)):
        issues.append(
            {
                "kind": "duplicate_header",
                "message": "header has duplicate column names",
            }
        )

    ragged = 0
    ragged_examples = []
    columns: List[List[str]] = [[] for _ in names]
    hashes: Dict[str, List[int]] = {}
    # row numbers are 1-based in the file: header is line 1 when present
    row_no_offset = 2 if has_header else 1

    for idx, row in enumerate(data):
        row_no = idx + row_no_offset
        if len(row) != expected:
            ragged += 1
            if len(ragged_examples) < 5:
                ragged_examples.append(
                    {"row": row_no, "expected": expected, "got": len(row)}
                )
        # normalize width
        if len(row) < expected:
            row = row + [""] * (expected - len(row))
        elif len(row) > expected:
            # extra fields go unrecorded in columns; already flagged
            row = row[:expected]
        for c, cell in enumerate(row):
            columns[c].append(cell)
        h = row_hash(row)
        hashes.setdefault(h, []).append(row_no)

    if ragged:
        preview = "; ".join(
            "row %d: expected %d fields, got %d" % (e["row"], e["expected"], e["got"])
            for e in ragged_examples
        )
        extra = "" if ragged <= len(ragged_examples) else " (%d more)" % (ragged - len(ragged_examples))
        issues.append(
            {
                "kind": "ragged",
                "count": ragged,
                "examples": ragged_examples,
                "message": "ragged rows: %s%s" % (preview, extra),
            }
        )

    duplicate_groups = {h: nos for h, nos in hashes.items() if len(nos) > 1}
    duplicate_rows = sum(len(nos) - 1 for nos in duplicate_groups.values())
    if duplicate_rows:
        examples = []
        for nos in list(duplicate_groups.values())[:3]:
            examples.append("rows " + ", ".join(str(n) for n in nos))
        issues.append(
            {
                "kind": "duplicate",
                "count": duplicate_rows,
                "message": "%d duplicate row%s (%s)"
                % (
                    duplicate_rows,
                    "" if duplicate_rows == 1 else "s",
                    "; ".join(examples),
                ),
            }
        )

    col_profiles = []
    for i, name in enumerate(names):
        info = infer_column(columns[i])
        info["index"] = i + 1
        info["name"] = name
        col_profiles.append(info)

    return {
        "rows": len(data),
        "columns": len(names),
        "header": names,
        "truncated": truncated,
        "columns_profile": col_profiles,
        "issues": issues,
        "duplicates": duplicate_rows,
    }


def format_text(
    meta: Dict[str, Any],
    profile: Dict[str, Any],
) -> str:
    lines = []
    lines.append("File:            %s" % meta["source"])
    size = meta["bytes"]
    if size < 1024:
        size_s = "%d B" % size
    elif size < 1024 * 1024:
        size_s = "%.1f KB" % (size / 1024.0)
    else:
        size_s = "%.1f MB" % (size / (1024.0 * 1024.0))
    lines.append("Size:            %s" % size_s)
    bom = " (%s)" % meta["bom"] if meta["bom"] else ""
    lines.append("Encoding:        %s%s" % (meta["encoding"], bom))
    lines.append("Line endings:    %s" % meta["newlines"])
    lines.append(
        "Delimiter:       %s (%s)"
        % (_visible_delim(meta["delimiter"]), meta["delimiter_source"])
    )
    lines.append("Header:          %s" % ("yes" if meta["has_header"] else "no"))
    trunc = ", truncated at --limit" if profile["truncated"] else ""
    lines.append(
        "Shape:           %d rows × %d columns%s"
        % (profile["rows"], profile["columns"], trunc)
    )
    lines.append("")
    if profile["columns_profile"]:
        # column table
        name_w = max(4, max(len(c["name"]) for c in profile["columns_profile"]))
        name_w = min(name_w, 28)
        lines.append(
            "  %s  %s  %s  %s  %s  %s"
            % (
                "#".rjust(3),
                "Name".ljust(name_w),
                "Type".ljust(8),
                "Empty".rjust(5),
                "Unique".rjust(6),
                "Range / samples",
            )
        )
        for c in profile["columns_profile"]:
            extra = _range_or_samples(c)
            lines.append(
                "  %s  %s  %s  %s  %s  %s"
                % (
                    str(c["index"]).rjust(3),
                    _clip(c["name"], name_w).ljust(name_w),
                    c["type"].ljust(8),
                    str(c["empty"]).rjust(5),
                    ("≥%d" % c["unique"] if c["unique_capped"] else str(c["unique"])).rjust(6),
                    extra,
                )
            )
            for note in c.get("notes") or []:
                lines.append("       note: %s" % note)
        lines.append("")
    if profile["issues"]:
        lines.append("Issues:")
        for issue in profile["issues"]:
            lines.append("  - %s" % issue["message"])
    else:
        lines.append("Issues:          none")
    return "\n".join(lines) + "\n"


def _visible_delim(d: str) -> str:
    mapping = {",": "comma (,)", "\t": "tab (\\t)", ";": "semicolon (;)", "|": "pipe (|)"}
    return mapping.get(d, repr(d))


def _clip(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _range_or_samples(c: Dict[str, Any]) -> str:
    if c["type"] in {"int", "float", "date", "datetime"} and "min" in c:
        return "%s .. %s" % (c["min"], c["max"])
    if c["type"] == "bool":
        return "true=%d, false=%d" % (c.get("true", 0), c.get("false", 0))
    if c["type"] == "empty":
        return "(all empty)"
    samples = c.get("samples") or []
    shown = ", ".join(_short_sample(s) for s in samples)
    if c.get("unique_capped") or (c.get("unique", 0) > len(samples)):
        if shown:
            shown += ", …"
    return shown or "(none)"


def _short_sample(value: str) -> str:
    text = value.replace("\n", "\\n")
    if len(text) > 24:
        text = text[:23] + "…"
    return json.dumps(text, ensure_ascii=False)


def build_json(meta: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": meta["source"],
        "bytes": meta["bytes"],
        "encoding": meta["encoding"],
        "bom": meta["bom"],
        "newlines": meta["newlines"],
        "delimiter": meta["delimiter"],
        "delimiter_source": meta["delimiter_source"],
        "header": meta["has_header"],
        "shape": {"rows": profile["rows"], "columns": profile["columns"]},
        "truncated": profile["truncated"],
        "columns": profile["columns_profile"],
        "issues": profile["issues"],
        "duplicates": profile["duplicates"],
        "version": __version__,
    }


def run(
    path: Optional[str],
    delimiter: Optional[str],
    encoding: Optional[str],
    has_header: bool,
    limit: Optional[int],
    as_json: bool,
    out: Any,
    err: Any,
) -> int:
    text, enc, bom, nbytes = read_input(path, encoding)
    text_for_sep, hint = skip_sep_hint(text)
    newlines = detect_newlines(text)
    sample = "\n".join(text_for_sep.splitlines()[:40])
    delim, how = sniff_delimiter(sample if sample else text_for_sep, delimiter)
    if hint and delimiter is None:
        delim, how = hint, "sep= hint"
    rows = parse_rows(text_for_sep, delim)
    if not rows:
        profile = {
            "rows": 0,
            "columns": 0,
            "header": [],
            "truncated": False,
            "columns_profile": [],
            "issues": [{"kind": "empty", "message": "file has no data rows"}],
            "duplicates": 0,
        }
    else:
        if limit is None and len(rows) - (1 if has_header else 0) > DEFAULT_ROW_CAP:
            limit = DEFAULT_ROW_CAP
            # will set truncated in profile
        profile = profile_rows(rows, has_header, limit)
        if profile["truncated"] and (len(rows) - (1 if has_header else 0)) > (
            limit or 0
        ):
            profile["issues"].insert(
                0,
                {
                    "kind": "truncated",
                    "message": "only the first %d data rows were profiled (over the %d-row cap; pass --limit to change)"
                    % (profile["rows"], limit or DEFAULT_ROW_CAP),
                },
            )

    source = "stdin" if (path is None or path == "-") else path
    meta = {
        "source": source,
        "bytes": nbytes,
        "encoding": enc,
        "bom": bom,
        "newlines": newlines,
        "delimiter": delim,
        "delimiter_source": how,
        "has_header": has_header,
    }
    if as_json:
        out.write(json.dumps(build_json(meta, profile), ensure_ascii=False, indent=2))
        out.write("\n")
    else:
        out.write(format_text(meta, profile))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="csv-shape",
        description="Profile a CSV/TSV locally: delimiter, types, missing values, ragged and duplicate rows.",
    )
    p.add_argument(
        "file",
        nargs="?",
        help="path to a CSV/TSV file, or - for stdin (stdin is also used when no file is given)",
    )
    p.add_argument(
        "-d",
        "--delimiter",
        help="field delimiter (default: sniff comma / tab / semicolon / pipe). Use \\t for tab.",
    )
    p.add_argument(
        "--encoding",
        help="text encoding (default: detect UTF-8, BOM, GB18030, then Latin-1)",
    )
    p.add_argument(
        "--no-header",
        action="store_true",
        help="treat the first row as data; name columns col_1, col_2, …",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="only profile the first N data rows (default: all, capped at %d)"
        % DEFAULT_ROW_CAP,
    )
    p.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="machine-readable JSON on stdout",
    )
    p.add_argument(
        "--version",
        action="version",
        version="csv-shape %s" % __version__,
    )
    return p


def parse_delimiter_flag(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if value in {r"\t", "tab", "TAB"}:
        return "\t"
    if value == r"\\t":
        return "\t"
    if len(value) != 1:
        raise UsageError("delimiter must be a single character (or \\t for tab)")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    path = args.file
    if path is None:
        if sys.stdin.isatty() and not args.file:
            parser.print_help(sys.stderr)
            sys.stderr.write("\nerror: provide a file, or pipe data on stdin\n")
            return 2
        path = "-"
    try:
        if args.limit is not None and args.limit < 0:
            raise UsageError("--limit must be >= 0")
        delim = parse_delimiter_flag(args.delimiter)
        return run(
            path=path,
            delimiter=delim,
            encoding=args.encoding,
            has_header=not args.no_header,
            limit=args.limit,
            as_json=args.as_json,
            out=sys.stdout,
            err=sys.stderr,
        )
    except UsageError as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 2
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
