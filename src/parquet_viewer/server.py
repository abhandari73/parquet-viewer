#!/usr/bin/env python3
"""Parquet Viewer - browse, filter and inspect parquet files in a web browser.

    pqv ~/data                       # serves http://localhost:8770
    pqv ~/data --port 9000 --no-browser

Everything runs locally: a small HTTP server (Python standard library) answers the
web page, and DuckDB reads the parquet files.  Only files below the ROOT folder can
be opened; DuckDB is locked to that folder as well.

Requires: duckdb >= 1.1 (pip install duckdb).
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import decimal
import io
import json
import math
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import duckdb
except ImportError:  # pragma: no cover
    sys.exit("duckdb is not installed. Run:  pip install duckdb")

from . import __version__

PARQUET_EXT = (".parquet", ".parq", ".pq")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_PAGE = 5000
SAFE_INT = 2 ** 53
API_HEADER = "X-PQV"            # custom header required on API calls (blocks cross-site requests)
CACHE_TTL = 300                 # seconds a cached row count / schema stays valid

INT_TYPES = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
             "UINTEGER", "UBIGINT", "UHUGEINT"}
FLOAT_TYPES = {"FLOAT", "DOUBLE", "REAL"}


class UserError(Exception):
    """An error caused by the request (bad path, bad filter ...) - shown to the user."""


# ----------------------------------------------------------------------------- helpers

def qident(name: str) -> str:
    """Quote a column name for SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def lit(value) -> str:
    """SQL string literal (DuckDB: quotes are doubled, backslash is not an escape)."""
    return "'" + str(value).replace("'", "''") + "'"


def type_kind(t: str) -> str:
    t = t.upper()
    base = t.split("(")[0].strip()
    if t.endswith("]") or base.startswith(("STRUCT", "MAP", "UNION", "LIST")):
        return "nested"
    if base in INT_TYPES:
        return "int"
    if base in FLOAT_TYPES:
        return "float"
    if base.startswith("DECIMAL") or base == "NUMERIC":
        return "decimal"
    if base == "BOOLEAN":
        return "bool"
    if base == "DATE":
        return "date"
    if base.startswith("TIMESTAMP") or base == "DATETIME":
        return "timestamp"
    if base.startswith("TIME"):
        return "time"
    if base in ("VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR") or base.startswith("ENUM"):
        return "string"
    if base in ("BLOB", "BYTEA"):
        return "blob"
    return "other"


def is_numeric(kind: str) -> bool:
    return kind in ("int", "float", "decimal")


