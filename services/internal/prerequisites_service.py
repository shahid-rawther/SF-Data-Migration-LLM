"""Work out which Salesforce objects have to be loaded before a given one.

``compat_engine.resolve_dependency_order`` orders the mappings already chosen
for a run. This module answers the earlier question — "what does this object
depend on?" — by walking Salesforce's own reference graph upwards from the
object being mapped, so the answer holds before any of those parents have been
added to the run.

Edges come from the destination describe: a field of type ``reference`` names
the objects it can point at, and a record cannot carry that lookup until a
record exists on the other end. Only createable fields count, since a field the
API will not let us set is not something the migration has to satisfy.

The hard part is relevance, not reachability. Follow every lookup and the walk
reaches most of the org, so an object is treated as a prerequisite only where
there is evidence it belongs in the migration:

    the object is already in the run, or some mapping populates it

A config that reads tables without writing to an object is not evidence, so
only mappings with a destination object count. Applying the same rule as the
walk ascends is what keeps the hierarchy finite and worth reading.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field as dataclass_field
from typing import Iterable

from services.internal import schema_cache
from services.internal.mappings_service import (
    index_by_destination_object,
    mapped_destination_fields,
)

DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_NODES = 25
MAX_NEEDED_BY = 6
MAX_CANDIDATES = 8

STATUS_SELECTED = "selected"
STATUS_MAPPING_EXISTS = "mapping_exists"
STATUS_NEEDS_MAPPING = "needs_mapping"


@dataclass
class PrerequisiteEdge:
    """A lookup field that makes one object depend on another."""

    from_object: str
    to_object: str
    field: str
    mapped: bool = False
    required: bool = False
    # The lookup accepts more than one object, so the parent is a choice.
    polymorphic: bool = False


@dataclass
class PrerequisiteNode:
    object: str
    depth: int
    status: str
    selected: bool = False
    is_target: bool = False
    mapping_names: list[str] = dataclass_field(default_factory=list)
    source_tables: list[str] = dataclass_field(default_factory=list)
    self_reference_fields: list[str] = dataclass_field(default_factory=list)
    schema_unavailable: bool = False
    # A sample of the objects that need this one, and through which field.
    needed_by: list[dict] = dataclass_field(default_factory=list)
    needed_by_count: int = 0
    # True when something depends on it through a field Salesforce requires.
    required_by_someone: bool = False


def dependency_map(
    objects: list[str], pairs: Iterable[tuple[str, str]]
) -> dict[str, set[str]]:
    """``child -> parents`` for the pairs whose both ends are in ``objects``.

    Self-edges are dropped: an object cannot be loaded before itself, so a
    self-reference is a field-level concern rather than an ordering one.
    """
    present = set(objects)
    dependencies: dict[str, set[str]] = {o: set() for o in objects}
    for child, parent in pairs:
        if child in present and parent in present and child != parent:
            dependencies[child].add(parent)
    return dependencies


def condense(
    objects: list[str], pairs: Iterable[tuple[str, str]]
) -> list[list[str]]:
    """Group the graph into strongly connected components, parents first.

    Mutually referencing objects are normal in Salesforce — an account has an
    owner and a user has an account — so instead of failing, the graph is
    condensed. The components sort cleanly even when their members do not, and
    each multi-object component tells the caller which objects have to go
    together.

    Tarjan emits a component only after every component it depends on, which is
    exactly parent-first order. ``pairs`` are ``(child, parent)``.
    """
    dependencies = dependency_map(objects, pairs)

    counter = 0
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(root: str) -> None:
        nonlocal counter
        # Explicit stack: recursion depth would otherwise track graph depth.
        work = [(root, iter(sorted(dependencies.get(root, set()))))]
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = lowlink[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(dependencies.get(child, set())))))
                    advanced = True
                    break
                if child in on_stack:
                    lowlink[node] = min(lowlink[node], index[child])
            if advanced:
                continue

            work.pop()
            if lowlink[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(sorted(component))
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

    for obj in sorted(objects):
        if obj not in index:
            visit(obj)

    return components


def ordered_components(
    objects: list[str], pairs: Iterable[tuple[str, str]]
) -> tuple[list[str], list[list[str]]]:
    """Parent-first order over ``objects``, plus the groups that cycle."""
    components = condense(objects, pairs)
    order = [obj for component in components for obj in component]
    cycles = [c for c in components if len(c) > 1]
    return order, cycles


def build_prerequisites(
    destination_object: str,
    destination_credentials: dict,
    *,
    source_type: str | None = None,
    selected_objects: list[str] | None = None,
    draft_config: dict | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_nodes: int = DEFAULT_MAX_NODES,
) -> dict:
    """Return the Salesforce prerequisite hierarchy for one destination object.

    ``draft_config`` is the mapping the user is editing right now; the fields it
    populates take precedence over anything stored, so the answer reflects the
    screen rather than the database.
    """
    empty = {
        "object": destination_object,
        "order": [],
        "nodes": [],
        "edges": [],
        "missing_objects": [],
        "ready": True,
        "cycles": [],
        "required_gaps": [],
        "unresolved_references": [],
        "other_references": [],
        "truncated": False,
        "truncation_reason": "",
        "max_depth": max_depth,
    }
    if not destination_object:
        return empty

    selected = {o for o in (selected_objects or []) if o}
    mapping_index = index_by_destination_object(source_type)

    def populated_fields(name: str) -> set[str]:
        if name == destination_object and draft_config is not None:
            return mapped_destination_fields(draft_config)
        merged: set[str] = set()
        for entry in mapping_index.get(name, []):
            merged |= entry["mapped_destination_fields"]
        return merged

    nodes: dict[str, PrerequisiteNode] = {}
    edges: list[PrerequisiteEdge] = []
    required_gaps: list[dict] = []
    unresolved: list[dict] = []
    other_references: list[dict] = []
    visited: set[str] = set()
    truncated = False
    truncation_reason = ""

    frontier = [destination_object]
    depth = 0

    while frontier:
        if depth > max_depth:
            truncated = True
            truncation_reason = f"stopped after {max_depth} levels"
            break

        described = schema_cache.get_many_destination_object_fields(
            frontier, destination_credentials
        )
        next_frontier: list[str] = []

        for current in frontier:
            if current in visited:
                continue
            visited.add(current)

            is_target = current == destination_object
            entries = mapping_index.get(current, [])
            fields = described.get(current, [])
            nodes[current] = PrerequisiteNode(
                object=current,
                depth=depth,
                status=(
                    STATUS_SELECTED
                    if current in selected
                    else STATUS_MAPPING_EXISTS
                    if entries
                    else STATUS_NEEDS_MAPPING
                ),
                selected=current in selected,
                is_target=is_target,
                mapping_names=[e["name"] for e in entries],
                source_tables=list(
                    dict.fromkeys(t for e in entries for t in e["source_tables"])
                ),
                schema_unavailable=not fields,
            )
            node = nodes[current]
            populated = populated_fields(current)

            for meta in fields:
                if meta.get("type") != "reference":
                    continue
                # A field the API will not let us set is not a constraint on
                # the migration, whatever it points at.
                if not meta.get("createable"):
                    continue
                targets = [t for t in (meta.get("reference_to") or []) if t]
                if not targets:
                    continue

                field_name = meta.get("name", "")
                is_mapped = field_name in populated
                is_required = bool(meta.get("required"))
                polymorphic = len(targets) > 1

                if current in targets and field_name not in node.self_reference_fields:
                    node.self_reference_fields.append(field_name)

                # A lookup is judged once, not once per candidate object. Some
                # Salesforce fields accept scores of objects, and one usable
                # target satisfies the field — reporting the rest as gaps would
                # bury the real findings.
                others = [t for t in targets if t != current]
                resolvable = [
                    t for t in others if t in selected or mapping_index.get(t)
                ]

                for parent in resolvable:
                    edges.append(
                        PrerequisiteEdge(
                            from_object=current,
                            to_object=parent,
                            field=field_name,
                            mapped=is_mapped,
                            required=is_required,
                            polymorphic=polymorphic,
                        )
                    )
                    if parent not in visited and parent not in next_frontier:
                        next_frontier.append(parent)

                if resolvable or current in targets or not others:
                    continue

                gap = {
                    "object": current,
                    "field": field_name,
                    "candidates": others[:MAX_CANDIDATES],
                    "candidate_count": len(others),
                    "polymorphic": polymorphic,
                }
                if is_required:
                    required_gaps.append(gap)
                elif is_mapped:
                    unresolved.append(gap)
                else:
                    other_references.append(gap)

        room = max(max_nodes - len(visited), 0)
        if len(next_frontier) > room:
            next_frontier = next_frontier[:room]
            truncated = True
            truncation_reason = f"stopped after {max_nodes} objects"

        frontier = next_frontier
        depth += 1

    order, cycles = ordered_components(
        list(nodes), [(e.from_object, e.to_object) for e in edges]
    )

    for edge in edges:
        parent = nodes.get(edge.to_object)
        if not parent:
            continue
        parent.needed_by_count += 1
        parent.required_by_someone = parent.required_by_someone or edge.required
        if len(parent.needed_by) < MAX_NEEDED_BY:
            parent.needed_by.append(
                {
                    "object": edge.from_object,
                    "field": edge.field,
                    "mapped": edge.mapped,
                    "required": edge.required,
                }
            )

    ordered_nodes = [asdict(nodes[name]) for name in order if name in nodes]
    missing = [
        n["object"]
        for n in ordered_nodes
        if not n["is_target"] and n["status"] != STATUS_SELECTED
    ]

    return {
        "object": destination_object,
        "order": order,
        "nodes": ordered_nodes,
        "edges": [asdict(e) for e in edges],
        "missing_objects": missing,
        "ready": not missing,
        "cycles": cycles,
        "required_gaps": required_gaps,
        "unresolved_references": unresolved,
        "other_references": other_references,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "max_depth": max_depth,
    }
