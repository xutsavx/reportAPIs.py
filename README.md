# Trial Balance Report API

The API performs report calculations and account-tree expansion in Python.
SQL Server is used only for parameterized `SELECT` queries that fetch source
rows and configuration. No stored procedure or SQL function is called.

## Setup

Install dependencies and the SQL Server ODBC driver (Driver 17 or 18):

```powershell
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Set database and Redis credentials in the local `.env` file before starting
the API. It is excluded from Git. See `.env.example` for the available
settings. The application loads `.env` automatically.

Redis stores the complete account list used to build report trees. The list is
loaded from SQL Server on a cache miss and reused to build each request's tree;
tree results are not cached. Each request compares SQL Server's Change
Tracking version with the version stored alongside the cached account rows.
Changes to `dbo.RMD_ACLIST` or `dbo.AccountOrder` cause the rows to be
reloaded and the next tree rebuilt. Redis entries also expire after 60 seconds
without a report request. The `TREE_CACHE_IDLE_SECONDS` and
`TREE_CACHE_KEY_PREFIX` settings are optional.
Redis must be reachable at `REDIS_URL` (default `redis://localhost:6379/0`).
HTTP responses of at least 1,000 bytes are compressed with Brotli when the
client advertises `Accept-Encoding: br`. Redis cache values are uncompressed
JSON bytes.

Before running the API, a DBA must enable SQL Server Change Tracking for the
database and both account source tables, and grant the application SQL login
`VIEW CHANGE TRACKING` on each table:

```sql
ALTER DATABASE [YourDatabase]
SET CHANGE_TRACKING = ON (CHANGE_RETENTION = 2 DAYS, AUTO_CLEANUP = ON);

ALTER TABLE dbo.RMD_ACLIST
ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF);
ALTER TABLE dbo.AccountOrder
ENABLE CHANGE_TRACKING WITH (TRACK_COLUMNS_UPDATED = OFF);

GRANT VIEW CHANGE TRACKING ON dbo.RMD_ACLIST TO [YourAppUser];
GRANT VIEW CHANGE TRACKING ON dbo.AccountOrder TO [YourAppUser];
```

## Endpoints

- `GET /trialBalanceReport` calculates opening, running, and closing balances
  in Python. `DATE1` and `DATE2` are required; query parameters retain the
  SQL report names and defaults: `DIV`, `CID`, `COSTCENTER`, `PHISCALID`,
  `LEDGERWISE`, `SUMMARYREPORT`, `LEVELS`, `SHOWOPENINGTRIALONLY`,
  `SHOWSUBLEDGER`, `SHOWDEBTORSCREDITORSDETAILS`, `SHOWSTOCKVALUE`,
  `SHOWCLOSINGONPY`, `SHOWZEROBL`, `SHOWNETCLOSING`, `NODEACID`, and
  `NODEACNAME`.
- `GET /consolidatedTrialBalanceReport` calculates and pivots closing
  balances across divisions in Python. It accepts `DATE1`, `DATE2`, `DIV`,
  `CID`, `COSTCENTER`, `PHISCALID`, `SHOWOPENINGTRIALONLY`, `SHOWSUBLEDGER`,
  `SHOWDEBTORSCREDITORSDETAILS`, `SHOWSTOCKVALUE`, `LEDGERWISE`, and
  `SHOWNETCLOSING`.
- `GET /trialBalanceTree` builds a Python-expanded tree from the cached
  account list. It accepts `PHISCALID`, `NODEACID`, `NODEACNAME`, `GROUPONLY`,
  `SHOWDEBTORSCREDITORSDETAILS`, `SHOWSUBLEDGER`, and `ISFORPARTYAC`.
- `GET /health` reports API and cache backend status.

The Python stock valuation follows the checked-in newer division-wise FIFO
rules, including perishable-item layers and the latest non-zero rate fallback.
Report results and account trees are recalculated for each request; only the
complete account list is cached. Manual cache invalidation remains disabled.
