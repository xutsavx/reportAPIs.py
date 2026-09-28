# Trial Balance API Performance Comparison

## Executive Summary

For the single local test performed on 2026-09-28 with `fiscalyear=82/83`, the stored-procedure endpoint was faster end to end:

- `/trialBalanceReportFromSP`: **2.97 seconds**, **6,629,116 response bytes**
- `/trialBalanceReport`: **6.01 seconds**, **5,417,380 response bytes**

That is about a **2.0x lower response time** for the SP endpoint in this run. The SP response was about **22.4% larger**, so its transfer cost was higher. These are single-request observations, not a controlled benchmark; they do not establish which implementation uses less total CPU or memory across both SQL Server and the API host.

A separate stress run launched **50 concurrent requests to each endpoint at once** (100 total), after one warm-up request per endpoint, with a 180-second client timeout. It saturated the tested local setup: `/trialBalanceReport` completed **0/50** calls, while `/trialBalanceReportFromSP` completed **15/50** and had a **107.75 s median** and **173.38 s p95** among successful calls. Treat this as a capacity/failure test, not a valid comparative latency result; neither endpoint met a usable completion rate at this load.

## What Each Endpoint Does

| Stage | `/trialBalanceReport` | `/trialBalanceReportFromSP` |
|---|---|---|
| Account hierarchy | Fetches `rmd_aclist` on each request and builds the tree in Python | Performed inside `dbo.py_trialbalance_api_test` |
| Balance data | SQL query groups `rmd_trntran` by `a_acid` and sums debit/credit for the fiscal year | Returned by the stored procedure |
| Rollup and flattening | Python merges totals into the hierarchy, rolls child totals into parents, then flattens it | Performed inside the stored procedure; API returns rows unchanged |
| JSON output | API serializes the flat rows using `orjson` | API serializes the procedure rows using `orjson` |

Both endpoints return their complete result as one JSON array. The API materializes database rows before serialization; it does not stream rows directly from SQL Server to the client.

## API Timing Breakdown

Each endpoint writes timing details to the Uvicorn/API log. `/trialBalanceReport` reports account-table fetch, Python tree construction, balance-query fetch, merge/flatten, JSON serialization, and total API processing time. `/trialBalanceReportFromSP` reports the following detailed stages:

| Log field | What it measures |
|---|---|
| `connect` | Client wall time opening the ODBC connection |
| `execute_wait` | Client wall time in `cursor.execute`; some procedure work may continue while the client fetches results |
| `result_set` | Time advancing past non-rowset results to the returned rowset |
| `fetch_transfer` | `fetchall()` time, including result transfer and ODBC value decoding |
| `row_mapping` | Python conversion of fetched tuples into dictionaries |
| `close` | ODBC connection close time |
| `db_client_total` | Total measured ODBC client wall time, including connect, execute, fetch, row mapping, and close |
| `serialized` | API-side JSON serialization time and uncompressed JSON byte count |
| `API processing before response` | API wall time through serialization; it excludes response transfer to the caller |

These values estimate where time is spent from the API client's perspective. `execute_wait` is **not** SQL Server CPU time, and `fetch_transfer` is not pure network time: SQL execution, driver work, and transfer may overlap. `db_client_total` is the API-side wall-clock duration for the database call, while SQL Server CPU and per-statement execution require SQL-side instrumentation. `curl` end-to-end time includes response delivery; the API cannot know when the remote client has received the complete body from inside the route handler.

## SQL Procedure Reference

The checked-in `py_trialbalance_api_test.sql` shows this server-side sequence:

1. Build `#DATA` by filtering `rmd_trntran` on `phiscalid`, grouping by `a_acid`, and summing debit and credit.
2. Generate account hierarchy levels from `rmd_aclist.hid_path`, join transaction totals to accounts, expand each account to its ancestors, and aggregate those values into `#ROLLUP`.
3. Create a clustered index on `#ROLLUP.ancestor_path`.
4. Join the account list to rollup values and return accounts ordered by `hid_path`.

The API's SP timing treats this as one procedure call; it cannot separate the four SQL statements or measure their SQL CPU/reads. To measure those, capture an actual execution plan and SQL Server `STATISTICS TIME` and `STATISTICS IO` for the procedure, or use Query Store / Extended Events under representative load. Keep diagnostic output out of the procedure's result sets so the API continues to return only report rows.

## Observed Measurements

The measurements below came from one `curl` request to each local endpoint. They include API work and delivery of the response to the local client.

