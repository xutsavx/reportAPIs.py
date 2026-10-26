"""
Database access for rmd_aclist.

Deliberately does NOT use a recursive CTE. For 100k+ rows / 10+ levels,
walking the hierarchy row-by-row on the SQL Server side is much slower
than pulling the whole table flat in one shot and linking it in Python
(O(n), see tree_builder.py). One indexed table scan beats N recursive
round trips.
"""
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import pyodbc

DB_SERVER = os.getenv("DB_SERVER", "")
DB_DATABASE = os.getenv("DB_DATABASE", "")
DB_USER = os.getenv("DB_USER", "")          # leave blank to use Windows/trusted auth
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_DRIVER = os.getenv("DB_DRIVER", "{ODBC Driver 18 for SQL Server}")
DB_TRUST_CERT = os.getenv("DB_TRUST_SERVER_CERTIFICATE", "yes")


def get_connection_string() -> str:
    auth = f"UID={DB_USER};PWD={DB_PASSWORD};" if DB_USER else "Trusted_Connection=yes;"
    return (
        f"DRIVER={DB_DRIVER};SERVER={DB_SERVER};DATABASE={DB_DATABASE};"
        f"{auth}TrustServerCertificate={DB_TRUST_CERT};"
    )


def _fetch_dicts(cursor) -> List[Dict[str, Any]]:
    columns = [column[0].lower() for column in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_account_rows() -> List[Dict[str, Any]]:
    """Fetch account hierarchy fields for in-process tree construction."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT A.ACID, A.TYPE, A.ACCODE, A.ACNAME, A.PARENT,
                   ISNULL(A.MCAT, '') AS MCAT,
                   ISNULL(A.HASSUBLEDGER, 0) AS HASSUBLEDGER,
                   A.PTYPE, ISNULL(O.ORD, 1000) AS ACCOUNT_ORDER
            FROM dbo.RMD_ACLIST A WITH (NOLOCK)
            LEFT JOIN dbo.AccountOrder O WITH (NOLOCK) ON O.ACID = A.ACID
            """
        )
        return _fetch_dicts(cursor)
    finally:
        conn.close()


def fetch_subledger_rows(fiscalyear: str) -> List[Dict[str, Any]]:
    """Fetch subledger links used to construct synthetic account-tree nodes."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT T.A_ACID, ISNULL(T.SL_ACID, '') AS SL_ACID,
                   S.SL_ACNAME
            FROM dbo.RMD_TRNTRAN T WITH (NOLOCK)
            LEFT JOIN dbo.RMD_SUBLEDGER_ACLIST S WITH (NOLOCK)
                ON S.SL_ACID = T.SL_ACID
            WHERE T.PHISCALID = ?
            """,
            fiscalyear,
        )
        return _fetch_dicts(cursor)
    finally:
        conn.close()


