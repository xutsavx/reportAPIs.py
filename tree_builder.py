"""
Flat rows -> nested tree, in two linear passes. No recursion, so it
doesn't matter whether the data is 10 levels deep or 1000 - cost is
purely O(n) in row count.
"""
from typing import List, Tuple, Dict, Any, Optional


def build_tree(rows: List[Tuple[str, str, Optional[str]]]):
    # Pass 1: create every node once.
    nodes: Dict[str, Dict[str, Any]] = {
        acid: {"acid": acid, "acname": acname, "children": []}
        for acid, acname, _parent in rows
    }

    # Pass 2: link each node under its parent (or treat as root).
    roots = []
    for acid, _acname, parent in rows:
        node = nodes[acid]
        # Guards against NULL parents, self-referencing rows, and
        # dangling parents (parent id that doesn't exist) - all get
        # treated as roots instead of crashing the build.
        if parent is None or parent == acid or parent not in nodes:
            roots.append(node)
        else:
            nodes[parent]["children"].append(node)

    return roots, nodes


def build_account_tree_rows(
    account_rows: List[Dict[str, Any]],
    subledger_rows: List[Dict[str, Any]],
    root_acid: str = "AL",
    root_name: str = "ACCOUNT",
    show_debtors_creditors: bool = False,
    show_subledger: bool = False,
    is_for_party_account: bool = False,
    account_config: Optional[Dict[str, Any]] = None,
    group_only: bool = False,
) -> List[Dict[str, Any]]:
    """Expand account rows into the report's ordered, flat tree shape."""
    accounts = {str(row["acid"]): row for row in account_rows}
    account_config = account_config or {}
    party_accounts = {
        acid: row for acid, row in accounts.items() if acid.startswith("PA")
    }
    regular_accounts = {
        acid: row for acid, row in accounts.items() if not acid.startswith("PA")
    }
    children_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    nodes_by_id: Dict[str, Dict[str, Any]] = {}
    special_nodes: Dict[str, Dict[str, Any]] = {}

    if is_for_party_account:
        included = party_accounts
    else:
        included = regular_accounts

    def add_node(
        acid: str,
        row: Optional[Dict[str, Any]],
        parent: str,
        acname: Optional[str] = None,
        accode: Optional[str] = None,
        node_type: Optional[str] = None,
        mcat: Optional[str] = None,
        sort_order: Optional[int] = None,
    ) -> None:
        source = row or {}
        node = {
            "acid": acid,
            "parent": parent,
            "type": node_type or str(source.get("type") or "A"),
            "accode": accode if accode is not None else source.get("accode") or "",
            "acname": acname if acname is not None else source.get("acname") or "",
            "mcat": mcat if mcat is not None else source.get("mcat") or "",
            "account_order": (
                sort_order
                if sort_order is not None
                else int(source.get("account_order") or 1000)
            ),
        }
        children_by_parent.setdefault(parent, []).append(node)
        nodes_by_id[acid] = node

    for acid, row in included.items():
        parent = str(row.get("parent") or "")
        valid_parents = included
        parent_is_special = parent in {"BS", "PL", "PA"}
        node_type = (
            "G" if show_subledger and bool(row.get("hassubledger"))
            else str(row.get("type") or "A")
        )
        if group_only and node_type != "G":
            continue
        if parent in valid_parents or parent_is_special:
            add_node(acid, row, parent, node_type=node_type)
        elif parent:
            add_node(acid, row, "XX", node_type=node_type)

    if not is_for_party_account and root_acid == "AL":
        special_nodes["BS"] = {
            "acid": "BS", "parent": root_acid, "type": "G", "accode": "AL",
            "acname": "BALANCE SHEET", "mcat": "", "account_order": 1000,
        }
        special_nodes["PL"] = {
            "acid": "PL", "parent": root_acid, "type": "G", "accode": "PL",
            "acname": "PROFIT AND LOSS", "mcat": "", "account_order": 1000,
        }
        for acid, row in party_accounts.items():
            if show_debtors_creditors and row.get("type") == "A":
                config_key = "SUNDRYDEBTORS" if str(row.get("ptype") or "").upper() == "C" else "SUNDRYCREDITORS"
                parent_acid = str(account_config.get(config_key) or "")
                if not parent_acid:
                    parent_acid = str(row.get("parent") or "XX")
                if not group_only:
                    add_node(acid, row, parent_acid)
    elif is_for_party_account and root_acid == "PX":
        special_nodes["PA"] = {
            "acid": "PA", "parent": root_acid, "type": "G", "accode": "PX",
            "acname": "PARTY ACCOUNT", "mcat": "", "account_order": 1,
        }

    for node in special_nodes.values():
        if node is not None:
            children_by_parent.setdefault(node["parent"], []).append(node)

    if show_subledger and not is_for_party_account:
        for row in subledger_rows:
            account_id = str(row.get("a_acid") or "")
            if account_id not in regular_accounts:
                continue
            subledger_id = str(row.get("sl_acid") or "")
            if subledger_id:
                node_id = f"{subledger_id}_{account_id}"
                name = row.get("sl_acname") or ""
                code = subledger_id
            elif bool(row.get("hassubledger")):
                node_id = f"{account_id}_{account_id}"
                name = row.get("acname") or ""
                code = row.get("accode") or ""
            else:
                continue
            if not group_only:
                add_node(node_id, None, account_id, name, code, "A")
            regular_accounts[account_id]["type"] = "G"
            account_node = nodes_by_id.get(account_id)
            if account_node is not None:
                account_node["type"] = "G"

    orphan_ids = {
        node["acid"]
        for parent, nodes in children_by_parent.items()
        if parent == "XX"
        for node in nodes
    }
    if orphan_ids:
        unknown = {
            "acid": "XX", "parent": root_acid, "type": "G", "accode": "",
            "acname": "UNKNOWN A/C GROUP", "mcat": "", "account_order": 3,
        }
        children_by_parent.setdefault(root_acid, []).append(unknown)

    # Match the SQL reset routine's grouping markers without modifying RMD_ACLIST.
    if not show_subledger and not is_for_party_account:
        for acid, row in regular_accounts.items():
            if not bool(row.get("hassubledger")) or str(row.get("type")) != "G":
                continue
            pending = [acid]
            seen = set()
            while pending:
                parent = pending.pop()
                for child in children_by_parent.get(parent, []):
                    child_id = child["acid"]
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    child["mcat"] = "SG" if child["type"] == "G" else "SL"
                    pending.append(child_id)

    def sort_key(node: Dict[str, Any]) -> Tuple[int, int, str]:
        order = node["account_order"] if not show_subledger else 0
        type_order = 0 if str(node["type"]) == "G" else 1
        return (-order, type_order, str(node["acname"]).casefold())

    for children in children_by_parent.values():
        children.sort(key=sort_key)

    root = {
        "acid": root_acid,
        "parent": None,
        "type": "G",
        "accode": "0",
        "acname": root_name,
        "mcat": "",
    }
    flat_rows: List[Dict[str, Any]] = []
    stack = [(root, 1, 0)]
    while stack:
        node, level, child_index = stack.pop()
        if child_index == 0:
            display_name = node["acname"] if level == 1 else (" " * ((level - 2) * 6 + 2)) + node["acname"]
            flat_rows.append({
                "ID": len(flat_rows) + 1,
                "ACID": node["acid"],
                "PARENT": node["parent"],
                "TYPE": node["type"],
                "LEVEL": level,
                "ACCODE": node["accode"],
                "ACNAME": display_name,
                "MCAT": node["mcat"],
            })
        children = children_by_parent.get(node["acid"], [])
        if child_index < len(children):
            stack.append((node, level, child_index + 1))
            stack.append((children[child_index], level + 1, 0))

    return flat_rows
