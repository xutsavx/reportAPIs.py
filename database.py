"""
Database access for rmd_aclist.

Deliberately does NOT use a recursive CTE. For 100k+ rows / 10+ levels,
walking the hierarchy row-by-row on the SQL Server side is much slower
than pulling the whole table flat in one shot and linking it in Python
(O(n), see tree_builder.py). One indexed table scan beats N recursive
round trips.
"""
import os
from typing import List, Tuple, Optional

import pyodbc

DB_SERVER = os.getenv("DB_SERVER", "192.168.125.68,1213")
DB_DATABASE = os.getenv("DB_DATABASE", "BRO_STORE")
DB_USER = os.getenv("DB_USER", "sa")          # leave blank to use Windows/trusted auth
DB_PASSWORD = os.getenv("DB_PASSWORD", "Ims16877Nepal")
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