| Endpoint | Fiscal year | HTTP | End-to-end time | Downloaded bytes |
|---|---:|---:|---:|---:|
| `/trialBalanceReport` | `82/83` | 200 | 6.01 s | 5,417,380 bytes (5.17 MiB) |
| `/trialBalanceReportFromSP` | `82/83` | 200 | 2.97 s | 6,629,116 bytes (6.32 MiB) |

A separate in-process call through the updated SP route recorded this API-side breakdown for 34,782 rows and 6,629,116 serialized bytes:

| Stage | Time |
|---|---:|
| ODBC connect | 0.283 s |
| `cursor.execute` wait | 0.415 s |
| Advance to rowset | 0.000 s |
| Fetch/transfer/decode | 1.114 s |
| Map tuples to dictionaries | 0.169 s |
| Close connection | 0.008 s |
| Total ODBC client time | 1.989 s |
| JSON serialization | 0.239 s |
| API processing through serialization | 2.272 s |

This is one sample and is not a measure of SQL Server CPU. The in-process duration excludes HTTP response delivery; the `curl` duration above includes it.

## 50-Concurrent-Request Stress Test

Test conditions: one warm-up request to each route, followed by 50 requests to each route launched together against the local API (`fiscalyear=82/83`). All clients consumed the complete response body. Each request had a 180-second client timeout. Results:

| Endpoint | Attempted | HTTP 200 completed | Failed/canceled | Successful-response median | Successful-response p95 | Max successful time | Successful data received |
|---|---:|---:|---:|---:|---:|---:|---:|
| `/trialBalanceReport` | 50 | 0 | 50 | N/A | N/A | N/A | 0 MB |
| `/trialBalanceReportFromSP` | 50 | 15 | 35 | 107.75 s | 173.38 s | 173.38 s | 94.83 MiB total (6.32 MiB average per success) |

The client benchmark ran for 180.06 seconds before the request timeout window ended. Failed calls were canceled at the timeout; they are not measured successful latencies. Since the regular endpoint had no successes, no latency percentile can be calculated for it. The large success-latency and failure counts indicate the tested API/database path could not sustain 100 simultaneous full-report requests.

This burst used a local development API process and the configured SQL Server; it does not establish which resource saturated. At the single-request response sizes, completing all 100 calls would require delivering roughly 575 MiB in total. Likely contributors include the single API worker process and its threadpool, concurrent SQL execution/connection pressure, server-side contention, and response generation/transfer. Confirm the bottleneck from API/server logs, SQL Server waits and active requests, and host CPU/memory before selecting a remedy. This result is a failed stress test and is not a production capacity estimate.

The SP endpoint previously spent about 1.86 seconds in FastAPI's recursive JSON normalization on a 34,782-row sample. Replacing that conversion with direct `orjson` serialization removed that particular overhead. The regular endpoint was also changed to direct `orjson` serialization. These changes reduce API serialization work but do not reduce SQL execution, row materialization, or response transfer time.

## Resource Tradeoffs

| Resource | Regular endpoint | SP endpoint | What is known |
|---|---|---|---|
| API CPU | Higher expected: builds the tree, rolls up values, flattens rows, and serializes | Lower expected: fetches result rows and serializes them | CPU was not measured directly. The SP endpoint's API-side work is simpler, but it still materializes and serializes every row. |
| SQL Server CPU | Performs the fiscal-year aggregation; account-tree work and hierarchy rollup run in Python | Performs all stored-procedure logic, including hierarchy and balance calculations | SP SQL CPU, elapsed time, reads, and waits were not captured. Moving work to SQL does not necessarily reduce total CPU. |
| API memory | Expected higher: account rows, tree nodes, balance rows, merged hierarchy and flat rows can coexist during processing | Holds the fetched procedure rows and serialized response body during response creation | Peak process memory/RSS was not measured. The 6.32 MiB body is not the same as the larger in-memory Python object footprint. |
| SQL memory/tempdb | Used for the balance `GROUP BY` and related query work | Depends on the stored procedure's joins, grouping, recursion, sorts, and intermediate results | No execution plan, memory grant, or tempdb measurements are available. |
| Network | Lower in this test: 5.17 MiB | Higher in this test: 6.32 MiB, about 22.4% more | Actual bytes depend on account rows, result columns, and data for the fiscal year. |
| Persistent storage | No report result is stored by the API | No report result is stored by the API | Neither endpoint writes a report file/table. The SP may use tempdb internally; that is unknown without its definition/plan. |

