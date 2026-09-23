"""The launch gate.

Two failures used to be discoverable only after a load had already run: a lookup
field carrying a raw source id, which fails every row with MALFORMED_ID, and a
required lookup with no value and no parent in the selection. Both are visible
from schema metadata alone, so this refuses the run instead.

It is deliberately deterministic and never calls a model — a migration must not
be blocked or unblocked by whether the agent was reachable — and it reports the
parent-first order it computed so the caller does not have to describe the
destination objects a second time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from common.logger import logger

from agents import detector
from agents.config import AgentSettings, load_settings
from agents.context import MigrationContext


@dataclass
class PreflightResult:
    ok: bool = True
    enforced: bool = True
    blockers: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    has_cycle: bool = False
    checked: bool = False
    note: str = ""
    # Lookups this run will deliberately load empty because no order resolves
    # them, each to be offered as a backfill afterwards. Told up front so the
    # deferral is expected rather than discovered in the final report.
    deferred: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "enforced": self.enforced,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "order": self.order,
            "has_cycle": self.has_cycle,
            "checked": self.checked,
            "note": self.note,
            "deferred": self.deferred,
            "deferred_notice": self.deferred_notice(),
        }

    def deferred_notice(self) -> str:
        """What to tell someone before they launch, or empty if nothing applies."""
        if not self.deferred:
            return ""
        fields = ", ".join(
            f"{d['destination_object']}.{d['field']}" for d in self.deferred[:4]
        )
        more = f" and {len(self.deferred) - 4} more" if len(self.deferred) > 4 else ""
        notice = (
            f"{len(self.deferred)} lookup{'' if len(self.deferred) == 1 else 's'} "
            f"({fields}{more}) cannot be filled while the records are created, "
            "because the objects involved reference each other. Those records will "
            "load with the field empty, and you will be asked to approve a second "
            "pass that attaches them once everything is in."
        )
        # An awaited mapping outside the selection changes what "once everything
        # is in" means: the second pass cannot run at all until it is added.
        awaited = sorted(
            {
                name
                for entry in self.deferred
                for name in entry.get("awaiting") or []
            }
        )
        if awaited:
            notice += (
                f" {', '.join(awaited)} {'is' if len(awaited) == 1 else 'are'} not "
                "in this migration, so those references stay empty until you add "
                f"{'it' if len(awaited) == 1 else 'them'} to the session and run "
                "again."
            )
        return notice

    def message(self) -> str:
        lines = [
            f"{len(self.blockers)} unresolved relationship problem(s) would fail this "
            "migration:"
        ]
        for blocker in self.blockers:
            where = blocker.get("mapping", "")
            if blocker.get("field"):
                where = f"{where}.{blocker['field']}"
            lines.append(f"  - [{blocker.get('kind')}] {where}: {blocker.get('detail')}")
        lines.append(
            "Resolve these in the mapping step (the relationship agent can wire most "
            "of them), or set AGENT_PREFLIGHT_ENFORCE=false to launch anyway."
        )
        return "\n".join(lines)


class PreflightBlocked(RuntimeError):
    """A migration was stopped before extraction by the launch gate."""

    def __init__(self, result: PreflightResult) -> None:
        super().__init__(result.message())
        self.result = result


def run(
    source_type: str,
    mapping_names: list[str],
    source_credentials: dict | None = None,
    destination_credentials: dict | None = None,
    settings: AgentSettings | None = None,
    context: MigrationContext | None = None,
) -> PreflightResult:
    """Scan the selected mappings. Never raises: inspect ``ok`` on the result."""
    settings = settings or load_settings()
    result = PreflightResult(enforced=settings.preflight_enforce)
    try:
        context = context or MigrationContext.load(
            source_type=source_type,
            mapping_names=mapping_names,
            source_credentials=source_credentials,
            destination_credentials=destination_credentials,
        )
        if not context.mapping_names:
            result.note = "No mapping configs were found for the selected objects"
            return result

        findings = detector.detect(context)
        analysis = context.dependency_analysis()
        result.checked = True
        result.blockers = [finding.to_dict() for finding in detector.blocking(findings)]
        result.warnings = [
            finding.to_dict()
            for finding in findings
            if not finding.blocking and finding.severity != "info"
        ]
        result.order = analysis.get("order", [])
        result.has_cycle = bool(analysis.get("has_cycle"))
        result.ok = not result.blockers

        from services.internal import backfill_service

        result.deferred = backfill_service.pending_notice(context.mapping_names)
    except Exception as exc:  # noqa: BLE001 — a gate fault must not block a run
        logger.warning("[agent] preflight could not complete (non-blocking): %s", exc)
        result.note = f"Preflight did not complete: {exc}"
    return result


def enforce(
    source_type: str,
    mapping_names: list[str],
    source_credentials: dict | None = None,
    destination_credentials: dict | None = None,
    settings: AgentSettings | None = None,
    context: MigrationContext | None = None,
) -> PreflightResult:
    """Run the gate and raise :class:`PreflightBlocked` when it should stop the run."""
    settings = settings or load_settings()
    result = run(
        source_type=source_type,
        mapping_names=mapping_names,
        source_credentials=source_credentials,
        destination_credentials=destination_credentials,
        settings=settings,
        context=context,
    )
    if result.blockers:
        if result.enforced:
            raise PreflightBlocked(result)
        logger.warning(
            "[agent] preflight found %d blocker(s) but enforcement is off:\n%s",
            len(result.blockers),
            result.message(),
        )
    return result
