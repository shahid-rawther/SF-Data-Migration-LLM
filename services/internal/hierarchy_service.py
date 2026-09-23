"""The Salesforce ancestor graph for a whole selection, drawn top to bottom.

``prerequisites_service`` answers "what must load before this one object?" for
the mapping being edited, and deliberately only follows lookups whose target is
already in the run — that keeps the answer short enough to read inside a card.

This module answers the earlier, wider question: given every Salesforce object a
migration intends to write, what does the full chain of parents above them look
like? So it follows every createable lookup upward whether or not anything maps
the object on the other end, because an unmapped parent is precisely what the
person needs to see before they start.

Only ancestors are walked. A lookup makes the object that carries it depend on
the object it points at, so the arrow runs child to parent, and the walk moves in
that direction only. An object that references a selected object is a child of
it and is none of this graph's business: selecting Account pulls in the Groups
and Users an Account points at, not the Incidents that point at Account.

Two things stop the walk from swallowing the org. Depth and node caps bound it
outright, and a lookup that accepts more objects than
``POLYMORPHIC_EXPAND_LIMIT`` is not expanded into all of them — a field that
takes fifty objects names a choice, not fifty parents, so only the candidates
already in scope are followed and the rest are reported as a choice to make.

Being above the selection is not the same as being needed by it, and the
difference is most of the graph. Three tiers come out of the walk:

* **required** — the lookup is one Salesforce will not accept a record without,
  so the parent has to exist whatever the mappings say;
* **needed** — required, or a lookup some mapping actually fills, so this
  migration as configured cannot resolve it without the parent;
* the remainder, reachable only through lookups that are neither, which load
  perfectly well left empty and can be filled later or never.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field as dataclass_field

from common.logger import logger
from services.internal import schema_cache
from services.internal.mappings_service import index_by_destination_object
from services.internal.prerequisites_service import condense, dependency_map

DEFAULT_MAX_DEPTH = 4
DEFAULT_MAX_NODES = 60
# A lookup accepting more targets than this names a choice rather than a set of
# parents, so it is not expanded blindly.
POLYMORPHIC_EXPAND_LIMIT = 4
MAX_CANDIDATES = 8
MAX_RELATIONS = 8


@dataclass
class HierarchyEdge:
    """A lookup field that makes ``child`` depend on ``parent``."""

    child: str
    parent: str
    field: str
    required: bool = False
    # A mapping config fills this lookup, so the migration intends to resolve it
    # and the parent's ids have to exist by the time the child loads.
    mapped: bool = False
    # The lookup accepts more than one object, so this parent is one option.
    polymorphic: bool = False


@dataclass
class HierarchyNode:
    object: str
    # Distance from the top of the graph: 0 has no parents, and every edge runs
    # from a lower level to a higher one, so parents always sit above children.
    level: int = 0
    selected: bool = False
    has_mapping: bool = False
    mapping_names: list[str] = dataclass_field(default_factory=list)
    source_tables: list[str] = dataclass_field(default_factory=list)
    self_reference_fields: list[str] = dataclass_field(default_factory=list)
    schema_unavailable: bool = False
    # What this object points at, and what points at it, for drawing the graph.
    depends_on: list[dict] = dataclass_field(default_factory=list)
    needed_by: list[dict] = dataclass_field(default_factory=list)
    depends_on_count: int = 0
    needed_by_count: int = 0
    # Something depends on it through a field Salesforce will not let us omit.
    required_by_someone: bool = False
    # Reachable from the selection through lookups Salesforce will not let the
    # load omit, so it has to exist whatever the mappings say.
    required_chain: bool = False
    # Reachable through lookups that are either required or actually mapped, so
    # this migration as configured cannot load the selection without it. The rest
    # of the graph is merely above the selection.
    needed: bool = False
    # Part of a group of objects that reference each other, so no single
    # position in the order is correct for it on its own.
    in_cycle: bool = False


def _levels(
    nodes: list[str], edges: list[HierarchyEdge]
) -> tuple[dict[str, int], list[list[str]], list[list[str]]]:
    """Rank every object so that a parent is always above its children.

    Levels are computed over the condensed graph rather than the raw one: a
    cycle has no internal ranking, but the group it forms does, and its members
    share a level so the drawing stays acyclic top to bottom.
    """
    components = condense(nodes, [(e.child, e.parent) for e in edges])
    dependencies = dependency_map(nodes, [(e.child, e.parent) for e in edges])

    component_of = {
        member: index for index, comp in enumerate(components) for member in comp
    }
    # Components come back parent-first, so one forward pass settles every
    # level: everything a component depends on has already been ranked.
    component_level: list[int] = [0] * len(components)
    for index, component in enumerate(components):
        members = set(component)
        parent_levels = [
            component_level[component_of[parent]]
            for member in component
            for parent in dependencies.get(member, ())
            if parent not in members
        ]
        component_level[index] = 1 + max(parent_levels) if parent_levels else 0

    level_of = {
        member: component_level[index]
        for index, comp in enumerate(components)
        for member in comp
    }
    depth = max(level_of.values(), default=0)
    levels: list[list[str]] = [[] for _ in range(depth + 1)]
    for name in sorted(level_of, key=lambda n: (level_of[n], n)):
        levels[level_of[name]].append(name)

    cycles = [sorted(c) for c in components if len(c) > 1]
    return level_of, levels, cycles


def _reachable_from(roots: list[str], edges: list[HierarchyEdge], keep) -> set[str]:
    """Objects reachable from the selection over the edges ``keep`` accepts.

    Most of an ancestor graph is optional: a lookup that is neither required nor
    mapped puts an object above the selection without making it a precondition
    of loading anything. Restricting the walk to the edges that do constrain the
    load is what stops a graph of thirty objects from reading as thirty
    obligations.
    """
    parents: dict[str, set[str]] = {}
    for edge in edges:
        if edge.child != edge.parent and keep(edge):
            parents.setdefault(edge.child, set()).add(edge.parent)

    reached: set[str] = set()
    queue = list(roots)
    while queue:
        current = queue.pop()
        for parent in parents.get(current, ()):
            if parent not in reached:
                reached.add(parent)
                queue.append(parent)
    return reached


def build_hierarchy(
    destination_objects: list[str],
    destination_credentials: dict,
    *,
    source_type: str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    """Walk upward from every selected object and return the ancestor graph."""
    roots = list(dict.fromkeys(o for o in (destination_objects or []) if o))
    empty = {
        "selected_objects": roots,
        "nodes": [],
        "edges": [],
        "levels": [],
        "order": [],
        "cycles": [],
        "required_objects": [],
        "needed_objects": [],
        "required_gaps": [],
        "polymorphic_choices": [],
        "objects_without_mapping": [],
        "truncated": False,
        "truncation_reason": "",
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    }
    if not roots:
        return empty

    mapping_index = index_by_destination_object(source_type)
    root_set = set(roots)

    def populated(name: str) -> set[str]:
        """The destination fields some mapping actually writes to."""
        merged: set[str] = set()
        for entry in mapping_index.get(name, []):
            merged |= entry["mapped_destination_fields"]
        return merged

    nodes: dict[str, HierarchyNode] = {}
    edges: list[HierarchyEdge] = []
    required_gaps: list[dict] = []
    polymorphic_choices: list[dict] = []
    visited: set[str] = set()
    truncated = False
    truncation_reason = ""

    frontier = list(roots)
    depth = 0

    while frontier:
        if depth > max_depth:
            truncated = True
            truncation_reason = (
                f"stopped {max_depth} levels above the selected objects"
            )
            break

        described = schema_cache.get_many_destination_object_fields(
            frontier, destination_credentials
        )
        next_frontier: list[str] = []

        for current in frontier:
            if current in visited:
                continue
            visited.add(current)

            entries = mapping_index.get(current, [])
            fields = described.get(current, [])
            node = HierarchyNode(
                object=current,
                selected=current in root_set,
                has_mapping=bool(entries),
                mapping_names=[e["name"] for e in entries],
                source_tables=list(
                    dict.fromkeys(t for e in entries for t in e["source_tables"])
                ),
                schema_unavailable=not fields,
            )
            nodes[current] = node
            filled = populated(current)

            for meta in fields:
                if meta.get("type") != "reference":
                    continue
                # A field the API will not let us set cannot constrain the load
                # order, whatever it points at.
                if not meta.get("createable"):
                    continue
                targets = [t for t in (meta.get("reference_to") or []) if t]
                if not targets:
                    continue

                field_name = meta.get("name", "")
                is_required = bool(meta.get("required"))
                is_mapped = field_name in filled
                polymorphic = len(targets) > 1

                if current in targets and field_name not in node.self_reference_fields:
                    node.self_reference_fields.append(field_name)

                others = [t for t in targets if t != current]
                if not others:
                    continue

                if len(others) > POLYMORPHIC_EXPAND_LIMIT:
                    # One usable target satisfies the field, so following all of
                    # them would bury the real parents. Only what is already in
                    # scope is walked; the choice itself is reported.
                    parents = [
                        t for t in others if t in root_set or mapping_index.get(t)
                    ]
                    if not parents:
                        polymorphic_choices.append(
                            {
                                "object": current,
                                "field": field_name,
                                "required": is_required,
                                "candidates": others[:MAX_CANDIDATES],
                                "candidate_count": len(others),
                            }
                        )
                else:
                    parents = others

                for parent in parents:
                    edges.append(
                        HierarchyEdge(
                            child=current,
                            parent=parent,
                            field=field_name,
                            required=is_required,
                            mapped=is_mapped,
                            polymorphic=polymorphic,
                        )
                    )
                    if parent not in visited and parent not in next_frontier:
                        next_frontier.append(parent)

                if is_required and not parents:
                    required_gaps.append(
                        {
                            "object": current,
                            "field": field_name,
                            "candidates": others[:MAX_CANDIDATES],
                            "candidate_count": len(others),
                            "polymorphic": polymorphic,
                        }
                    )

        room = max(max_nodes - len(visited), 0)
        if len(next_frontier) > room:
            # Parents the load genuinely depends on are kept over merely
            # reachable ones, so a truncated graph still shows what would block.
            constraining = {e.parent for e in edges if e.required or e.mapped}
            next_frontier.sort(key=lambda name: name not in constraining)
            next_frontier = next_frontier[:room]
            truncated = True
            truncation_reason = f"stopped after {max_nodes} objects"

        frontier = next_frontier
        depth += 1

    # Edges pointing at objects the caps kept out would draw arrows to nothing.
    edges = [e for e in edges if e.child in nodes and e.parent in nodes]

    level_of, levels, cycles = _levels(list(nodes), edges)
    cyclic = {member for group in cycles for member in group}
    required_chain = _reachable_from(roots, edges, lambda e: e.required)
    needed = _reachable_from(roots, edges, lambda e: e.required or e.mapped)
    for name, node in nodes.items():
        node.level = level_of.get(name, 0)
        node.in_cycle = name in cyclic
        node.required_chain = name in required_chain
        node.needed = name in needed

    for edge in edges:
        child, parent = nodes.get(edge.child), nodes.get(edge.parent)
        if child:
            child.depends_on_count += 1
            if len(child.depends_on) < MAX_RELATIONS:
                child.depends_on.append(
                    {
                        "object": edge.parent,
                        "field": edge.field,
                        "required": edge.required,
                        "mapped": edge.mapped,
                        "polymorphic": edge.polymorphic,
                    }
                )
        if parent:
            parent.needed_by_count += 1
            parent.required_by_someone = parent.required_by_someone or edge.required
            if len(parent.needed_by) < MAX_RELATIONS:
                parent.needed_by.append(
                    {
                        "object": edge.child,
                        "field": edge.field,
                        "required": edge.required,
                        "mapped": edge.mapped,
                    }
                )

    order = [name for component in condense(
        list(nodes), [(e.child, e.parent) for e in edges]
    ) for name in component]

    logger.info(
        "[hierarchy] %d selected object(s) -> %d ancestor(s) across %d level(s)%s",
        len(roots),
        len(nodes) - len(roots),
        len(levels),
        " (truncated)" if truncated else "",
    )

    return {
        "selected_objects": roots,
        "nodes": [asdict(nodes[name]) for name in order if name in nodes],
        "edges": [asdict(e) for e in edges],
        "levels": levels,
        "order": order,
        "cycles": cycles,
        # Ancestors Salesforce will not let the load skip, in the same order.
        "required_objects": [name for name in order if name in required_chain],
        # Ancestors this migration cannot load the selection without, because a
        # lookup is either required or mapped. A superset of required_objects.
        "needed_objects": [name for name in order if name in needed],
        "required_gaps": required_gaps,
        "polymorphic_choices": polymorphic_choices,
        "objects_without_mapping": [
            name for name in order if not nodes[name].has_mapping
        ],
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
    }
