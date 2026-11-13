#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime

import csv_shape as cs


HERE = os.path.dirname(os.path.abspath(__file__))
ORDERS = os.path.join(HERE, "examples", "orders.csv")
MESSY = os.path.join(HERE, "examples", "messy.tsv")


class ParseHelpersTest(unittest.TestCase):
    def test_int_and_leading_zeros(self):
        self.assertEqual(cs.parse_int("42"), 42)
        self.assertEqual(cs.parse_int("-3"), -3)
        self.assertEqual(cs.parse_int("0"), 0)
        self.assertIsNone(cs.parse_int("010000"))
        self.assertIsNone(cs.parse_int("01"))
        self.assertIsNone(cs.parse_int("1.5"))
        self.assertIsNone(cs.parse_int(""))

    def test_float(self):
        self.assertEqual(cs.parse_float("8.5"), 8.5)
        self.assertEqual(cs.parse_float("3.20"), 3.2)
        self.assertEqual(cs.parse_float("1e-3"), 0.001)
        self.assertEqual(cs.parse_float("42"), 42.0)
        self.assertIsNone(cs.parse_float("not-yet"))

    def test_bool_and_empty(self):
        self.assertTrue(cs.parse_bool("yes"))
        self.assertFalse(cs.parse_bool("NO"))
        self.assertTrue(cs.is_empty("N/A"))
        self.assertTrue(cs.is_empty("  null "))
        self.assertFalse(cs.is_empty("gift"))

    def test_dates(self):
        kind, val = cs.parse_date_or_dt("2026-01-03")
        self.assertEqual(kind, "date")
        self.assertEqual(val, date(2026, 1, 3))
        kind, val = cs.parse_date_or_dt("2026-01-03T10:15:00")
        self.assertEqual(kind, "datetime")
        self.assertEqual(val, datetime(2026, 1, 3, 10, 15, 0))
        kind, val = cs.parse_date_or_dt("2026-01-03T10:15:00Z")
        self.assertEqual(kind, "datetime")
        kind, val = cs.parse_date_or_dt("2024/11/20")
        self.assertEqual(kind, "date")
        self.assertIsNone(cs.parse_date_or_dt("yesterday"))


class EncodingNewlineTest(unittest.TestCase):
    def test_utf8_bom(self):
        enc, bom = cs.detect_encoding("\ufeffa,b\n1,2\n".encode("utf-8-sig"))
        self.assertEqual(enc, "utf-8-sig")
        self.assertEqual(bom, "UTF-8 BOM")

    def test_gb18030(self):
        raw = "品名,数量\n茶叶,2\n".encode("gb18030")
        enc, bom = cs.detect_encoding(raw)
        self.assertEqual(enc, "gb18030")
        self.assertIsNone(bom)

    def test_newlines(self):
        self.assertEqual(cs.detect_newlines("a,b\n1,2\n"), "LF")
        self.assertEqual(cs.detect_newlines("a,b\r\n1,2\r\n"), "CRLF")
        self.assertTrue(cs.detect_newlines("a,b\n1,2\r\n").startswith("mixed"))


