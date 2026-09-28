import asyncio
import logging
import time
from contextlib import asynccontextmanager
from decimal import Decimal

import orjson
from typing import Any, Dict, Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from brotli_asgi import BrotliMiddleware
from pydantic import BaseModel, Field

from cache import tree_cache
from database import (
    execute_report_source_with_timings,
    fetch_all_rows,
    fetch_trial_balance_rows,
)
from report_builder import build_trial_balance, flatten_trial_balance
from tree_builder import build_tree

logging.basicConfig(level=logging.INFO)

# How often to rebuild in the background. Tune to how often the
# underlying data actually changes - or set very high and rely on
# POST /refresh triggered from wherever writes happen.
# REFRESH_INTERVAL_SECONDS = 300


# async def periodic_refresh():
#     while True:
#         await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
#         try:
#             await asyncio.to_thread(tree_cache.rebuild)
#         except Exception:
#             logging.exception("Background tree refresh failed")


# @asynccontextmanager
# async def lifespan(app: FastAPI):
#     # Build once at startup so the first request is already fast.
#     await asyncio.to_thread(tree_cache.rebuild)
#     task = asyncio.create_task(periodic_refresh())
#     yield
#     task.cancel()


app = FastAPI(title="rmd_aclist tree API")
app.add_middleware(BrotliMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this for production
    allow_methods=["*"],
    allow_headers=["*"],
)


class ReportSourceRequest(BaseModel):
    source_type: Literal["query", "procedure"]
    source: str = Field(min_length=1)
    parameters: Dict[str, Any] = Field(default_factory=dict)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Type {type(value).__name__} is not JSON serializable")


# @app.get("/tree")
# async def get_tree():
#     """Full tree. Served as pre-serialized bytes - no build or
#     validation cost per request."""
#     return Response(content=tree_cache.get_full_tree_bytes(), media_type="application/json")


# @app.post("/report-data")
# async def get_report_data(request: ReportSourceRequest):
#     """Run a parameterized query or stored procedure and preserve its column shape."""
#     try:
#         return await asyncio.to_thread(
#             execute_report_source, request.source_type, request.source, request.parameters
#         )
#     except Exception as exc:
#         logging.exception("Report data source failed")
#         raise HTTPException(400, str(exc)) from exc


@app.get("/trialBalanceReport")
async def trial_balance_report(fiscalyear: str):
    """Fetch the account tree and balances in real time, then return flat rows."""
    request_started = time.perf_counter()
    try:
        tree_fetch_started = time.perf_counter()
        account_rows = await asyncio.to_thread(fetch_all_rows)
        tree_fetch_seconds = time.perf_counter() - tree_fetch_started
        logging.info(
            "Trial balance: fetched tree data in %.3f seconds (%d accounts)",
            tree_fetch_seconds,
            len(account_rows),
        )

        tree_build_started = time.perf_counter()
        tree, _nodes = await asyncio.to_thread(build_tree, account_rows)
        tree_build_seconds = time.perf_counter() - tree_build_started
        logging.info(
            "Trial balance: created account tree in %.3f seconds", tree_build_seconds
        )

        balance_fetch_started = time.perf_counter()
        rows = await asyncio.to_thread(fetch_trial_balance_rows, fiscalyear)
        balance_fetch_seconds = time.perf_counter() - balance_fetch_started
        logging.info(
            "Trial balance: fetched balance data in %.3f seconds (%d accounts)",
            balance_fetch_seconds,
            len(rows),
        )

        merge_started = time.perf_counter()
        rolled_up_tree = await asyncio.to_thread(
            build_trial_balance, tree, rows, "a_acid", "dramnt", "cramnt"
        )
        report_rows = await asyncio.to_thread(flatten_trial_balance, rolled_up_tree)
        merge_seconds = time.perf_counter() - merge_started
        serialization_started = time.perf_counter()
        response_body = orjson.dumps(report_rows, default=_json_default)
        response = Response(content=response_body, media_type="application/json")
        serialization_seconds = time.perf_counter() - serialization_started
        total_seconds = time.perf_counter() - request_started
        logging.info(
            "Trial balance: merged data in %.3f seconds, serialized %d rows (%d bytes) in %.3f seconds; total request %.3f seconds",
            merge_seconds,
            len(report_rows),
            len(response_body),
            serialization_seconds,
            total_seconds,
        )
        return response
    except Exception as exc:
        logging.exception("Trial balance report failed")
        raise HTTPException(400, str(exc)) from exc


@app.get("/trialBalanceReportFromSP")
async def trial_balance_report_from_sp(fiscalyear: str):
    """Return the trial-balance rows produced by the stored procedure."""
    procedure_started = time.perf_counter()
    try:
        rows, db_timings = await asyncio.to_thread(
            execute_report_source_with_timings,
            "procedure",
            "dbo.py_trialbalance_api_test",
            {"fiscalyear": fiscalyear},
        )
        logging.info(
            "Trial balance SP: rows=%d, ODBC client timings seconds: connect=%.3f execute_wait=%.3f result_set=%.3f fetch_transfer=%.3f row_mapping=%.3f close=%.3f db_client_total=%.3f",
            len(rows),
            db_timings["connect_seconds"],
            db_timings["execute_seconds"],
            db_timings["result_set_seconds"],
            db_timings["fetch_seconds"],
            db_timings["mapping_seconds"],
            db_timings["close_seconds"],
            db_timings["client_total_seconds"],
        )
        serialization_started = time.perf_counter()
        response_body = orjson.dumps(rows, default=_json_default)
        response = Response(content=response_body, media_type="application/json")
        serialization_seconds = time.perf_counter() - serialization_started
        logging.info(
            "Trial balance SP: serialized %d rows (%d bytes) in %.3f seconds; API processing before response=%.3f seconds",
            len(rows),
            len(response.body),
            serialization_seconds,
            time.perf_counter() - procedure_started,
        )
        return response
    except Exception as exc:
        logging.exception("Trial balance SP report failed")
        raise HTTPException(400, str(exc)) from exc


# @app.get("/tree/{acid}")
# async def get_subtree(acid: str):
#     """Just the subtree rooted at acid, including all descendants."""
#     data = tree_cache.get_subtree_bytes(acid)
#     if data is None:
#         raise HTTPException(404, f"acid {acid} not found")
#     return Response(content=data, media_type="application/json")


# @app.get("/children/{acid}")
# async def get_children(acid: str):
#     """One level of children only - for expandable/lazy-loading UI
#     trees that don't want the whole payload at once."""
#     data = tree_cache.get_children_bytes(acid)
#     if data is None:
#         raise HTTPException(404, f"acid {acid} not found")
#     return Response(content=data, media_type="application/json")


# @app.post("/refresh")
# async def refresh():
#     """Force an immediate rebuild - call this from a trigger/webhook
#     right after writes to rmd_aclist if you need fresher-than-5-minute data."""
#     elapsed = await asyncio.to_thread(tree_cache.rebuild)
#     return {"status": "ok", "seconds": round(elapsed, 3), **tree_cache.stats()}


@app.get("/health")
async def health():
    return {"status": "ok", **tree_cache.stats()}
