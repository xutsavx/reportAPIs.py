"""
Database access for rmd_aclist.

Deliberately does NOT use a recursive CTE. For 100k+ rows / 10+ levels,
walking the hierarchy row-by-row on the SQL Server side is much slower
than pulling the whole table flat in one shot and linking it in Python
(O(n), see tree_builder.py). One indexed table scan beats N recursive
round trips.
"""
import os
import re
import time
from typing import Any, Dict, List, Tuple, Optional

import pyodbc

DB_SERVER = os.getenv("DB_SERVER", "103.90.86.144,55451")
DB_DATABASE = os.getenv("DB_DATABASE", "SALT_HO_UAT")
DB_USER = os.getenv("DB_USER", "ims")          # leave blank to use Windows/trusted auth
DB_PASSWORD = os.getenv("DB_PASSWORD", "Uis02mQazbdgteyu")
DB_DRIVER = os.getenv("DB_DRIVER", "{ODBC Driver 18 for SQL Server}")
DB_TRUST_CERT = os.getenv("DB_TRUST_SERVER_CERTIFICATE", "yes")


def get_connection_string() -> str:
    auth = f"UID={DB_USER};PWD={DB_PASSWORD};" if DB_USER else "Trusted_Connection=yes;"
    return (
        f"DRIVER={DB_DRIVER};SERVER={DB_SERVER};DATABASE={DB_DATABASE};"
        f"{auth}TrustServerCertificate={DB_TRUST_CERT};"
    )


def fetch_all_rows() -> List[Tuple[str, str, Optional[str]]]:
    """
    One flat, indexed SELECT for the whole table. Runs only at startup
    and on periodic/manual refresh - never on a per-request path.
    """
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        # NOLOCK avoids read-blocking on a hot table; drop it if you need
        # strict consistency and occasional dirty reads aren't acceptable.
        cursor.execute(
            """
            SELECT acid, acname, parent
            FROM dbo.rmd_aclist WITH (NOLOCK)
            """
        )
        return [(r[0], r[1], r[2]) for r in cursor.fetchall()]
    finally:
        conn.close()


def fetch_trial_balance_rows(fiscalyear: str) -> List[Dict[str, Any]]:
    """Aggregate transaction debits and credits by account id."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT a_acid, SUM(ISNULL(dramnt, 0)) AS dramnt,
                   SUM(ISNULL(cramnt, 0)) AS cramnt
            FROM dbo.rmd_trntran WITH (NOLOCK)
            where phiscalid = ?
            GROUP BY a_acid
            having SUM(ISNULL(dramnt, 0)) + SUM(ISNULL(cramnt, 0)) <> 0;
            """,
            (fiscalyear,)
        )
        return [
            {"a_acid": row[0], "dramnt": row[1], "cramnt": row[2]}
            for row in cursor.fetchall()
        ]
    finally:
        conn.close()


def execute_report_source(
    source_type: str,
    source: str,
    parameters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Execute a report source and return rows by column name."""
    rows, _timings = execute_report_source_with_timings(source_type, source, parameters)
    return rows


def execute_report_source_with_timings(
    source_type: str,
    source: str,
    parameters: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
    """Execute a report source and time client-observable ODBC stages."""
    if source_type not in {"query", "procedure"}:
        raise ValueError("source_type must be 'query' or 'procedure'")
    if not source.strip():
        raise ValueError("source cannot be empty")
    if source_type == "procedure" and not re.fullmatch(r"[\w.\[\]]+", source):
        raise ValueError("procedure must be a SQL identifier, optionally schema-qualified")

    total_started = time.perf_counter()
    timings = {
        "connect_seconds": 0.0,
        "execute_seconds": 0.0,
        "result_set_seconds": 0.0,
        "fetch_seconds": 0.0,
        "mapping_seconds": 0.0,
        "close_seconds": 0.0,
        "client_total_seconds": 0.0,
    }
    connect_started = time.perf_counter()
    conn = pyodbc.connect(get_connection_string())
    timings["connect_seconds"] = time.perf_counter() - connect_started
    try:
        # SQL Server UDT columns (ODBC type -151) are returned as bytes so
        # stored-procedure results remain fetchable and JSON serializable.
        conn.add_output_converter(-151, lambda value: None if value is None else bytes(value).hex())
        cursor = conn.cursor()
        values = list(parameters.values())
        execute_started = time.perf_counter()
        if source_type == "procedure":
            placeholders = ", ".join("?" for _ in values)
            cursor.execute(f"EXEC {source} {placeholders}" if values else f"EXEC {source}", *values)
        else:
            cursor.execute(source, *values)
        timings["execute_seconds"] = time.perf_counter() - execute_started

        result_set_started = time.perf_counter()
        while cursor.description is None:
            if not cursor.nextset():
                timings["result_set_seconds"] = time.perf_counter() - result_set_started
                return [], timings
        timings["result_set_seconds"] = time.perf_counter() - result_set_started

        columns = [column[0] for column in cursor.description]
        fetch_started = time.perf_counter()
        fetched_rows = cursor.fetchall()
        timings["fetch_seconds"] = time.perf_counter() - fetch_started

        mapping_started = time.perf_counter()
        rows = [dict(zip(columns, row)) for row in fetched_rows]
        timings["mapping_seconds"] = time.perf_counter() - mapping_started
        return rows, timings
    finally:
        close_started = time.perf_counter()
        conn.close()
        timings["close_seconds"] = time.perf_counter() - close_started
        timings["client_total_seconds"] = time.perf_counter() - total_started