def fetch_trial_balance_rows(parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch raw ledger rows; all report grouping and calculations stay in Python."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT T.A_ACID, T.DRAMNT, T.CRAMNT, ISNULL(T.SL_ACID, '') AS SL_ACID,
                   T.DIVISION, T.COMPANYID, T.PHISCALID, T.COSTCENTER,
                   M.TRNDATE, M.VOUCHERTYPE, A.TYPE, A.PTYPE, A.HASSUBLEDGER
            FROM dbo.RMD_TRNTRAN T WITH (NOLOCK)
            INNER JOIN dbo.RMD_ACLIST A WITH (NOLOCK)
                ON A.ACID = T.A_ACID AND A.TYPE = 'A'
            INNER JOIN dbo.RMD_TRNMAIN M WITH (NOLOCK) ON M.VCHRNO = T.VCHRNO
            WHERE M.TRNDATE <= ? AND M.PHISCALID = ?
              AND (? = '%' OR T.DIVISION = ?)
              AND (? = '%' OR T.COSTCENTER = ?)
            """,
            parameters["DATE2"],
            parameters["PHISCALID"],
            parameters["DIV"],
            parameters["DIV"],
            parameters["COSTCENTER"],
            parameters["COSTCENTER"],
        )
        return _fetch_dicts(cursor)
    finally:
        conn.close()


def fetch_report_metadata(fiscal_year: str) -> Dict[str, Any]:
    """Fetch configuration and fiscal-year values needed by Python calculations."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT ISNULL(ENABLESUBLEDGER, 0) AS ENABLESUBLEDGER,
                   ISNULL(ENABLESINGLEBATCHMODE, 0) AS ENABLESINGLEBATCHMODE,
                   ISNULL(ENABLEITEMWISECOSTCENTER, 0) AS ENABLEITEMWISECOSTCENTER,
                   ISNULL(ENABLEBATCH, 0) AS ENABLEBATCH,
                   ISNULL(EnableAcBaseBranchTransfer, 1) AS ENABLEACBASEBRANCHTRANSFER,
                   ISNULL(EnableBranchWiseTfrInOutAC, 0) AS ENABLEBRANCHWISETFRINOUTAC,
                   ISNULL(EnableLateACPosting_TransferOut, 0) AS ENABLELATEACPOSTING_TRANSFEROUT
            FROM dbo.SETTING WITH (NOLOCK)
            """
        )
        settings = _fetch_dicts(cursor)
        cursor.execute(
            "SELECT BEGINDATE FROM dbo.PHISCALYEAR WITH (NOLOCK) WHERE PHISCALID = ?",
            fiscal_year,
        )
        fiscal_rows = _fetch_dicts(cursor)
        cursor.execute(
            "SELECT PARTICULARS, TRNAC FROM dbo.RMD_TAXCHARGES_CONFIG WITH (NOLOCK)"
        )
        configs = _fetch_dicts(cursor)
        return {
            "settings": settings[0] if settings else {},
            "begin_date": fiscal_rows[0]["begindate"] if fiscal_rows else None,
            "accounts": {str(row["particulars"]).upper(): row["trnac"] for row in configs},
        }
    finally:
        conn.close()


def fetch_branch_transfer_rows(parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch raw branch-transfer inventory movements for Python accounting."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT M.VCHRNO, M.REFBILL, M.BILLTO, M.BILLTOADD, M.VOUCHERTYPE,
                   M.TRNDATE, M.PHISCALID, M.DIVISION, M.COSTCENTER,
                   P.MCODE, P.BATCH, P.QUANTITY, P.RATE, P.REALQTY_IN, P.REALQTY,
                   I.PTYPE, BPM.LANDINGCOST, D.ACID AS DESTINATION_ACID,
                   D.TRANSFERINAC, D.TRANSFEROUTAC,
                   TI.TRNAC AS CONFIG_TRANSFERINAC,
                   TI.DEFAULT_TRNAC AS DEFAULT_TRANSFERINAC,
                   TOC.TRNAC AS CONFIG_TRANSFEROUTAC,
                   TOC.DEFAULT_TRNAC AS DEFAULT_TRANSFEROUTAC
            FROM dbo.INVMAIN M WITH (NOLOCK)
            INNER JOIN dbo.INVPROD P WITH (NOLOCK) ON P.VCHRNO = M.VCHRNO
            INNER JOIN dbo.MENUITEM I WITH (NOLOCK) ON I.MCODE = P.MCODE
            LEFT JOIN dbo.BATCHPRICE_MASTER BPM WITH (NOLOCK)
                ON BPM.MCODE = P.MCODE AND BPM.BATCHCODE = P.BATCH
            LEFT JOIN dbo.DIVISION D WITH (NOLOCK)
                ON D.INITIAL = CASE WHEN M.VOUCHERTYPE = 'TO' THEN M.BILLTOADD ELSE M.BILLTO END
            LEFT JOIN dbo.RMD_TAXCHARGES_CONFIG TI WITH (NOLOCK)
                ON TI.PARTICULARS = 'BRANCHTRANSFERIN'
            LEFT JOIN dbo.RMD_TAXCHARGES_CONFIG TOC WITH (NOLOCK)
                ON TOC.PARTICULARS = 'BRANCHTRANSFEROUT'
            WHERE M.TRNDATE BETWEEN ? AND ? AND M.PHISCALID = ?
              AND M.VOUCHERTYPE IN ('TR', 'TO')
              AND (? = '%' OR ISNULL(CONVERT(VARCHAR, M.COSTCENTER), '') = ?)
            """,
            parameters["DATE1"],
            parameters["DATE2"],
            parameters["PHISCALID"],
            parameters["COSTCENTER"],
            parameters["COSTCENTER"],
        )
        return _fetch_dicts(cursor)
    finally:
        conn.close()


def fetch_stock_rows(fiscal_year: str, through_date: datetime) -> List[Dict[str, Any]]:
    """Fetch raw inventory movements and rates for Python stock valuation."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT M.VCHRNO, M.TRNDATE, M.TRNTIME, M.PHISCALID, M.VOUCHERTYPE,
                   M.DIVISION, P.DIVISION AS PRODUCTDIVISION, M.COSTCENTER,
                   P.MCODE, P.BATCH, P.NBATCHID,
                   P.WAREHOUSE, P.REALQTY_IN, P.REALQTY, P.REALRATE, P.RATE,
                   P.CRATE, P.PRATE, I.PTYPE, I.TYPE AS ITEMTYPE, I.ITEMCOSTCENTER,
                   I.CRATE AS ITEMCRATE, I.PRATE_A, D.COSTCENTER AS DIVISIONCOSTCENTER,
                   W.ISVIRTUAL, W.ISADJUSTMENT, W.DIVISION AS WAREHOUSEDIVISION,
                   BPM.LANDINGCOST
            FROM dbo.RMD_TRNMAIN M WITH (NOLOCK)
            INNER JOIN dbo.RMD_TRNPROD P WITH (NOLOCK) ON P.VCHRNO = M.VCHRNO
            INNER JOIN dbo.MENUITEM I WITH (NOLOCK) ON I.MCODE = P.MCODE
            INNER JOIN dbo.RMD_WAREHOUSE W WITH (NOLOCK) ON W.NAME = P.WAREHOUSE
            INNER JOIN dbo.DIVISION D WITH (NOLOCK) ON D.INITIAL = M.DIVISION
            LEFT JOIN dbo.vwBatchPriceMaster BPM WITH (NOLOCK)
                ON BPM.MCODE = P.MCODE AND BPM.BATCHCODE = P.BATCH
               AND ISNULL(BPM.ID, '') = ISNULL(P.NBATCHID, '')
            WHERE M.PHISCALID = ? AND M.TRNDATE <= ?
              AND I.TYPE = 'A' AND I.PTYPE < 10
            """,
            fiscal_year,
            through_date,
        )
        return _fetch_dicts(cursor)
    finally:
        conn.close()


def fetch_division_rows() -> List[Dict[str, Any]]:
    """Fetch division identifiers and display names for the consolidated report."""
    conn = pyodbc.connect(get_connection_string())
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT INITIAL, NAME FROM dbo.DIVISION WITH (NOLOCK)")
        return _fetch_dicts(cursor)
    finally:
        conn.close()
