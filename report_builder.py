from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from fnmatch import fnmatchcase
from typing import Any, Dict, Iterable, List, Optional, Tuple


def _number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(value if isinstance(value, (int, float, Decimal)) else str(value).replace(",", ""))


def build_trial_balance(tree: List[Dict[str, Any]], rows: Iterable[Dict[str, Any]], account_field: str,
                        debit_field: str, credit_field: str) -> List[Dict[str, Any]]:
    totals = defaultdict(lambda: {"debit": 0.0, "credit": 0.0, "rows": []})
    for row in rows:
        account_id = str(row.get(account_field, ""))
        if not account_id:
            continue
        totals[account_id]["debit"] += _number(row.get(debit_field))
        totals[account_id]["credit"] += _number(row.get(credit_field))
        totals[account_id]["rows"].append(row)

    def merge(node: Dict[str, Any]) -> Dict[str, Any]:
        children = [merge(child) for child in node.get("children", [])]
        own = totals.get(str(node["acid"]), {"debit": 0.0, "credit": 0.0, "rows": []})
        debit = own["debit"] + sum(child["debit"] for child in children)
        credit = own["credit"] + sum(child["credit"] for child in children)
        return {
            **node,
            "debit": debit,
            "credit": credit,
            "balance": debit - credit,
            "transactions": own["rows"],
            "children": children,
        }

    return [merge(root) for root in tree]

def flatten_trial_balance(tree: List[Dict[str, Any]], indent: str = "    ") -> List[Dict[str, Any]]:
    """Return the rolled-up tree as depth-first rows for tabular reports."""
    result: List[Dict[str, Any]] = []

    def visit(node: Dict[str, Any], depth: int) -> None:
        result.append({
            "acid": node["acid"],
            "acname": f"{indent * depth}{node['acname']}",
            "debit": node["debit"],
            "credit": node["credit"],
            "balance": node["balance"],
            "depth": depth,
            "has_children": bool(node.get("children")),
        })
        for child in node.get("children", []):
            visit(child, depth + 1)

    for root in tree:
        visit(root, 0)
    return result


AMOUNT_FIELDS = (
    "OPENING_DR", "OPENING_CR", "RUNNING_DR", "RUNNING_CR", "CLOSING_DR", "CLOSING_CR"
)
ZERO_AMOUNTS = {field: Decimal(0) for field in AMOUNT_FIELDS}


def _decimal(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value).replace(",", ""))


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


def _is_match(value: Any, pattern: str) -> bool:
    return fnmatchcase(str(value or ""), pattern.replace("%", "*"))


def _amount_pair(debit: Decimal, credit: Decimal, show_net: bool) -> Tuple[Decimal, Decimal]:
    if not show_net:
        return debit, credit
    net = debit - credit
    return (net, Decimal(0)) if net >= 0 else (Decimal(0), -net)