class SniffAndProfileTest(unittest.TestCase):
    def test_sniff_tab_and_comma(self):
        d, how = cs.sniff_delimiter("a\tb\n1\t2\n")
        self.assertEqual(d, "\t")
        d, how = cs.sniff_delimiter("a,b;c\n1,2;3\n")
        # comma appears once? actually 'a,b;c' has both. first line 1 comma 1 semicolon.
        # This is ambiguous; just check forced override works.
        d, how = cs.sniff_delimiter("a;b;c\n1;2;3\n")
        self.assertEqual(d, ";")
        d, how = cs.sniff_delimiter("anything", forced="|")
        self.assertEqual((d, how), ("|", "set"))

    def test_sep_hint(self):
        text, hint = cs.skip_sep_hint("sep=;\na;b\n1;2\n")
        self.assertEqual(hint, ";")
        self.assertTrue(text.startswith("a;b"))

    def test_excel_sep_hint_in_bom_encoded_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "excel.csv")
            with open(path, "wb") as source:
                source.write("sep=;\r\nid;name\r\n1;alpha\r\n".encode("utf-8-sig"))

            out = io.StringIO()
            self.assertEqual(cs.run(path, None, None, True, None, True, out, io.StringIO()), 0)

        data = json.loads(out.getvalue())
        self.assertEqual(data["delimiter"], ";")
        self.assertEqual(data["delimiter_source"], "sep= hint")
        self.assertEqual(data["shape"], {"rows": 1, "columns": 2})
        self.assertEqual([column["name"] for column in data["columns"]], ["id", "name"])

    def test_orders_shape(self):
        buf = io.StringIO()
        code = cs.run(
            ORDERS, None, None, True, None, False, buf, io.StringIO()
        )
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("12 rows × 7 columns", out)
        self.assertIn("order_id", out)
        self.assertIn("int", out)
        self.assertIn("sku", out)
        self.assertIn("Issues:          none", out)

    def test_orders_json_types(self):
        buf = io.StringIO()
        cs.run(ORDERS, None, None, True, None, True, buf, io.StringIO())
        data = json.loads(buf.getvalue())
        by_name = {c["name"]: c for c in data["columns"]}
        self.assertEqual(data["shape"], {"rows": 12, "columns": 7})
        self.assertEqual(by_name["order_id"]["type"], "int")
        self.assertEqual(by_name["order_id"]["min"], 1001)
        self.assertEqual(by_name["order_id"]["max"], 1012)
        self.assertEqual(by_name["qty"]["type"], "int")
        self.assertEqual(by_name["price"]["type"], "float")
        self.assertEqual(by_name["paid_at"]["type"], "datetime")
        self.assertEqual(by_name["paid_at"]["empty"], 1)
        self.assertEqual(by_name["shipped"]["type"], "bool")
        self.assertEqual(by_name["note"]["type"], "string")
        self.assertGreaterEqual(by_name["note"]["empty"], 1)
        self.assertEqual(data["issues"], [])

    def test_messy_tsv(self):
        buf = io.StringIO()
        cs.run(MESSY, None, None, True, None, True, buf, io.StringIO())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["delimiter"], "\t")
        by_name = {c["name"]: c for c in data["columns"]}
        self.assertEqual(by_name["postal"]["type"], "string")  # leading zeros
        self.assertFalse(by_name["postal"].get("notes"))
        self.assertEqual(by_name["score"]["type"], "string")  # mixed
        notes = " ".join(by_name["score"].get("notes") or [])
        self.assertIn("mixed", notes)
        kinds = {i["kind"] for i in data["issues"]}
        self.assertIn("ragged", kinds)
        self.assertIn("duplicate", kinds)
        self.assertEqual(data["duplicates"], 1)

    def test_no_header_and_limit(self):
        buf = io.StringIO()
        cs.run(ORDERS, ",", None, False, 3, True, buf, io.StringIO())
        data = json.loads(buf.getvalue())
        self.assertEqual(data["header"], False)
        self.assertEqual(data["columns"][0]["name"], "col_1")
        self.assertEqual(data["shape"]["rows"], 3)
        self.assertTrue(data["truncated"])

    def test_stdin_and_cli_main(self):
        text = "id,name\n1,alpha\n2,beta\n"
        old = cs.sys.stdin
        try:
            cs.sys.stdin = io.StringIO(text)
            # run() reads sys.stdin.buffer — StringIO has no buffer.
            # Use a wrapper.
        finally:
            cs.sys.stdin = old

        class _Stdin:
            def __init__(self, raw):
                self.buffer = io.BytesIO(raw)

        old_stdin, old_stdout = cs.sys.stdin, cs.sys.stdout
        stdout = io.StringIO()
        try:
            cs.sys.stdin = _Stdin(text.encode("utf-8"))
            cs.sys.stdout = stdout
            code = cs.main(["--json", "-"])
        finally:
            cs.sys.stdin, cs.sys.stdout = old_stdin, old_stdout
        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["shape"], {"rows": 2, "columns": 2})
        self.assertEqual(data["file"], "stdin")

    def test_missing_file(self):
        err = io.StringIO()
        old_err = cs.sys.stderr
        try:
            cs.sys.stderr = err
            code = cs.main(["/tmp/csv-shape-does-not-exist-xyz.csv"])
        finally:
            cs.sys.stderr = old_err
        self.assertEqual(code, 2)
        self.assertIn("not found", err.getvalue())

    def test_binary_rejected(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(path, "wb") as f:
                f.write(b"a,b\n1\x00,2\n")
            err = io.StringIO()
            old = cs.sys.stderr
            try:
                cs.sys.stderr = err
                code = cs.main([path])
            finally:
                cs.sys.stderr = old
            self.assertEqual(code, 2)
            self.assertIn("binary", err.getvalue())
        finally:
            os.remove(path)

    def test_duplicate_headers(self):
        raw = "a,a,b\n1,2,3\n"
        # use temp file
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(raw)
            buf = io.StringIO()
            cs.run(path, ",", None, True, None, True, buf, io.StringIO())
            data = json.loads(buf.getvalue())
            names = [c["name"] for c in data["columns"]]
            self.assertEqual(names[0], "a")
            self.assertTrue(names[1].startswith("a__"))
            self.assertTrue(any(i["kind"] == "duplicate_header" for i in data["issues"]))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