def jv(v):
    """Convert a DuckDB result value into something JSON can carry."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v if -SAFE_INT < v < SAFE_INT else str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
        return v
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return f if math.isfinite(f) else str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time, dt.timedelta, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b[:64].hex() + ("…" if len(b) > 64 else "")
    if isinstance(v, (list, tuple)):
        return [jv(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jv(x) for k, x in v.items()}
    return str(v)


def csv_value(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(jv(v))
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex()
    return v


class TTLCache:
    def __init__(self, size=256, ttl=CACHE_TTL):
        self.size, self.ttl = size, ttl
        self.data = collections.OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.data.get(key)
            if item is None or time.time() - item[0] > self.ttl:
                self.data.pop(key, None)
                return None
            self.data.move_to_end(key)
            return item[1]

    def put(self, key, value):
        with self.lock:
            self.data[key] = (time.time(), value)
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


# ----------------------------------------------------------------------------- the viewer

class Source:
    """One table: a single parquet file or every parquet file below a folder."""

    def __init__(self, rel, full, dataset, sql, columns, types):
        self.rel, self.full, self.dataset = rel, full, dataset
        self.sql = sql                  # FROM-clause expression
        self.columns = columns          # ordered column names
        self.types = types              # name -> duckdb type string


class Viewer:
    def __init__(self, root: str, memory_limit: str | None = None, threads: int | None = None,
                 show_hidden: bool = False):
        self.root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(self.root):
            raise SystemExit(f"Root folder not found: {self.root}")
        self.show_hidden = show_hidden
        self.tmp = tempfile.mkdtemp(prefix="parquet_viewer_")
        self.db = duckdb.connect(":memory:")
        self.sandboxed = self._configure(memory_limit, threads)
        self.schema_cache = TTLCache()
        self.count_cache = TTLCache(size=1024)
        self.exports = TTLCache(size=64, ttl=600)
        self.active = {}                 # channel -> running cursor (so a new request cancels the old one)
        self.active_lock = threading.Lock()

    # -- setup
    def _configure(self, memory_limit, threads) -> bool:
        db = self.db
        if memory_limit:
            db.execute(f"SET memory_limit={lit(memory_limit)}")
        if threads:
            db.execute(f"SET threads={int(threads)}")
        db.execute(f"SET temp_directory={lit(self.tmp)}")
        try:
            dirs = [self.root.rstrip(os.sep) + os.sep, self.tmp.rstrip(os.sep) + os.sep]
            db.execute("SET allowed_directories=[" + ", ".join(lit(d) for d in dirs) + "]")
            db.execute("SET enable_external_access=false")
            db.execute("SET lock_configuration=true")
            return True
        except duckdb.Error as exc:  # older DuckDB: no allowed_directories
            print(f"note: DuckDB file sandbox not available ({exc}); relying on path checks only")
            return False

    # -- paths
    def resolve(self, rel: str | None) -> str:
        rel = (rel or "").replace("\\", "/").strip().lstrip("/")
        full = os.path.normpath(os.path.join(self.root, rel))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise UserError("That path is outside the root folder.")
        return full

    def rel(self, full: str) -> str:
        r = os.path.relpath(full, self.root)
        return "" if r == "." else r.replace(os.sep, "/")

    def _visible(self, name: str) -> bool:
        return self.show_hidden or not name.startswith(".")

    # -- folder listing
    def list_dir(self, rel: str) -> dict:
        full = self.resolve(rel)
        if not os.path.isdir(full):
            raise UserError(f"Not a folder: {rel or '/'}")
        entries, other = [], 0
        with os.scandir(full) as it:
            for e in it:
                if not self._visible(e.name):
                    continue
                try:
                    if e.is_dir():
                        n_pq = n_dir = 0
                        try:
                            with os.scandir(e.path) as sub:
                                for i, s in enumerate(sub):
                                    if i > 20000:
                                        break
                                    if not self._visible(s.name):
                                        continue
                                    if s.is_dir():
                                        n_dir += 1
                                    elif s.name.lower().endswith(PARQUET_EXT):
                                        n_pq += 1
                        except OSError:
                            pass
                        entries.append({"name": e.name, "type": "dir", "mtime": e.stat().st_mtime,
                                        "n_parquet": n_pq, "n_dirs": n_dir})
                    elif e.name.lower().endswith(PARQUET_EXT):
                        st = e.stat()
                        entries.append({"name": e.name, "type": "file", "size": st.st_size,
                                        "mtime": st.st_mtime})
                    else:
                        other += 1
                except OSError:
                    continue
        entries.sort(key=lambda x: (x["type"] != "dir", natural_key(x["name"])))
        n_files = sum(1 for x in entries if x["type"] == "file")
        hive = any(x["type"] == "dir" and "=" in x["name"] for x in entries)
        return {
            "path": self.rel(full),
            "name": os.path.basename(full) or os.path.basename(self.root),
            "parent": None if full == self.root else self.rel(os.path.dirname(full)),
            "entries": entries,
            "other_files": other,
            "can_open_as_table": n_files > 1 or hive or (n_files == 1 and hive),
        }

    # -- sources
    def _signature(self, full: str, dataset: bool):
        st = os.stat(full)
        return (full, dataset, st.st_mtime, st.st_size if not dataset else 0)

    def source(self, rel: str, dataset: bool) -> Source:
        full = self.resolve(rel)
        if dataset:
            if not os.path.isdir(full):
                raise UserError("Open-as-table needs a folder.")
        else:
            if not os.path.isfile(full):
                raise UserError(f"File not found: {rel}")
            if not full.lower().endswith(PARQUET_EXT):
                raise UserError("Only parquet files can be opened.")
        key = self._signature(full, dataset)
        cached = self.schema_cache.get(key)
        if cached:
            return cached
        cur = self.db.cursor()
        try:
            if dataset:
                glob = os.path.join(full, "**", "*.parquet")
                strip = len(full.rstrip(os.sep)) + 2
                sql = None
                last_err = None
                for hive in ("true", "false"):
                    cand = (f"(SELECT * EXCLUDE (filename), substr(filename, {strip}) AS _file "
                            f"FROM read_parquet({lit(glob)}, hive_partitioning={hive}, "
                            f"union_by_name=true, filename=true))")
                    try:
                        desc = cur.execute(f"DESCRIBE SELECT * FROM {cand}").fetchall()
                        sql = cand
                        break
                    except duckdb.Error as exc:
                        last_err = exc
                        if "hive" not in str(exc).lower():
                            break
                if sql is None:
                    raise UserError(clean_error(last_err))
            else:
                sql = f"read_parquet({lit(full)})"
                try:
                    desc = cur.execute(f"DESCRIBE SELECT * FROM {sql}").fetchall()
                except duckdb.Error as exc:
                    raise UserError(clean_error(exc))
        finally:
            cur.close()
        columns = [r[0] for r in desc]
        types = {r[0]: r[1] for r in desc}
        src = Source(self.rel(full), full, dataset, sql, columns, types)
        self.schema_cache.put(key, src)
        return src

    # -- running SQL
    def run(self, sql: str, channel: str | None = None, fetch="all"):
        """Run one SELECT. A newer request on the same channel interrupts the older one."""
        try:
            stmts = duckdb.extract_statements(sql)
        except duckdb.Error as exc:
            raise UserError(clean_error(exc))
        if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
            raise UserError("Only a single SELECT is allowed (check the SQL filter for ';').")
        cur = self.db.cursor()
        ok = False
        if channel:
            with self.active_lock:
                old = self.active.get(channel)
                self.active[channel] = cur
            if old is not None:
                try:
                    old.interrupt()
                except Exception:
                    pass
        try:
            cur.execute(sql)
            if fetch == "cursor":
                ok = True
                return cur
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return cols, rows
        except duckdb.InterruptException:
            raise UserError("Query cancelled by a newer request.")
        except duckdb.Error as exc:
            raise UserError(clean_error(exc))
        finally:
            if not ok:
                self._release(channel, cur)

    def _release(self, channel, cur):
        if channel:
            with self.active_lock:
                if self.active.get(channel) is cur:
                    del self.active[channel]
        try:
            cur.close()
        except Exception:
            pass

    # -- filters
    def where_sql(self, src: Source, spec: dict) -> str:
        clauses = []
        for f in spec.get("filters") or []:
            if f.get("enabled") is False:
                continue
            clauses.append(self._filter_clause(src, f))
        search = (spec.get("search") or "").strip()
        if search:
            clauses.append(self._search_clause(src, search, spec.get("columns")))
        where = (spec.get("where") or "").strip()
        if where:
            clauses.append(f"({where})")
        return (" WHERE " + " AND ".join(clauses)) if clauses else ""

    def _filter_clause(self, src: Source, f: dict) -> str:
        col = f.get("col")
        if col not in src.types:
            raise UserError(f"Unknown column in filter: {col}")
        q, t, op = qident(col), src.types[col], f.get("op", "=")
        kind = type_kind(t)
        val = f.get("value")
        val = "" if val is None else str(val)

        def cast(v):
            if kind == "string":
                return lit(v)
            return f"CAST({lit(v.strip())} AS {t})"

        text = f"CAST({q} AS VARCHAR)"
        if op == "null":
            return f"{q} IS NULL"
        if op == "not_null":
            return f"{q} IS NOT NULL"
        if op == "nan":
            return f"isnan({q})"
        if op == "contains":
            return f"contains(lower({text}), {lit(val.lower())})"
        if op == "not_contains":
            return f"NOT contains(lower({text}), {lit(val.lower())})"
        if op == "starts":
            return f"starts_with(lower({text}), {lit(val.lower())})"
        if op == "ends":
            return f"ends_with(lower({text}), {lit(val.lower())})"
        if op == "regex":
            return f"regexp_matches({text}, {lit(val)})"
        if op in ("in", "not_in"):
            items = [x for x in re.split(r"[,\n]", val) if x.strip() != ""]
            if kind == "string":
                items = [x.strip() for x in items]
            if not items:
                raise UserError(f"Filter on {col}: give a comma-separated list of values.")
            neg = "NOT " if op == "not_in" else ""
            return f"{q} {neg}IN (" + ", ".join(cast(x) for x in items) + ")"
        if op == "between":
            lo, hi = f.get("value"), f.get("value2")
            if lo in (None, "") or hi in (None, ""):
                raise UserError(f"Filter on {col}: 'between' needs two values.")
            return f"{q} BETWEEN {cast(str(lo))} AND {cast(str(hi))}"
        if op in ("=", "!=", "<", "<=", ">", ">="):
            if val == "" and kind != "string":
                raise UserError(f"Filter on {col}: missing value.")
            return f"{q} {op} {cast(val)}"
        raise UserError(f"Unknown filter operator: {op}")

    def _search_clause(self, src: Source, term: str, columns=None) -> str:
        cols = [c for c in (columns or src.columns) if c in src.types] or src.columns
        parts = []
        low = lit(term.lower())
        for c in cols:
            k = type_kind(src.types[c])
            if k == "string":
                parts.append(f"contains(lower({qident(c)}), {low})")
        try:
            num = float(term.replace(",", ""))
        except ValueError:
            num = None
        if num is not None and math.isfinite(num):
            for c in cols:
                k = type_kind(src.types[c])
                if k == "int" and num.is_integer():
                    parts.append(f"{qident(c)} = {int(num)}")
                elif k in ("float", "decimal"):
                    parts.append(f"{qident(c)} = {num!r}")
        if re.fullmatch(r"\d{4}-\d{2}(-\d{2})?", term):
            for c in cols:
                if type_kind(src.types[c]) in ("date", "timestamp"):
                    parts.append(f"starts_with(CAST({qident(c)} AS VARCHAR), {lit(term)})")
        if not parts:
            return "FALSE"
        return "(" + " OR ".join(parts) + ")"

    def _select_list(self, src: Source, spec: dict):
        cols = spec.get("columns") or src.columns
        bad = [c for c in cols if c not in src.types]
        if bad:
            raise UserError(f"Unknown column(s): {', '.join(bad[:5])}")
        return cols

    def _order_sql(self, src: Source, spec: dict) -> str:
        parts = []
        for s in spec.get("sort") or []:
            c = s.get("col")
            if c not in src.types:
                continue
            parts.append(f"{qident(c)} {'DESC' if s.get('desc') else 'ASC'}")
        return (" ORDER BY " + ", ".join(parts)) if parts else ""

    @staticmethod
    def _out_expr(src: Source, c: str) -> str:
        # values with a time zone need pytz in Python - send them as text instead
        if "TIME ZONE" in src.types[c].upper():
            return f"CAST({qident(c)} AS VARCHAR) AS {qident(c)}"
        return qident(c)

    def build_select(self, src: Source, spec: dict, with_order=True) -> str:
        cols = self._select_list(src, spec)
        sql = f"SELECT {', '.join(self._out_expr(src, c) for c in cols)} FROM {src.sql} AS t{self.where_sql(src, spec)}"
        if with_order:
            sql += self._order_sql(src, spec)
        return sql

    def count(self, src: Source, where: str, channel=None) -> int:
        key = (src.sql, where)
        n = self.count_cache.get(key)
        if n is None:
            _, rows = self.run(f"SELECT count(*) FROM {src.sql} AS t{where}", channel)
            n = int(rows[0][0])
            self.count_cache.put(key, n)
        return n

    # -- API actions
    def info(self, rel: str, dataset: bool) -> dict:
        src = self.source(rel, dataset)
        out = {"path": src.rel, "name": os.path.basename(src.full) or os.path.basename(self.root),
               "dataset": dataset,
               "columns": [{"name": c, "type": src.types[c], "kind": type_kind(src.types[c])}
                           for c in src.columns]}
        cur = self.db.cursor()
        try:
            if dataset:
                files = []
                for dirpath, dirnames, filenames in os.walk(src.full):
                    dirnames[:] = [d for d in dirnames if self._visible(d)]
                    for fn in filenames:
                        if fn.endswith(".parquet") and self._visible(fn):
                            files.append(os.path.join(dirpath, fn))
                out["n_files"] = len(files)
                out["size"] = sum(_size(p) for p in files)
                glob = os.path.join(src.full, "**", "*.parquet")
                try:
                    out["rows"] = int(cur.execute(
                        f"SELECT sum(num_rows) FROM parquet_file_metadata({lit(glob)})").fetchone()[0] or 0)
                except duckdb.Error:
                    out["rows"] = self.count(src, "")
                out["partitions"] = sorted({seg.split("=", 1)[0] for p in files
                                            for seg in os.path.relpath(p, src.full).split(os.sep)[:-1]
                                            if "=" in seg})
                out["sample_files"] = [self.rel(p) for p in sorted(files)[:5]]
            else:
                st = os.stat(src.full)
                out["size"], out["mtime"] = st.st_size, st.st_mtime
                p = lit(src.full)
                meta = cur.execute(f"SELECT * FROM parquet_file_metadata({p})")
                names = [d[0] for d in meta.description]
                row = dict(zip(names, meta.fetchone()))
                out["rows"] = int(row.get("num_rows") or 0)
                out["file_meta"] = {k: jv(row.get(k)) for k in
                                    ("created_by", "num_rows", "num_row_groups", "format_version")
                                    if k in row}
                try:
                    cm = cur.execute(
                        f"SELECT path_in_schema, any_value(type), string_agg(DISTINCT compression, ', '), "
                        f"sum(total_compressed_size), sum(total_uncompressed_size), "
                        f"sum(stats_null_count) FROM parquet_metadata({p}) GROUP BY path_in_schema, column_id "
                        f"ORDER BY column_id").fetchall()
                    out["column_meta"] = [{"path": r[0], "physical": r[1], "compression": r[2],
                                           "compressed": jv(r[3]), "uncompressed": jv(r[4]),
                                           "nulls": jv(r[5])} for r in cm]
                except duckdb.Error:
                    pass
                try:
                    kv = cur.execute(f"SELECT key, value FROM parquet_kv_metadata({p})").fetchall()
                    out["kv_meta"] = [{"key": _decode(k), "value": _decode(v, 50000)} for k, v in kv]
                except duckdb.Error:
                    pass
        finally:
            cur.close()
        return out

    def query(self, spec: dict) -> dict:
        t0 = time.time()
        src = self.source(spec.get("path"), bool(spec.get("dataset")))
        limit = max(1, min(int(spec.get("limit") or 100), MAX_PAGE))
        offset = max(0, int(spec.get("offset") or 0))
        where = self.where_sql(src, spec)
        base = self.build_select(src, spec)
        channel = spec.get("channel") or None
        cols, rows = self.run(f"{base} LIMIT {limit} OFFSET {offset}", channel)
        total = self.count(src, where, channel)
        unfiltered = self.count(src, "") if where else total
        return {
            "columns": [{"name": c, "type": src.types.get(c, ""), "kind": type_kind(src.types.get(c, ""))}
                        for c in cols],
            "rows": [[jv(v) for v in r] for r in rows],
            "offset": offset, "limit": limit, "total": total, "unfiltered": unfiltered,
            "sql": base, "elapsed": round(time.time() - t0, 3),
        }

    def stats(self, spec: dict) -> dict:
        t0 = time.time()
        src = self.source(spec.get("path"), bool(spec.get("dataset")))
        col = spec.get("column")
        if col not in src.types:
            raise UserError(f"Unknown column: {col}")
        t = src.types[col]
        kind = type_kind(t)
        q = qident(col)
        ch = spec.get("channel") or "stats"
        vexpr = f"CAST({q} AS TIMESTAMP)" if "TIME ZONE" in t.upper() and kind == "timestamp" else (
            f"CAST({q} AS VARCHAR)" if "TIME ZONE" in t.upper() else q)
        base = f"(SELECT {vexpr} AS v FROM {src.sql} AS t{self.where_sql(src, spec)})"
        out = {"column": col, "type": t, "kind": kind}

        nan_sql = ", count(*) FILTER (WHERE isnan(v))" if kind == "float" else ", 0"
        distinct_sql = "approx_count_distinct(v)" if kind != "nested" else "NULL"
        _, r = self.run(f"SELECT count(*), count(v), {distinct_sql}{nan_sql} FROM {base}", ch)
        n, nn, nd, nnan = r[0]
        out.update(rows=n, non_null=nn, nulls=n - nn, distinct=nd, nan=nnan or 0)

        if is_numeric(kind):
            x = "CAST(v AS DOUBLE)"
            fin = f"v IS NOT NULL AND isfinite({x})"
            _, r = self.run(
                f"SELECT min({x}), max({x}), avg({x}), stddev_samp({x}), "
                f"approx_quantile({x}, [0.005, 0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99, 0.995]), "
                f"sum({x}), count(*) FILTER (WHERE {x} = 0), count(*) FILTER (WHERE {x} < 0) "
                f"FROM {base} WHERE {fin}", ch)
            mn, mx, avg, sd, qs, sm, zeros, neg = r[0]
            out.update(min=jv(mn), max=jv(mx), mean=jv(avg), std=jv(sd), sum=jv(sm), zeros=zeros,
                       negative=neg)
            if qs:
                out["quantiles"] = dict(zip(["0.5%", "1%", "5%", "25%", "50%", "75%", "95%", "99%",
                                             "99.5%"], [jv(v) for v in qs]))
            if mn is not None:
                out["histogram"] = self._histogram(base, x, fin, qs, mn, mx, kind, ch)
        elif kind in ("date", "timestamp"):
            _, r = self.run(f"SELECT min(v), max(v) FROM {base}", ch)
            mn, mx = r[0]
            out.update(min=jv(mn), max=jv(mx))
            if mn is not None:
                a = mn if isinstance(mn, dt.date) else None
                b = mx if isinstance(mx, dt.date) else None
                span = (b - a).days if (a is not None and b is not None) else 0
                unit = "day" if span <= 92 else ("month" if span <= 366 * 12 else "year")
                _, r = self.run(f"SELECT CAST(date_trunc('{unit}', v) AS DATE) AS b, count(*) FROM {base} "
                                f"WHERE v IS NOT NULL GROUP BY 1 ORDER BY 1", ch)
                out["periods"] = {"unit": unit, "bins": [[jv(b), c] for b, c in fill_periods(r, unit)]}
        elif kind == "string":
            _, r = self.run(f"SELECT min(length(v)), max(length(v)), avg(length(v)), min(v), max(v), "
                            f"count(*) FILTER (WHERE v = '') FROM {base}", ch)
            out.update(min_len=r[0][0], max_len=r[0][1], mean_len=jv(r[0][2]), min=jv(r[0][3]),
                       max=jv(r[0][4]), empty=r[0][5])

        few = nd is not None and nd <= 60
        if kind != "nested" and nn and (few or kind in ("string", "bool", "other", "blob")):
            _, r = self.run(f"SELECT v, count(*) AS c FROM {base} GROUP BY v ORDER BY c DESC, v LIMIT 30", ch)
            out["top"] = [[jv(v), c] for v, c in r]
        out["elapsed"] = round(time.time() - t0, 3)
        return out

    def _histogram(self, base, x, fin, qs, mn, mx, kind, ch, nbins=30):
        lo, hi = (qs[0], qs[-1]) if qs else (mn, mx)
        if lo is None or hi is None or lo >= hi:
            lo, hi = mn, mx
        if lo >= hi:
            return {"lo": lo, "hi": hi, "below": 0, "above": 0, "bins": [], "single": True}
        int_bins = kind == "int" and (hi - lo) < nbins
        if int_bins:
            lo, hi = math.floor(lo), math.floor(hi) + 1
            nbins = int(hi - lo)
        width = (hi - lo) / nbins
        _, r = self.run(
            f"SELECT least(greatest(floor(({x} - {lo!r}) / {width!r}), -1), {nbins}) AS b, count(*) "
            f"FROM {base} WHERE {fin} GROUP BY 1 ORDER BY 1", ch)
        counts = {int(b): c for b, c in r}
        bins = [counts.get(i, 0) for i in range(nbins)]
        # values exactly at hi land in bin nbins -> put them in the last bin unless they are above hi
        _, r2 = self.run(f"SELECT count(*) FILTER (WHERE {x} > {hi!r}), count(*) FILTER (WHERE {x} = {hi!r}) "
                         f"FROM {base} WHERE {fin}", ch)
        above, at_hi = r2[0]
        if int_bins:
            above += at_hi
        else:
            bins[-1] += at_hi
        return {"lo": jv(lo), "hi": jv(hi), "width": jv(width), "bins": bins,
                "below": counts.get(-1, 0), "above": above,
                "clipped": bool(lo > mn or hi < mx)}

    def summarize(self, spec: dict) -> dict:
        t0 = time.time()
        src = self.source(spec.get("path"), bool(spec.get("dataset")))
        sql = "SUMMARIZE " + self.build_select(src, spec, with_order=False)
        cols, rows = self.run(sql, spec.get("channel") or "summary")
        return {"columns": cols, "rows": [[jv(v) for v in r] for r in rows],
                "elapsed": round(time.time() - t0, 3)}

    def prepare_export(self, spec: dict) -> dict:
        src = self.source(spec.get("path"), bool(spec.get("dataset")))
        sql = self.build_select(src, spec)
        token = secrets.token_urlsafe(16)
        base = os.path.splitext(os.path.basename(src.full.rstrip(os.sep)))[0] or "export"
        name = base + ("_filtered" if self.where_sql(src, spec) else "") + ".csv"
        self.exports.put(token, (sql, name))
        return {"url": f"/api/export/{token}", "filename": name}

    def code(self, spec: dict) -> dict:
        src = self.source(spec.get("path"), bool(spec.get("dataset")))
        sql = self.build_select(src, spec)
        py = ("import duckdb\n\n"
              f"sql = \"\"\"\n{pretty_sql(sql)}\n\"\"\"\n"
              "df = duckdb.sql(sql).df()        # pandas DataFrame\n")
        return {"sql": pretty_sql(sql), "python": py}


def pretty_sql(sql: str) -> str:
    for kw in (" FROM ", " WHERE ", " ORDER BY "):
        sql = sql.replace(kw, "\n" + kw.strip() + " ", 1)
    sql = sql.replace(" AND ", "\n  AND ")
    return sql


def fill_periods(rows, unit, limit=600):
    """Add zero-count bins for empty days/months/years between the first and last one."""
    counts = {b: c for b, c in rows if b is not None}
    if not counts:
        return []
    cur, last = min(counts), max(counts)
    out = []
    while cur <= last and len(out) < limit:
        out.append((cur, counts.get(cur, 0)))
        if unit == "day":
            cur = cur + dt.timedelta(days=1)
        elif unit == "month":
            cur = dt.date(cur.year + cur.month // 12, cur.month % 12 + 1, 1)
        else:
            cur = dt.date(cur.year + 1, 1, 1)
    return out


def natural_key(s: str):
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", s)]


def clean_error(exc) -> str:
    msg = str(exc)
    msg = re.sub(r"\n\s*LINE \d+:.*", "", msg, flags=re.S)
    return msg.strip()[:2000]


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _decode(v, limit=2000):
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = bytes(v).decode("utf-8", "replace")
    v = "" if v is None else str(v)
    return v if len(v) <= limit else v[:limit] + "…"


# ----------------------------------------------------------------------------- HTTP layer

class Handler(BaseHTTPRequestHandler):
    server_version = "ParquetViewer/" + __version__
    viewer: Viewer = None
    allowed_hosts = None   # set of host names, or None = any
    quiet = False

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # -- responses
    def _send(self, status, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, allow_nan=False, default=str).encode("utf-8"))

    def _host_ok(self) -> bool:
        if self.allowed_hosts is None:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        return name in self.allowed_hosts

    # -- routing
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            if not self._host_ok():
                return self._send(403, b"Forbidden host", "text/plain")
            url = urlparse(self.path)
            path = url.path
            if method == "GET" and path.startswith("/api/export/"):
                return self._export(path.rsplit("/", 1)[-1])
            if path.startswith("/api/"):
                if self.headers.get(API_HEADER) != "1":
                    return self._json({"error": "missing API header"}, 403)
                params = {k: v[-1] for k, v in parse_qs(url.query).items()}
                body = {}
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > 2_000_000:
                        raise UserError("Request too large.")
                    body = json.loads(self.rfile.read(n) or b"{}")
                return self._json(self._api(path[5:], params, body))
            if method == "GET":
                return self._static(path)
            self._send(405, b"Method not allowed", "text/plain")
        except UserError as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover
            traceback.print_exc()
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _api(self, name, params, body):
        v = self.viewer
        if name == "config":
            return {"root": v.root, "root_name": os.path.basename(v.root) or v.root,
                    "duckdb": duckdb.__version__, "version": __version__, "sandboxed": v.sandboxed,
                    "max_page": MAX_PAGE}
        if name == "list":
            return v.list_dir(params.get("path", ""))
        if name == "info":
            return v.info(params.get("path", ""), params.get("dataset") == "1")
        if name == "query":
            return v.query(body)
        if name == "stats":
            return v.stats(body)
        if name == "summarize":
            return v.summarize(body)
        if name == "export":
            return v.prepare_export(body)
        if name == "code":
            return v.code(body)
        raise UserError(f"Unknown API call: {name}")

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            return self._send(404, b"Not found", "text/plain")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            return self._send(404, b"Not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def _export(self, token):
        item = self.viewer.exports.get(token)
        if not item:
            return self._send(404, b"Export link expired - press Export again.", "text/plain")
        sql, name = item
        cur = self.viewer.run(sql, None, fetch="cursor")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([d[0] for d in cur.description])
            while True:
                rows = cur.fetchmany(20000)
                if not rows:
                    break
                for r in rows:
                    w.writerow([csv_value(x) for x in r])
                self.wfile.write(buf.getvalue().encode("utf-8"))
                buf.seek(0)
                buf.truncate()
            self.wfile.write(buf.getvalue().encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.viewer._release(None, cur)
        self.close_connection = True


def make_server(root, host="127.0.0.1", port=8770, memory_limit=None, threads=None,
                show_hidden=False, quiet=False):
    viewer = Viewer(root, memory_limit=memory_limit, threads=threads, show_hidden=show_hidden)
    handler = type("BoundHandler", (Handler,), {})
    handler.viewer = viewer
    handler.quiet = quiet
    handler.allowed_hosts = ({"localhost", "127.0.0.1", "[::1]"}
                             if host in ("127.0.0.1", "localhost", "::1") else None)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd, viewer


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pqv", description="Browse and filter parquet files in a web browser.")
    ap.add_argument("root", nargs="?", default=".",
                    help="folder to browse (default: current folder); nothing above it can be opened")
    ap.add_argument("--port", type=int, default=8770, help="port (default 8770)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default 127.0.0.1 = this computer only)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--memory-limit", default=None, help="DuckDB memory limit, e.g. 2GB (useful on a Pi)")
    ap.add_argument("--threads", type=int, default=None, help="DuckDB threads")
    ap.add_argument("--show-hidden", action="store_true", help="list hidden files and folders")
    ap.add_argument("--quiet", action="store_true", help="do not log requests")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__} (DuckDB {duckdb.__version__})")
    a = ap.parse_args(argv)

    try:
        httpd, viewer = make_server(a.root, a.host, a.port, a.memory_limit, a.threads,
                                    a.show_hidden, a.quiet)
    except OSError as exc:
        sys.exit(f"Could not start on port {a.port}: {exc}. Try --port {a.port + 1}")
    shown = "localhost" if a.host in ("127.0.0.1", "::1", "localhost") else a.host
    url = f"http://{shown}:{a.port}/"
    print(f"Parquet Viewer {__version__}  (DuckDB {duckdb.__version__})")
    print(f"Root : {viewer.root}")
    print(f"Open : {url}      Ctrl+C to stop")
    if a.host not in ("127.0.0.1", "::1", "localhost"):
        print("warning: listening on a network address - anyone who can reach it can read the files")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