def _stock_value(
    stock_rows: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
    opening: bool,
) -> Decimal:
    settings = metadata.get("settings", {})
    start_date = _as_date(parameters["DATE1"])
    end_date = _as_date(parameters["DATE2"])
    begin_date = _as_date(metadata.get("begin_date") or parameters["DATE1"])
    cost_center_filter = str(parameters["COSTCENTER"])
    division_filter = str(parameters["DIV"])
    item_cost_center = bool(settings.get("enableitemwisecostcenter"))
    batch_mode = int(settings.get("enablebatch") or 0)

    batches: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    fifo_stock: Dict[Tuple[str, str], Decimal] = defaultdict(Decimal)
    fifo_purchases: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    opening_rates: Dict[str, List[Tuple[Decimal, Decimal]]] = defaultdict(list)

    allowed_fifo_vouchers = {"PI", "OP", "PD", "RP", "DA", "DM", "SA", "TR"}
    for row in stock_rows:
        if bool(row.get("isvirtual")):
            continue
        row_date = _as_date(row["trndate"])
        voucher_type = str(row.get("vouchertype") or "")
        warehouse_division = str(row.get("warehousedivision") or "")
        if division_filter != "%" and warehouse_division != division_filter:
            continue

        item_code = str(row["mcode"])
        product_type = int(row.get("ptype") or 0)
        if product_type == 9 and not bool(row.get("isadjustment")):
            if opening and voucher_type == "OP":
                opening_rates[item_code].append(
                    (_decimal(row.get("realrate")), _decimal(row.get("realqty_in")))
                )
            elif not opening and (row_date <= end_date or voucher_type == "OP"):
                fifo_stock[(warehouse_division, item_code)] += (
                    _decimal(row.get("realqty_in")) - _decimal(row.get("realqty"))
                )
                if voucher_type in allowed_fifo_vouchers and _decimal(row.get("realqty_in")) > 0:
                    receipt_rate = _decimal(row.get("crate")) or _decimal(row.get("rate"))
                    if receipt_rate:
                        fifo_purchases[(warehouse_division, item_code)].append({
                            "date": row.get("trndate"),
                            "voucher": str(row.get("vchrno") or ""),
                            "qty": _decimal(row.get("realqty_in")),
                            "rate": receipt_rate,
                        })

        if opening:
            included = row_date < start_date or voucher_type == "OP"
        else:
            included = begin_date <= row_date <= end_date
        if not included:
            continue

        batch = "PERI~" if product_type == 9 else str(row.get("batch") or "")
        batch_id = str(row.get("nbatchid") or "") if batch_mode == 2 else ""
        effective_cost_center = (
            row.get("itemcostcenter") if item_cost_center else None
        ) or row.get("costcenter") or row.get("divisioncostcenter") or "9999"
        if cost_center_filter != "%" and not _is_match(effective_cost_center, cost_center_filter):
            continue

        batch_key = (item_code, batch, batch_id)
        batch_data = batches.setdefault(
            batch_key,
            {"qty": Decimal(0), "ptype": product_type, "landing_cost": _decimal(row.get("landingcost"))},
        )
        batch_data["qty"] += _decimal(row.get("realqty_in")) - _decimal(row.get("realqty"))

    fifo_rates: Dict[str, Decimal] = {}
    if opening:
        for item_code, layers in opening_rates.items():
            quantity = sum((qty for _rate, qty in layers), Decimal(0))
            if quantity:
                fifo_rates[item_code] = sum(
                    (rate * qty for rate, qty in layers), Decimal(0)
                ) / quantity
    else:
        item_values: Dict[str, Decimal] = defaultdict(Decimal)
        item_quantities: Dict[str, Decimal] = defaultdict(Decimal)
        fallback_rates: Dict[Tuple[str, str], Dict[str, Any]] = {}
        item_fallback_rates: Dict[str, Decimal] = {}
        for row in stock_rows:
            if int(row.get("ptype") or 0) != 9 or _as_date(row["trndate"]) > end_date:
                continue
            item_code = str(row.get("mcode") or "")
            item_fallback_rates[item_code] = (
                _decimal(row.get("itemcrate")) or _decimal(row.get("prate_a"))
            )
            if division_filter != "%" and str(row.get("division") or "") != division_filter:
                continue
            if str(row.get("vouchertype") or "") not in allowed_fifo_vouchers:
                continue
            rate = (
                _decimal(row.get("crate"))
                or _decimal(row.get("prate"))
                or _decimal(row.get("rate"))
            )
            rate_key = (str(row.get("productdivision") or row.get("division") or ""), item_code)
            previous = fallback_rates.get(rate_key)
            if rate and (previous is None or row["trndate"] > previous["date"]):
                fallback_rates[rate_key] = {"date": row["trndate"], "rate": rate}

        for (warehouse_division, item_code), stock_qty in fifo_stock.items():
            if stock_qty <= 0:
                fallback = fallback_rates.get((warehouse_division, item_code))
                rate = fallback["rate"] if fallback else item_fallback_rates.get(item_code, Decimal(0))
                item_values[item_code] += stock_qty * rate
                item_quantities[item_code] += stock_qty
                continue
            remaining = max(stock_qty, Decimal(0))
            purchases = sorted(
                fifo_purchases.get((warehouse_division, item_code), []),
                key=lambda item: (item["date"], item["voucher"]),
                reverse=True,
            )
            for index, purchase in enumerate(purchases):
                layer_qty = (
                    remaining
                    if index == len(purchases) - 1
                    else min(remaining, purchase["qty"])
                )
                if layer_qty > 0:
                    item_values[item_code] += layer_qty * purchase["rate"]
                    item_quantities[item_code] += layer_qty
                    remaining -= layer_qty
                if remaining <= 0:
                    break
            if not purchases:
                fallback = fallback_rates.get((warehouse_division, item_code))
                rate = fallback["rate"] if fallback else item_fallback_rates.get(item_code, Decimal(0))
                item_values[item_code] += stock_qty * rate
                item_quantities[item_code] += stock_qty
        for item_code, quantity in item_quantities.items():
            if quantity:
                fifo_rates[item_code] = item_values[item_code] / quantity

    stock_value = Decimal(0)
    for (item_code, _batch, _batch_id), batch_data in batches.items():
        quantity = batch_data["qty"]
        if not quantity:
            continue
        if batch_data["ptype"] == 9:
            price = fifo_rates.get(item_code, Decimal(0))
        else:
            price = batch_data["landing_cost"]
        stock_value += quantity * price

    return stock_value


