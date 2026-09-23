# rmd_aclist Tree API

Fetches a self-referencing table (`acid` PK, `acname`, `parent`) from SQL
Server and serves it as a nested JSON tree, built for large data (100k+
rows, 10+ levels).

## How it's fast

1. **One flat SELECT**, not a recursive CTE — `database.py` pulls the
   whole table in a single indexed scan.
2. **O(n) tree build**, not recursive — `tree_builder.py` links parent →
   child in two linear passes, so depth never matters.
3. **Pre-serialized cache** — `cache.py` runs `orjson.dumps()` once per
   rebuild and caches the raw bytes. Every request just returns that
   buffer: no rebuild, no re-serialization, no Pydantic validation of a
   huge nested structure, on the request path.
4. **Background refresh** instead of rebuilding per request, plus a
   manual `/refresh` you can call after writes.

## Setup

```bash
pip install -r requirements.txt
```

Install the SQL Server ODBC driver (Driver 17 or 18) if you don't have
it already, then set env vars (or edit the defaults in `database.py`):

```bash
export DB_SERVER=your-server
export DB_DATABASE=your-db
export DB_USER=your-user       # omit for Windows/trusted auth
export DB_PASSWORD=your-pass
```

Run:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Recommended index

Even though the tree build is a single flat scan, add an index on
`parent` — it helps this query and any other query that filters/joins
on it:

```sql
CREATE NONCLUSTERED INDEX IX_rmd_aclist_parent
ON dbo.rmd_aclist (parent)
INCLUDE (acname);
```

`acid` should already be indexed as the PK/clustered index.

## Endpoints

- `GET /tree` — full tree
- `GET /tree/{acid}` — subtree rooted at `acid`
- `GET /children/{acid}` — one level of children only (for lazy-loading
  UIs — avoids sending the whole 100k+ node tree to the browser)
- `POST /refresh` — force an immediate rebuild
- `GET /health` — row count + last build time

## Keeping data fresh

Two options, not mutually exclusive:

- **Timer**: `REFRESH_INTERVAL_SECONDS` in `main.py` (default 5 min).
- **On-demand**: call `POST /refresh` from wherever writes to
  `rmd_aclist` happen (a trigger, an application event, a cron job).

## Scaling beyond one process

This cache lives in the memory of a single Python process. That's
intentional — it's what makes serving instant. Implications:

- **Single `uvicorn` worker is usually fine.** Once the cache is
  built, serving a request is just "return cached bytes" — extremely
  cheap, so one async worker can handle very high concurrency.
- **If you do run multiple workers/instances** (e.g. behind a load
  balancer), each one rebuilds its own copy independently — they won't
  be perfectly in sync, and you're duplicating the build work. If that
  matters, move the cached bytes into Redis: rebuild in one place,
  `SET` the JSON bytes, and have every worker `GET` and return them
  directly. Everything else in this design stays the same.

## Payload size note

At 100k+ rows, `GET /tree` can be a large response. `GZipMiddleware`
is already enabled to shrink it over the wire. If the frontend renders
an expandable tree UI, prefer `/children/{acid}` for lazy loading
instead of pulling the whole tree at once — the backend is fast either
way, but a multi-MB JSON payload is still a lot for a browser to parse
in one go.
