"""Tests for parquet_viewer: python tests/test_parquet_viewer.py  (or pytest)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
from parquet_viewer import server as pv  # noqa: E402


def make_data(root):
    c = duckdb.connect()
    os.makedirs(os.path.join(root, "universe"))
    os.makedirs(os.path.join(root, "bars", "year=2024"))
    os.makedirs(os.path.join(root, "bars", "year=2025"))
    os.makedirs(os.path.join(root, "misc"))
    c.execute(f"""COPY (
        SELECT DATE '2024-01-02' + (i // 100)::INT AS date, 10000 + i % 100 AS permno,
               'T' || (i % 100) AS ticker, (i % 100) < 10 AS in_sp500,
               CASE WHEN i % 10 = 0 THEN NULL ELSE i * 1.5 END AS prc,
               CASE WHEN i % 25 = 0 THEN 'NaN'::DOUBLE ELSE (i % 7) - 3.0 END AS ret,
               (i * 100)::DECIMAL(18, 2) AS mktcap,
               ['Energy', 'IT', 'Financials'][1 + i % 3] AS sector,
               9007199254740993 + i AS bigid
        FROM range(1000) t(i)) TO '{root}/universe/u_2024.parquet'""")
    for y in (2024, 2025):
        c.execute(f"""COPY (SELECT TIMESTAMP '{y}-01-02 09:30:00' + to_minutes(m) AS ts, 'S' || s AS sym, m * 1.0 AS px
                      FROM range(10) a(m), range(3) b(s)) TO '{root}/bars/year={y}/{y}0102.parquet'""")
    c.execute(f"""COPY (SELECT i AS id, [i, i + 1] AS lst, {{'a': i}} AS st, 'abc'::BLOB AS bl
                  FROM range(5) t(i)) TO '{root}/misc/nested.parquet'""")
    with open(os.path.join(root, "misc", "broken.parquet"), "w") as fh:
        fh.write("not parquet")
    with open(os.path.join(root, "misc", "notes.txt"), "w") as fh:
        fh.write("x")
    outside = os.path.join(os.path.dirname(root), "outside.parquet")
    c.execute(f"COPY (SELECT 1 AS secret) TO '{outside}'")


class ViewerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp()
        cls.root = os.path.join(cls.base, "data")
        os.makedirs(cls.root)
        make_data(cls.root)
        cls.httpd, cls.viewer = pv.make_server(cls.root, port=0, quiet=True)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.base, ignore_errors=True)

    # -- helpers
    def call(self, name, params=None, body=None, header=True, host=None):
        url = f"http://127.0.0.1:{self.port}/api/{name}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if header:
            req.add_header("X-PQV", "1")
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"error": raw.decode()}

    def q(self, **kw):
        body = {"path": "universe/u_2024.parquet", "dataset": False, "limit": 100}
        body.update(kw)
        return self.call("query", body=body)

    # -- tests
    def test_static_and_config(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/") as r:
            self.assertIn(b"Parquet Viewer", r.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/app.js") as r:
            self.assertEqual(r.status, 200)
        st, cfg = self.call("config")
        self.assertEqual(st, 200)
        self.assertEqual(cfg["root"], self.root)
        self.assertTrue(cfg["sandboxed"])

    def test_security(self):
        st, _ = self.call("config", header=False)
        self.assertEqual(st, 403)
        st, _ = self.call("config", host="evil.example.com")
        self.assertEqual(st, 403)
        st, r = self.call("list", {"path": "../"})
        self.assertEqual(st, 400)
        st, r = self.call("info", {"path": "../outside.parquet"})
        self.assertEqual(st, 400)
        # SQL condition cannot read files outside the root or run a second statement
        st, r = self.q(where=f"permno IN (SELECT secret FROM read_parquet('{self.base}/outside.parquet'))")
        self.assertEqual(st, 400, r)
        st, r = self.q(where="1=1; COPY (SELECT 1) TO 'x.csv'")
        self.assertEqual(st, 400, r)
        st, r = self.q(where="1=1) TO 'x.csv' --")
        self.assertEqual(st, 400, r)
        self.assertFalse(os.path.exists(os.path.join(self.root, "x.csv")))

    def test_list(self):
        st, r = self.call("list", {"path": ""})
        self.assertEqual(st, 200)
        names = [e["name"] for e in r["entries"]]
        self.assertEqual(names, ["bars", "misc", "universe"])
        st, r = self.call("list", {"path": "bars"})
        self.assertTrue(r["can_open_as_table"])
        st, r = self.call("list", {"path": "misc"})
        self.assertEqual([e["name"] for e in r["entries"]], ["broken.parquet", "nested.parquet"])
        self.assertEqual(r["other_files"], 1)

    def test_info(self):
        st, r = self.call("info", {"path": "universe/u_2024.parquet"})
        self.assertEqual(st, 200)
        self.assertEqual(r["rows"], 1000)
        self.assertEqual(len(r["columns"]), 9)
        self.assertEqual(r["column_meta"][0]["path"], "date")
        st, r = self.call("info", {"path": "bars", "dataset": "1"})
        self.assertEqual(r["rows"], 60)
        self.assertEqual(r["n_files"], 2)
        self.assertEqual(r["partitions"], ["year"])
        self.assertIn("_file", [c["name"] for c in r["columns"]])
        st, r = self.call("info", {"path": "misc/broken.parquet"})
        self.assertEqual(st, 400)

    def test_query_basic(self):
        st, r = self.q()
        self.assertEqual(st, 200, r)
        self.assertEqual(r["total"], 1000)
        self.assertEqual(len(r["rows"]), 100)
        row = dict(zip([c["name"] for c in r["columns"]], r["rows"][0]))
        self.assertEqual(row["date"], "2024-01-02")
        self.assertIsNone(row["prc"])
        self.assertEqual(row["ret"], "NaN")
        self.assertEqual(row["bigid"], "9007199254740993")   # beyond JS safe range -> string
        st, r = self.q(offset=990, limit=50)
        self.assertEqual(len(r["rows"]), 10)

    def test_filters(self):
        st, r = self.q(filters=[{"col": "sector", "op": "=", "value": "IT"}])
        self.assertEqual(r["total"], 333)
        self.assertEqual(r["unfiltered"], 1000)
        st, r = self.q(filters=[{"col": "permno", "op": "in", "value": "10001, 10002"}])
        self.assertEqual(r["total"], 20)
        st, r = self.q(filters=[{"col": "date", "op": "between", "value": "2024-01-02", "value2": "2024-01-03"}])
        self.assertEqual(r["total"], 200)
        st, r = self.q(filters=[{"col": "prc", "op": "null"}])
        self.assertEqual(r["total"], 100)
        st, r = self.q(filters=[{"col": "ret", "op": "nan"}])
        self.assertEqual(r["total"], 40)
        st, r = self.q(filters=[{"col": "ticker", "op": "contains", "value": "t9"}])
        self.assertEqual(r["total"], 110)   # T9, T90..T99
        st, r = self.q(filters=[{"col": "in_sp500", "op": "=", "value": "true"}])
        self.assertEqual(r["total"], 100)
        st, r = self.q(filters=[{"col": "mktcap", "op": ">=", "value": "99000"}])
        self.assertEqual(r["total"], 10)
        st, r = self.q(filters=[{"col": "sector", "op": "=", "value": "IT", "enabled": False}])
        self.assertEqual(r["total"], 1000)
        st, r = self.q(filters=[{"col": "permno", "op": "=", "value": "abc"}])
        self.assertEqual(st, 400)
        st, r = self.q(filters=[{"col": "nope", "op": "=", "value": "1"}])
        self.assertEqual(st, 400)
        st, r = self.q(filters=[{"col": "ticker", "op": "=", "value": "it's"}])
        self.assertEqual((st, r["total"]), (200, 0))

    def test_where_search_sort_columns(self):
        st, r = self.q(where="permno = 10005 AND prc > 100")
        self.assertEqual(st, 200, r)
        self.assertEqual(r["total"], 9)
        st, r = self.q(where="no_such_col > 1")
        self.assertEqual(st, 400)
        st, r = self.q(search="T42")
        self.assertEqual(r["total"], 10)
        st, r = self.q(search="10042")
        self.assertEqual(r["total"], 10)
        st, r = self.q(search="2024-01-05")
        self.assertEqual(r["total"], 100)
        st, r = self.q(sort=[{"col": "prc", "desc": True}], columns=["permno", "prc"], limit=3)
        self.assertEqual([c["name"] for c in r["columns"]], ["permno", "prc"])
        self.assertEqual(r["rows"][0][1], 999 * 1.5)
        st, r = self.q(columns=["nope"])
        self.assertEqual(st, 400)

    def test_dataset_query(self):
        st, r = self.call("query", body={"path": "bars", "dataset": True, "filters": [{"col": "year", "op": "=", "value": "2025"}]})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["total"], 30)
        self.assertTrue(all(row[-1] == "year=2025/20250102.parquet" for row in r["rows"]))

    def test_nested(self):
        st, r = self.call("query", body={"path": "misc/nested.parquet"})
        self.assertEqual(st, 200, r)
        row = r["rows"][1]
        self.assertEqual(row[1], [1, 2])
        self.assertEqual(row[2], {"a": 1})
        self.assertTrue(row[3].startswith("0x"))
        st, r = self.call("stats", body={"path": "misc/nested.parquet", "column": "lst"})
        self.assertEqual(st, 200, r)

    def test_stats(self):
        st, r = self.call("stats", body={"path": "universe/u_2024.parquet", "column": "prc"})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["nulls"], 100)
        self.assertEqual(r["max"], 999 * 1.5)
        self.assertEqual(sum(r["histogram"]["bins"]) + r["histogram"]["below"] + r["histogram"]["above"], 900)
        st, r = self.call("stats", body={"path": "universe/u_2024.parquet", "column": "ret"})
        self.assertEqual(r["nan"], 40)
        self.assertEqual(len(r["top"]), 8)   # 7 values + NaN
        st, r = self.call("stats", body={"path": "universe/u_2024.parquet", "column": "date"})
        self.assertEqual(r["periods"]["unit"], "day")
        self.assertEqual(len(r["periods"]["bins"]), 10)
        st, r = self.call("stats", body={"path": "bars", "dataset": True, "column": "ts"})
        self.assertEqual(r["periods"]["unit"], "month")
        self.assertEqual(len(r["periods"]["bins"]), 13)      # Jan 2024 .. Jan 2025, empty months filled
        self.assertEqual(sum(c for _, c in r["periods"]["bins"]), 60)
        st, r = self.call("stats", body={"path": "universe/u_2024.parquet", "column": "sector",
                                         "filters": [{"col": "sector", "op": "!=", "value": "IT"}]})
        self.assertEqual([t[0] for t in r["top"]], ["Energy", "Financials"])
        st, r = self.call("stats", body={"path": "universe/u_2024.parquet", "column": "permno"})
        self.assertEqual(sum(r["histogram"]["bins"]) + r["histogram"]["below"] + r["histogram"]["above"], 1000)

    def test_summarize_code_export(self):
        st, r = self.call("summarize", body={"path": "universe/u_2024.parquet", "columns": ["permno", "prc"]})
        self.assertEqual(st, 200, r)
        self.assertEqual(len(r["rows"]), 2)
        st, r = self.call("code", body={"path": "universe/u_2024.parquet", "where": "permno = 10001"})
        self.assertIn("permno = 10001", r["sql"])
        self.assertIn("duckdb.sql", r["python"])
        st, r = self.call("export", body={"path": "universe/u_2024.parquet", "columns": ["permno", "ticker"],
                                          "filters": [{"col": "permno", "op": "=", "value": "10003"}]})
        self.assertEqual(r["filename"], "u_2024_filtered.csv")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{r['url']}") as resp:
            text = resp.read().decode()
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "permno,ticker")
        self.assertEqual(len(lines), 11)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/export/bogus")


if __name__ == "__main__":
    unittest.main(verbosity=2)