## Which Is Better?

**For observed response time and API-host workload, `/trialBalanceReportFromSP` performed better in this test.** It returned about twice as quickly and avoids Python tree construction and rollup. This is a reasonable choice if the stored procedure is set-based, correctly indexed, and SQL Server has sufficient capacity.

**For total system resource use, there is not enough evidence to declare the SP implementation better.** It moves the calculations from the API host to SQL Server and sends a larger response. If SQL Server is already CPU- or memory-constrained, that may be a worse overall tradeoff despite the lower API latency.

The Python calculation is deliberately linear in the number of accounts and balance rows: the account tree is linked in two passes, balance rows are aggregated, and the hierarchy is traversed to roll totals up. The transaction sums themselves are already performed in SQL. This avoids a recursive SQL tree query, but does consume API CPU and memory.

The SP's calculation efficiency **cannot be judged from this API repository** because the definition of `dbo.py_trialbalance_api_test` and its actual execution plan are not present here. A set-based implementation can be efficient; a cursor, repeated per-account queries, or an expensive recursive plan can be slower and use more SQL resources. The measured endpoint time alone does not reveal which is happening.

## Recommendation for a SaaS Environment

For hundreds of clients, I would use the **stored-procedure approach for the calculation path**, provided the procedure is set-based and its execution plan is validated. It avoids rebuilding the same account hierarchy and recalculating rollups in each API worker, gives SQL Server one place to optimize the report, and the measured endpoint was faster. This is an architectural preference, not proof that SQL Server uses fewer total resources; production CPU, memory, and concurrency tests should confirm it.

The current implementation is **not yet tenant-ready as shown**:

- The API connects to one database configured through global environment variables.
- `/trialBalanceReportFromSP` passes only `fiscalyear`; there is no tenant/company argument in this call.
- The application currently has no authentication or authorization dependency on the report route, and CORS allows all origins. CORS is not tenant authorization.

Before serving multiple customers, establish tenant isolation. Choose either database-per-tenant routing or a shared database with a tenant/company key enforced in every procedure and query (ideally reinforced with SQL Server Row-Level Security where appropriate). Derive the tenant identifier from the authenticated identity, never trust a tenant ID supplied only by the request. If using a shared database, the stored procedure should accept and filter by the tenant/company key as well as fiscal year, and indexes should lead with the tenant/company and fiscal-year predicates where the workload and schema support that design.

For hundreds of concurrent users, also:

- Put a per-tenant and global limit on simultaneous report executions; queue or reject excess work rather than exhausting SQL connections and API worker threads.
- Use connection pooling with explicit connection/query timeouts, and monitor SQL connection count, waits, CPU, memory grants, and per-tenant latency.
- Consider caching stable account hierarchies per tenant with explicit invalidation if the business permits it; do not share cached tenant data across customers.
- Avoid returning multi-megabyte arrays for interactive screens at high concurrency. Add pagination, filters, or an asynchronous report-download flow if clients do not require the complete report in one response. Preserve the full-array endpoint only where that contract is necessary.
- Load-test with representative tenant sizes and concurrent requests, checking correctness and isolation as well as p95/p99 latency and total SQL/API resource use.

If the SQL Server is the constrained shared resource while API instances have spare capacity, the Python calculation path may be preferable after measurement. If API CPU/memory or repeated hierarchy work is the bottleneck and the SP plan is efficient, keep the calculation in SQL. For a SaaS deployment, tenant isolation and concurrency controls matter more than choosing the calculation location in isolation.

## Recommended Benchmark Before Choosing Permanently

Run both endpoints for the same fiscal year at least 10 times after one warm-up request, and compare median and p95 latency. Capture these metrics for each run:

1. API timing logs report the stages described above. Use SQL Server statistics separately to split work within the stored procedure.
2. Record response byte counts and row counts. Confirm both reports contain equivalent account totals and include the same account scope before comparing sizes.
3. Measure API process CPU and peak working-set/RSS during each request.
4. For the SP, inspect the actual execution plan and collect SQL Server elapsed time, worker time, logical reads, memory grant, spills, and tempdb use. Use the same SQL measurements for the regular endpoint's aggregation query where possible.
5. Repeat under representative concurrent load if these endpoints will be called concurrently in production.

A decision should consider the combined SQL Server and API-host costs, not only which endpoint returns first.
