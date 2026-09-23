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
