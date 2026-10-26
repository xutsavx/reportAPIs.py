import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict

import orjson
from brotli_asgi import BrotliMiddleware
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field

load_dotenv()

from cache import account_cache
from database import (
    fetch_branch_transfer_rows,
    fetch_division_rows,
    fetch_report_metadata,
    fetch_stock_rows,
    fetch_subledger_rows,
    fetch_trial_balance_rows,
)
from report_builder import (
    build_consolidated_trial_balance_report,
    build_trial_balance_report,
)
from tree_builder import build_account_tree_rows

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trial_balance_api")

app = FastAPI(title="Trial Balance Report API")
app.add_middleware(BrotliMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


class SqlParameterModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")


class TrialBalanceParameters(SqlParameterModel):
    date1: datetime = Field(alias="DATE1")
    date2: datetime = Field(alias="DATE2")
    division: str = Field(default="%", alias="DIV", max_length=3)
    company_id: str = Field(default="%", alias="CID", max_length=50)
    cost_center: str = Field(default="%", alias="COSTCENTER", max_length=50)
    fiscal_year: str = Field(default="%", alias="PHISCALID", max_length=25)
    ledger_wise: int = Field(default=0, alias="LEDGERWISE", ge=0, le=255)
    summary_report: int = Field(default=0, alias="SUMMARYREPORT", ge=0, le=255)
    levels: int = Field(default=2, alias="LEVELS")
    show_opening_trial_only: int = Field(
        default=0, alias="SHOWOPENINGTRIALONLY", ge=0, le=255
    )
    show_subledger: int = Field(default=0, alias="SHOWSUBLEDGER", ge=0, le=255)
    show_debtors_creditors_details: int = Field(
        default=0, alias="SHOWDEBTORSCREDITORSDETAILS", ge=0, le=255
    )
    show_stock_value: int = Field(default=0, alias="SHOWSTOCKVALUE", ge=0, le=255)
    show_closing_on_previous_year: int = Field(
        default=0, alias="SHOWCLOSINGONPY", ge=0, le=255
    )
    show_zero_balance: int = Field(default=0, alias="SHOWZEROBL", ge=0, le=255)
    show_net_closing: int = Field(default=1, alias="SHOWNETCLOSING", ge=0, le=1)
    node_account_id: str = Field(default="AL", alias="NODEACID", max_length=25)
    node_account_name: str = Field(default="ACCOUNT", alias="NODEACNAME", max_length=100)


class ConsolidatedTrialBalanceParameters(SqlParameterModel):
    date1: datetime = Field(alias="DATE1")
    date2: datetime = Field(alias="DATE2")
    division: str = Field(default="%", alias="DIV", max_length=3)
    company_id: str = Field(default="%", alias="CID", max_length=50)
    cost_center: str = Field(default="%", alias="COSTCENTER", max_length=50)
    fiscal_year: str = Field(default="%", alias="PHISCALID", max_length=25)
    show_opening_trial_only: int = Field(
        default=0, alias="SHOWOPENINGTRIALONLY", ge=0, le=255
    )
    show_subledger: int = Field(default=0, alias="SHOWSUBLEDGER", ge=0, le=255)
    show_debtors_creditors_details: int = Field(
        default=0, alias="SHOWDEBTORSCREDITORSDETAILS", ge=0, le=255
    )
    show_stock_value: int = Field(default=0, alias="SHOWSTOCKVALUE", ge=0, le=255)
    ledger_wise: int = Field(default=0, alias="LEDGERWISE", ge=0, le=255)
    show_net_closing: int = Field(default=1, alias="SHOWNETCLOSING", ge=0, le=1)


class TrialBalanceTreeParameters(SqlParameterModel):
    fiscal_year: str = Field(default="%", alias="PHISCALID", max_length=25)
    node_account_id: str = Field(default="AL", alias="NODEACID", max_length=200)
    node_account_name: str = Field(default="ACCOUNT", alias="NODEACNAME", max_length=1000)
    group_only: int = Field(default=0, alias="GROUPONLY", ge=0, le=255)
    show_debtors_creditors: int = Field(
        default=0, alias="SHOWDEBTORSCREDITORSDETAILS", ge=0, le=255
    )
    show_subledger: int = Field(default=0, alias="SHOWSUBLEDGER", ge=0, le=255)
    is_for_party_account: int = Field(default=0, alias="ISFORPARTYAC", ge=0, le=255)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"Type {type(value).__name__} is not JSON serializable")


def _build_account_tree(
    parameters: Dict[str, Any], metadata: Dict[str, Any]
) -> list[Dict[str, Any]]:
    show_subledger = bool(
        parameters["SHOWSUBLEDGER"] and metadata["settings"].get("enablesubledger")
    )
    account_rows = account_cache.get_account_rows()
    subledger_rows = (
        fetch_subledger_rows(parameters["PHISCALID"]) if show_subledger else []
    )
    accounts_by_id = {str(row["acid"]): row for row in account_rows}
    for row in subledger_rows:
        account = accounts_by_id.get(str(row["a_acid"]))
        if account is not None:
            row.update(
                acname=account.get("acname"),
                accode=account.get("accode"),
                hassubledger=account.get("hassubledger"),
            )
    return build_account_tree_rows(
        account_rows,
        subledger_rows,
        root_acid=parameters["NODEACID"],
        root_name=parameters["NODEACNAME"],
        show_debtors_creditors=bool(parameters["SHOWDEBTORSCREDITORSDETAILS"]),
        show_subledger=show_subledger,
        is_for_party_account=bool(parameters["ISFORPARTYAC"]),
        account_config=metadata["accounts"],
        group_only=bool(parameters["GROUPONLY"]),
    )