def _branch_transfer_rows(
    rows: List[Dict[str, Any]], parameters: Dict[str, Any], metadata: Dict[str, Any]
) -> List[Dict[str, Any]]:
    settings = metadata.get("settings", {})
    if settings.get("enablesinglebatchmode") or not settings.get("enableacbasebranchtransfer", 1):
        return []

    divisions = {str(row.get("refbill") or "") for row in rows if row.get("vouchertype") == "TR"}
    late_posting = bool(settings.get("enablelateacposting_transferout"))
    branch_wise = bool(settings.get("enablebranchwisetfrinoutac"))
    amounts: Dict[Tuple[str, str, str, str, str], List[Decimal]] = {}
    for row in rows:
        voucher_type = str(row.get("vouchertype") or "")
        if voucher_type == "TR" and _decimal(row.get("realqty_in")) == 0:
            continue
        if voucher_type == "TO":
            if _decimal(row.get("realqty")) == 0 or (late_posting and str(row.get("vchrno")) not in divisions):
                continue
        if parameters["DIV"] != "%" and str(row.get("division") or "") != str(parameters["DIV"]):
            continue

        product_type = int(row.get("ptype") or 0)
        single_batch = bool(settings.get("enablesinglebatchmode"))
        unit_cost = (
            _decimal(row.get("rate"))
            if product_type == 9 or single_batch
            else _decimal(row.get("landingcost"))
        )
        amount = _decimal(row.get("quantity")) * unit_cost
        branch_account = (
            row.get("transferinac") if voucher_type == "TR" else row.get("transferoutac")
        ) if branch_wise else None
        if branch_account is None:
            branch_account = (
                row.get("config_transferinac") or row.get("default_transferinac")
                if voucher_type == "TR"
                else row.get("config_transferoutac") or row.get("default_transferoutac")
            )
        destination_account = row.get("destination_acid")
        primary_account = destination_account
        dr = amount if voucher_type == "TO" else Decimal(0)
        cr = amount if voucher_type == "TR" else Decimal(0)
        key = (voucher_type, str(primary_account or ""), str(branch_account or ""), str(row.get("costcenter") or ""), str(row.get("division") or ""))
        amounts.setdefault(key, [Decimal(0), Decimal(0)])
        amounts[key][0] += dr
        amounts[key][1] += cr

    result = []
    for (voucher_type, primary, counterpart, cost_center, division), (debit, credit) in amounts.items():
        result.append({
            "a_acid": primary, "dramnt": debit, "cramnt": credit,
            "sl_acid": "", "trndate": parameters["DATE2"], "vouchertype": voucher_type,
            "costcenter": cost_center, "division": division, "hassubledger": 0,
            "is_branch_transfer": True,
        })
        result.append({
            "a_acid": counterpart, "dramnt": credit, "cramnt": debit,
            "sl_acid": "", "trndate": parameters["DATE2"], "vouchertype": voucher_type,
            "costcenter": cost_center, "division": division, "hassubledger": 0,
            "is_branch_transfer": True,
        })
    return result


