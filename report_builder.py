from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, Iterable, List


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