async def _report_response(parameters: TrialBalanceParameters) -> Response:
    started = time.perf_counter()
    try:
        values = parameters.model_dump(by_alias=True)
        metadata = await asyncio.to_thread(fetch_report_metadata, values["PHISCALID"])
        tree_parameters = {
            "PHISCALID": values["PHISCALID"],
            "NODEACID": values["NODEACID"],
            "NODEACNAME": values["NODEACNAME"],
            "GROUPONLY": 0,
            "SHOWDEBTORSCREDITORSDETAILS": values["SHOWDEBTORSCREDITORSDETAILS"],
            "SHOWSUBLEDGER": values["SHOWSUBLEDGER"],
            "ISFORPARTYAC": 0,
        }
        tree_rows = await asyncio.to_thread(_build_account_tree, tree_parameters, metadata)
        transactions = await asyncio.to_thread(fetch_trial_balance_rows, values)
        settings = metadata["settings"]
        branch_rows = []
        if not settings.get("enablesinglebatchmode"):
            branch_rows = await asyncio.to_thread(fetch_branch_transfer_rows, values)
        stock_rows = await asyncio.to_thread(
            fetch_stock_rows, values["PHISCALID"], values["DATE2"]
        )
        rows = await asyncio.to_thread(
            build_trial_balance_report,
            tree_rows,
            transactions,
            values,
            metadata,
            branch_rows,
            stock_rows,
        )
        body = orjson.dumps(rows, default=_json_default)
        logger.info(
            "Python trial balance returned %d rows (%d bytes); API total %.3fs",
            len(rows),
            len(body),
            time.perf_counter() - started,
        )
        return Response(content=body, media_type="application/json")
    except Exception as exc:
        logger.exception("Python trial balance calculation failed")
        raise HTTPException(status_code=500, detail="Trial balance report failed") from exc


@app.get("/trialBalanceReport")
async def trial_balance_report(parameters: TrialBalanceParameters = Depends()):
    """Fetch raw rows and calculate the trial balance in Python."""
    return await _report_response(parameters)


@app.get("/consolidatedTrialBalanceReport")
async def consolidated_trial_balance_report(
    parameters: ConsolidatedTrialBalanceParameters = Depends(),
):
    """Calculate and pivot consolidated closing balances in Python."""
    started = time.perf_counter()
    try:
        values = parameters.model_dump(by_alias=True)
        values["DIV"] = "%"
        metadata = await asyncio.to_thread(fetch_report_metadata, values["PHISCALID"])
        tree_parameters = {
            "PHISCALID": values["PHISCALID"],
            "NODEACID": "AL",
            "NODEACNAME": "ACCOUNT",
            "GROUPONLY": 0,
            "SHOWDEBTORSCREDITORSDETAILS": values["SHOWDEBTORSCREDITORSDETAILS"],
            "SHOWSUBLEDGER": values["SHOWSUBLEDGER"],
            "ISFORPARTYAC": 0,
        }
        tree_rows = await asyncio.to_thread(_build_account_tree, tree_parameters, metadata)
        transactions = await asyncio.to_thread(fetch_trial_balance_rows, values)
        branch_rows = []
        if not metadata["settings"].get("enablesinglebatchmode"):
            branch_rows = await asyncio.to_thread(fetch_branch_transfer_rows, values)
        stock_rows = await asyncio.to_thread(
            fetch_stock_rows, values["PHISCALID"], values["DATE2"]
        )
        divisions = await asyncio.to_thread(fetch_division_rows)
        rows = await asyncio.to_thread(
            build_consolidated_trial_balance_report,
            tree_rows,
            transactions,
            values,
            metadata,
            divisions,
            branch_rows,
            stock_rows,
        )
        body = orjson.dumps(rows, default=_json_default)
        logger.info(
            "Python consolidated trial balance returned %d rows (%d bytes); API total %.3fs",
            len(rows),
            len(body),
            time.perf_counter() - started,
        )
        return Response(content=body, media_type="application/json")
    except Exception as exc:
        logger.exception("Python consolidated trial balance calculation failed")
        raise HTTPException(status_code=500, detail="Consolidated trial balance report failed") from exc


@app.get("/trialBalanceTree")
async def trial_balance_tree(parameters: TrialBalanceTreeParameters = Depends()):
    """Build the Python-expanded account tree from the cached account list."""
    values = parameters.model_dump(by_alias=True)
    try:
        metadata = await asyncio.to_thread(fetch_report_metadata, values["PHISCALID"])
        rows = await asyncio.to_thread(_build_account_tree, values, metadata)
        body = orjson.dumps(rows, default=_json_default)
        return Response(content=body, media_type="application/json")
    except Exception as exc:
        logger.exception("Python account tree expansion failed")
        raise HTTPException(status_code=500, detail="Account tree could not be loaded") from exc


@app.get("/health")
async def health():
    return {"status": "ok", **account_cache.stats()}