def build_trial_balance_report(
    tree_rows: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
    branch_rows: Optional[List[Dict[str, Any]]] = None,
    stock_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Reproduce the trial-balance aggregation and output rows in Python."""
    settings = metadata.get("settings", {})
    config = metadata.get("accounts", {})
    fiscal_begin = metadata.get("begin_date") or parameters["DATE1"]
    show_subledger = bool(parameters["SHOWSUBLEDGER"] and settings.get("enablesubledger"))
    show_net = bool(parameters["SHOWNETCLOSING"])
    date1 = _as_date(parameters["DATE1"])
    date2 = _as_date(parameters["DATE2"])
    totals: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: dict(ZERO_AMOUNTS))

    rows = list(transactions)
    rows.extend(_branch_transfer_rows(branch_rows or [], parameters, metadata))
    for row in rows:
        voucher_type = str(row.get("vouchertype") or "")
        row_date = _as_date(row["trndate"])
        if row_date > date2:
            continue
        if parameters["DIV"] != "%" and str(row.get("division") or "") != str(parameters["DIV"]):
            continue
        if parameters["COSTCENTER"] != "%" and str(row.get("costcenter") or "") != str(parameters["COSTCENTER"]):
            continue
        is_branch_transfer = bool(row.get("is_branch_transfer"))
        if parameters["SHOWOPENINGTRIALONLY"] and voucher_type not in {"AO", "OB"} and not is_branch_transfer:
            continue
        if not settings.get("enablesinglebatchmode") and voucher_type in {"TR", "TO"} and not is_branch_transfer:
            continue

        account_id = str(row.get("a_acid") or "")
        if not account_id:
            continue
        if show_subledger and bool(row.get("hassubledger")):
            subledger_id = str(row.get("sl_acid") or "") or account_id
            account_id = f"{subledger_id}_{account_id}"

        debit = _decimal(row.get("dramnt"))
        credit = _decimal(row.get("cramnt"))
        amounts = totals[account_id]
        if voucher_type in {"OB", "AO"} or row_date < date1:
            amounts["OPENING_DR"] += debit
            amounts["OPENING_CR"] += credit
        elif date1 <= row_date <= date2:
            amounts["RUNNING_DR"] += debit
            amounts["RUNNING_CR"] += credit
        amounts["CLOSING_DR"] += debit
        amounts["CLOSING_CR"] += credit

    for account_id, amounts in list(totals.items()):
        opening = _amount_pair(amounts["OPENING_DR"], amounts["OPENING_CR"], show_net)
        closing = _amount_pair(amounts["CLOSING_DR"], amounts["CLOSING_CR"], show_net)
        amounts["OPENING_DR"], amounts["OPENING_CR"] = opening
        amounts["CLOSING_DR"], amounts["CLOSING_CR"] = closing

    if not parameters["SHOWDEBTORSCREDITORSDETAILS"]:
        party_totals: Dict[str, Dict[str, Decimal]] = defaultdict(lambda: dict(ZERO_AMOUNTS))
        for row in transactions:
            account_id = str(row.get("a_acid") or "")
            if not account_id.startswith("PA") or account_id not in totals:
                continue
            config_id = "SUNDRYDEBTORS" if str(row.get("ptype") or "").upper() == "C" else "SUNDRYCREDITORS"
            target_id = str(config.get(config_id) or "")
            if target_id:
                for field in AMOUNT_FIELDS:
                    party_totals[target_id][field] += totals[account_id][field]
                del totals[account_id]
        for target_id, party_amounts in party_totals.items():
            party_amounts["OPENING_DR"], party_amounts["OPENING_CR"] = _amount_pair(
                party_amounts["OPENING_DR"], party_amounts["OPENING_CR"], show_net
            )
            party_amounts["CLOSING_DR"], party_amounts["CLOSING_CR"] = _amount_pair(
                party_amounts["CLOSING_DR"], party_amounts["CLOSING_CR"], show_net
            )
            for field in AMOUNT_FIELDS:
                totals[target_id][field] += party_amounts[field]

    opening_stock = _stock_value(stock_rows or [], parameters, metadata, opening=True)
    closing_stock = (
        _stock_value(stock_rows or [], parameters, metadata, opening=False)
        if parameters["SHOWSTOCKVALUE"] and not parameters["SHOWOPENINGTRIALONLY"]
        else Decimal(0)
    )
    stock_account = str(config.get("STOCK IN HAND") or "")
    opening_difference_account = str(config.get("ACCOUNTFORBALANCINGOPENINGBALANCE") or "")
    if stock_account:
        totals[stock_account]["OPENING_DR"] += opening_stock
        totals[stock_account]["CLOSING_DR"] += opening_stock
        if closing_stock:
            totals[stock_account]["RUNNING_CR"] += opening_stock
            totals[stock_account]["RUNNING_DR"] += closing_stock
            totals[stock_account]["CLOSING_DR"] -= opening_stock
            totals[stock_account]["CLOSING_DR"] += closing_stock
            opening_stock_account = str(config.get("OPENING STOCK") or "")
            closing_stock_account = str(config.get("CLOSING STOCK") or "")
            if opening_stock_account:
                totals[opening_stock_account]["RUNNING_DR"] += opening_stock
                totals[opening_stock_account]["CLOSING_DR"] += opening_stock
            if closing_stock_account:
                totals[closing_stock_account]["RUNNING_CR"] += closing_stock
                totals[closing_stock_account]["CLOSING_CR"] += closing_stock
    if opening_difference_account:
        totals[opening_difference_account]["OPENING_CR"] += opening_stock
        totals[opening_difference_account]["CLOSING_CR"] += opening_stock

    opening_diff = totals.get(opening_difference_account, dict(ZERO_AMOUNTS))
    difference = opening_diff["OPENING_DR"] - opening_diff["OPENING_CR"]
    diff_amounts = dict(ZERO_AMOUNTS)
    if difference > 0:
        diff_amounts["OPENING_DR"] = diff_amounts["CLOSING_DR"] = difference
    elif difference < 0:
        diff_amounts["OPENING_CR"] = diff_amounts["CLOSING_CR"] = -difference

    if opening_difference_account:
        totals[opening_difference_account] = dict(ZERO_AMOUNTS)

    tree_by_id = {str(row["ACID"]): row for row in tree_rows}
    ordered_tree = sorted(tree_rows, key=lambda row: int(row["LEVEL"]), reverse=True)
    for row in ordered_tree:
        parent = row.get("PARENT")
        if parent is None:
            continue
        parent_amounts = totals[str(parent)]
        child_amounts = totals[str(row["ACID"])]
        for field in AMOUNT_FIELDS:
            parent_amounts[field] += child_amounts[field]

    def formatted(value: Decimal) -> str:
        return f"{value:,.2f}"

    report_rows = []
    ledger_wise = bool(parameters["LEDGERWISE"])
    summary = bool(parameters["SUMMARYREPORT"])
    for row in tree_rows:
        account_id = str(row["ACID"])
        if row["ID"] == 1 or (summary and row["LEVEL"] > int(parameters["LEVELS"]) + 1):
            continue
        if not parameters["SHOWSUBLEDGER"] and row.get("MCAT") in {"SL", "SG"}:
            continue
        values = totals[account_id]
        if row["TYPE"] == "A" and not any(values[field] != 0 for field in AMOUNT_FIELDS):
            continue
        if ledger_wise and row["TYPE"] != "A":
            continue
        name = str(row["ACNAME"] or "")
        if not ledger_wise and row["TYPE"] == "G":
            name = name.upper()
        report_rows.append({
            "ACNAME": name,
            "ACCODE": row["ACCODE"],
            "OPENIGN_DR": formatted(values["OPENING_DR"]),
            "OPENING_CR": formatted(values["OPENING_CR"]),
            "RUNNING_DR": formatted(values["RUNNING_DR"]),
            "RUNNING_CR": formatted(values["RUNNING_CR"]),
            "CLOSING_DR": formatted(values["CLOSING_DR"]),
            "CLOSING_CR": formatted(values["CLOSING_CR"]),
            "ACID": account_id,
            "TYPE": row["TYPE"],
            "LEVEL": row["LEVEL"],
            "ID": row["ID"],
            "FFLG": "B" if row["TYPE"] == "G" else "R",
        })

    report_rows.append({
        "ACNAME": None, "ACCODE": None, "OPENIGN_DR": None, "OPENING_CR": None,
        "RUNNING_DR": None, "RUNNING_CR": None, "CLOSING_DR": None,
        "CLOSING_CR": None, "ACID": None, "TYPE": "A", "LEVEL": 999,
        "ID": 99997, "FFLG": "R",
    })
    difference_name = tree_by_id.get(opening_difference_account, {}).get("ACNAME")
    if any(diff_amounts[field] for field in AMOUNT_FIELDS) or difference_name is None:
        report_rows.append({
            "ACNAME": difference_name,
            "ACCODE": "",
            "OPENIGN_DR": formatted(diff_amounts["OPENING_DR"]),
            "OPENING_CR": formatted(diff_amounts["OPENING_CR"]),
            "RUNNING_DR": formatted(diff_amounts["RUNNING_DR"]),
            "RUNNING_CR": formatted(diff_amounts["RUNNING_CR"]),
            "CLOSING_DR": formatted(diff_amounts["CLOSING_DR"]),
            "CLOSING_CR": formatted(diff_amounts["CLOSING_CR"]),
            "ACID": opening_difference_account,
            "TYPE": "A",
            "LEVEL": 1000,
            "ID": 99998,
            "FFLG": "R",
        })
    return report_rows


def build_consolidated_trial_balance_report(
    tree_rows: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    parameters: Dict[str, Any],
    metadata: Dict[str, Any],
    divisions: List[Dict[str, Any]],
    branch_rows: Optional[List[Dict[str, Any]]] = None,
    stock_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build the consolidated report's division-specific closing columns."""
    report_parameters = {
        "DATE1": parameters["DATE1"],
        "DATE2": parameters["DATE2"],
        "DIV": "%",
        "CID": parameters["CID"],
        "COSTCENTER": parameters["COSTCENTER"],
        "PHISCALID": parameters["PHISCALID"],
        "LEDGERWISE": parameters["LEDGERWISE"],
        "SUMMARYREPORT": 0,
        "LEVELS": 2,
        "SHOWOPENINGTRIALONLY": parameters["SHOWOPENINGTRIALONLY"],
        "SHOWSUBLEDGER": parameters["SHOWSUBLEDGER"],
        "SHOWDEBTORSCREDITORSDETAILS": parameters["SHOWDEBTORSCREDITORSDETAILS"],
        "SHOWSTOCKVALUE": parameters["SHOWSTOCKVALUE"],
        "SHOWCLOSINGONPY": 0,
        "SHOWZEROBL": 0,
        "SHOWNETCLOSING": parameters["SHOWNETCLOSING"],
        "NODEACID": "AL",
        "NODEACNAME": "ACCOUNT",
    }
    ordered_divisions = sorted(divisions, key=lambda item: str(item["name"]).casefold())
    per_division: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for division in ordered_divisions:
        division_id = str(division["initial"])
        division_parameters = {**report_parameters, "DIV": division_id}
        result = build_trial_balance_report(
            tree_rows,
            transactions,
            division_parameters,
            metadata,
            branch_rows,
            stock_rows,
        )
        per_division[division_id] = {
            str(row["ACID"]): row for row in result if row.get("ACID") is not None
        }

    account_ids = set()
    for rows_by_account in per_division.values():
        account_ids.update(rows_by_account)

    tree_by_id = {str(row["ACID"]): row for row in tree_rows}
    result = []
    for account_id in account_ids:
        source_rows = [
            rows_by_account.get(account_id)
            for rows_by_account in per_division.values()
        ]
        source_rows = [row for row in source_rows if row is not None]
        if not source_rows:
            continue
        is_difference = account_id not in tree_by_id or any(
            row.get("ID") == 99998 for row in source_rows
        )
        division_amounts: Dict[str, Tuple[Decimal, Decimal]] = {}
        total_debit = Decimal(0)
        total_credit = Decimal(0)
        for division in ordered_divisions:
            division_id = str(division["initial"])
            row = per_division.get(division_id, {}).get(account_id)
            debit = _decimal(row.get("CLOSING_DR")) if row else Decimal(0)
            credit = _decimal(row.get("CLOSING_CR")) if row else Decimal(0)
            division_amounts[division_id] = (debit, credit)
            total_debit += debit
            total_credit += credit
        if total_debit + total_credit == 0:
            continue

        template = source_rows[0]
        record = {
            "ID": 9999999 if is_difference else template["ID"],
            "ACNAME": "DIFFERENCE IN OPENING B/L ......." if is_difference else tree_by_id[account_id]["ACNAME"],
            "ACID": account_id,
            "FFLG": "BR" if is_difference else template["FFLG"],
            "X_CLOSING DR": f"{total_debit:,.2f}",
            "X_CLOSING CR": f"{total_credit:,.2f}",
        }
        for division in ordered_divisions:
            division_id = str(division["initial"])
            division_name = str(division["name"])
            debit, credit = division_amounts[division_id]
            record[f"{division_name}~DR AMOUNT"] = f"{debit:,.2f}"
            record[f"{division_name}~CR AMOUNT"] = f"{credit:,.2f}"
        record["CLOSING BL ~ DR AMOUNT"] = f"{total_debit:,.2f}"
        record["CLOSING BL ~ CR AMOUNT"] = f"{total_credit:,.2f}"
        result.append(record)

    return sorted(result, key=lambda row: int(row["ID"]))