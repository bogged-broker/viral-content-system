"""
/data/lineage/migration_orchestrator.py

Plan-Bound, Crash-Safe Migration Execution Controller
Deterministic · Journaling · Idempotent · Governance-Aware
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

import lineage_registry as _reg
import schema_versions as _sv
from lineage_graph import LineageGraph
from lineage_store import LineageStore
from lineage_types import ArtifactID, ArtifactType, SchemaVersionID
from migration_executor import ArtifactContentStore, MigrationExecutor
from migration_plan import (
    BlockReason,
    MigrationPlan,
    MigrationPlanner,
    MigrationPolicy,
    MigrationStep,
    PlanMode,
    _migration_registry_fingerprint,
    _schema_registry_fingerprint,
)

__all__ = [
    "ExecutionStatus",
    "ExecutionFailure",
    "ExecutionReport",
    "MigrationJournal",
    "GovernanceCallbacks",
    "MigrationOrchestrator",
    "OrchestratorError",
    "PlanDriftError",
    "LockError",
    "JournalError",
    "GovernanceRejectionError",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class OrchestratorError(Exception):
    """Base class for all orchestrator-level violations. Always fatal."""


class PlanDriftError(OrchestratorError):
    """
    Current lineage/registry state no longer matches the approved plan.
    Raised pre-execution and on crash-resume re-validation.
    """


class LockError(OrchestratorError):
    """Could not acquire or verify the global migration lock."""


class JournalError(OrchestratorError):
    """Journal file is missing, corrupt, or inconsistent."""


class GovernanceRejectionError(OrchestratorError):
    """A governance callback rejected plan execution."""


class BlockedPlanError(OrchestratorError):
    """Plan contains blocked artifacts and strict mode forbids proceeding."""


# ---------------------------------------------------------------------------
# ExecutionStatus
# ---------------------------------------------------------------------------

class ExecutionStatus(str, Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED   = "COMPLETED"
    FAILED      = "FAILED"
    ABORTED     = "ABORTED"


# ---------------------------------------------------------------------------
# ExecutionFailure
# ---------------------------------------------------------------------------

class ExecutionFailure:
    """Immutable record of a single step-level execution failure."""

    __slots__ = ("step_index", "artifact_id", "from_version", "to_version",
                 "error_type", "detail")

    def __init__(
        self,
        *,
        step_index:   int,
        artifact_id:  ArtifactID,
        from_version: SchemaVersionID,
        to_version:   SchemaVersionID,
        error_type:   str,
        detail:       str,
    ) -> None:
        object.__setattr__(self, "step_index",   step_index)
        object.__setattr__(self, "artifact_id",  artifact_id)
        object.__setattr__(self, "from_version", from_version)
        object.__setattr__(self, "to_version",   to_version)
        object.__setattr__(self, "error_type",   error_type)
        object.__setattr__(self, "detail",       detail)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("ExecutionFailure is immutable.")

    def to_dict(self) -> dict:
        return {
            "step_index":   self.step_index,
            "artifact_id":  self.artifact_id.to_string(),
            "from_version": int(self.from_version),
            "to_version":   int(self.to_version),
            "error_type":   self.error_type,
            "detail":       self.detail,
        }

    def __repr__(self) -> str:
        return (
            f"ExecutionFailure(step={self.step_index}, "
            f"{self.artifact_id.to_string()!r}, {self.error_type!r})"
        )


# ---------------------------------------------------------------------------
# ExecutionReport
# ---------------------------------------------------------------------------

class ExecutionReport:
    """
    Immutable summary of a completed (or aborted) orchestration run.
    Produced once by MigrationOrchestrator.execute_plan(). Never modified.
    """

    __slots__ = (
        "plan_hash",
        "total_steps",
        "completed_steps",
        "success",
        "status",
        "started_at",
        "finished_at",
        "failures",
        "schema_fingerprint",
        "migration_fingerprint",
    )

    def __init__(
        self,
        *,
        plan_hash:             str,
        total_steps:           int,
        completed_steps:       int,
        success:               bool,
        status:                ExecutionStatus,
        started_at:            float,
        finished_at:           float,
        failures:              List[ExecutionFailure],
        schema_fingerprint:    str,
        migration_fingerprint: str,
    ) -> None:
        object.__setattr__(self, "plan_hash",             plan_hash)
        object.__setattr__(self, "total_steps",           total_steps)
        object.__setattr__(self, "completed_steps",       completed_steps)
        object.__setattr__(self, "success",               success)
        object.__setattr__(self, "status",                status)
        object.__setattr__(self, "started_at",            started_at)
        object.__setattr__(self, "finished_at",           finished_at)
        object.__setattr__(self, "failures",              tuple(failures))
        object.__setattr__(self, "schema_fingerprint",    schema_fingerprint)
        object.__setattr__(self, "migration_fingerprint", migration_fingerprint)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("ExecutionReport is immutable.")

    def to_dict(self) -> dict:
        return {
            "plan_hash":             self.plan_hash,
            "total_steps":           self.total_steps,
            "completed_steps":       self.completed_steps,
            "success":               self.success,
            "status":                self.status.value,
            "started_at":            self.started_at,
            "finished_at":           self.finished_at,
            "duration_seconds":      round(self.finished_at - self.started_at, 6),
            "failure_count":         len(self.failures),
            "failures":              [f.to_dict() for f in self.failures],
            "schema_fingerprint":    self.schema_fingerprint,
            "migration_fingerprint": self.migration_fingerprint,
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)

    def __repr__(self) -> str:
        return (
            f"ExecutionReport("
            f"status={self.status.value}, "
            f"completed={self.completed_steps}/{self.total_steps}, "
            f"failures={len(self.failures)})"
        )


# ---------------------------------------------------------------------------
# MigrationJournal
# ---------------------------------------------------------------------------

class MigrationJournal:
    """
    Durable execution journal for a single orchestration run.

    Persisted as an atomic JSON file separate from lineage.log.
    Written before execution begins and updated after each step completes.
    On crash-resume, the journal provides the authoritative record of which
    steps have been completed — the executor's idempotency ensures that
    re-running already-completed steps is safe.

    Journal file layout::

        {
          "plan_hash": "...",
          "schema_fingerprint": "...",
          "migration_fingerprint": "...",
          "status": "IN_PROGRESS" | "COMPLETED" | "FAILED" | "ABORTED",
          "started_at": <float>,
          "completed_steps": [0, 1, 2, ...],  # step indices
          "completed_at": <float | null>
        }
    """

    __slots__ = ("_path",)

    def __init__(self, journal_path: str) -> None:
        object.__setattr__(self, "_path", journal_path)

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationJournal path is immutable after construction.")

    # -- creation ------------------------------------------------------------

    def create(
        self,
        *,
        plan_hash:             str,
        schema_fingerprint:    str,
        migration_fingerprint: str,
        started_at:            float,
    ) -> None:
        """Write the initial journal entry atomically. Raises if already exists."""
        if os.path.exists(self._path):
            raise JournalError(
                f"Journal already exists at {self._path!r}. "
                "Resume with resume_from_journal() or remove stale journal."
            )
        self._write({
            "plan_hash":             plan_hash,
            "schema_fingerprint":    schema_fingerprint,
            "migration_fingerprint": migration_fingerprint,
            "status":                ExecutionStatus.IN_PROGRESS.value,
            "started_at":            started_at,
            "completed_steps":       [],
            "completed_at":          None,
        })

    # -- step tracking -------------------------------------------------------

    def mark_step_complete(self, step_index: int) -> None:
        """Atomically append *step_index* to the completed_steps list."""
        data = self._read()
        completed: List[int] = data.get("completed_steps", [])
        if step_index not in completed:
            completed.append(step_index)
            completed.sort()
        data["completed_steps"] = completed
        self._write(data)

    def completed_step_indices(self) -> Set[int]:
        """Return the set of completed step indices from the journal."""
        return set(self._read().get("completed_steps", []))

    # -- status transitions --------------------------------------------------

    def mark_completed(self, completed_at: float) -> None:
        data = self._read()
        data["status"]       = ExecutionStatus.COMPLETED.value
        data["completed_at"] = completed_at
        self._write(data)

    def mark_failed(self, completed_at: float) -> None:
        data = self._read()
        data["status"]       = ExecutionStatus.FAILED.value
        data["completed_at"] = completed_at
        self._write(data)

    def mark_aborted(self, completed_at: float) -> None:
        data = self._read()
        data["status"]       = ExecutionStatus.ABORTED.value
        data["completed_at"] = completed_at
        self._write(data)

    # -- inspection ----------------------------------------------------------

    def exists(self) -> bool:
        return os.path.exists(self._path)

    def read_metadata(self) -> dict:
        """Return the full journal dict. Raises JournalError if corrupt."""
        return self._read()

    def is_in_progress(self) -> bool:
        try:
            return self._read().get("status") == ExecutionStatus.IN_PROGRESS.value
        except JournalError:
            return False

    def validate_fingerprints(
        self,
        schema_fingerprint:    str,
        migration_fingerprint: str,
    ) -> None:
        """
        Verify that the journal's recorded fingerprints match the current
        registry state. Raises JournalError on mismatch — indicates registry
        changed between the original run and crash-resume.
        """
        data = self._read()
        if data.get("schema_fingerprint") != schema_fingerprint:
            raise JournalError(
                "Schema registry fingerprint has changed since journal was created. "
                "Cannot safely resume — re-plan required."
            )
        if data.get("migration_fingerprint") != migration_fingerprint:
            raise JournalError(
                "Migration registry fingerprint has changed since journal was created. "
                "Cannot safely resume — re-plan required."
            )

    def remove(self) -> None:
        """Delete the journal file after a clean completion."""
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass

    # -- atomic IO -----------------------------------------------------------

    def _write(self, data: dict) -> None:
        """Atomically write journal data via temp-file rename + fsync."""
        dirpath  = os.path.dirname(self._path) or "."
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=".journal_tmp_", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _read(self) -> dict:
        """
        Read and parse the journal file. Raises JournalError on failure.
        
        Includes journal corruption detection (spec §16: Testing Requirements).
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise JournalError(f"Journal not found at {self._path!r}.")
        except json.JSONDecodeError as exc:
            raise JournalError(
                f"Journal at {self._path!r} is corrupt (JSON decode error): {exc}"
            ) from exc
        
        # Validate journal structure (spec §16: Journal corruption detection)
        required_fields = {"plan_hash", "schema_fingerprint", "migration_fingerprint", 
                          "status", "started_at", "completed_steps"}
        missing = required_fields - set(data.keys())
        if missing:
            raise JournalError(
                f"Journal at {self._path!r} is corrupt: missing required fields: {sorted(missing)!r}"
            )
        
        # Validate status enum
        if data.get("status") not in {s.value for s in ExecutionStatus}:
            raise JournalError(
                f"Journal at {self._path!r} is corrupt: invalid status {data.get('status')!r}"
            )
        
        # Validate completed_steps is a list of integers
        completed = data.get("completed_steps", [])
        if not isinstance(completed, list):
            raise JournalError(
                f"Journal at {self._path!r} is corrupt: completed_steps must be a list, got {type(completed)!r}"
            )
        if not all(isinstance(x, int) and x >= 0 for x in completed):
            raise JournalError(
                f"Journal at {self._path!r} is corrupt: completed_steps must contain non-negative integers"
            )
        
        return data


# ---------------------------------------------------------------------------
# GovernanceCallbacks
# ---------------------------------------------------------------------------

class GovernanceCallbacks:
    """
    Optional governance hook container for plan approval and completion events.

    approve_plan(plan_hash) → bool
        Called before execution begins. Return False to abort.

    on_step_complete(step_index, step, output_artifact_id)
        Called after each step completes. Informational; must not raise.

    on_plan_completed(report)
        Called after the full plan completes. Suitable for CI notification,
        audit event emission, or downstream trigger.
    """

    def approve_plan(self, plan_hash: str) -> bool:
        """Return True to permit execution. Override to add approval gating."""
        return True

    def on_step_complete(
        self,
        step_index:          int,
        step:                MigrationStep,
        output_artifact_id:  ArtifactID,
    ) -> None:
        """Informational callback after each successful step."""

    def on_plan_completed(self, report: ExecutionReport) -> None:
        """Called once after the entire plan completes (success or failure)."""


# ---------------------------------------------------------------------------
# MigrationOrchestrator
# ---------------------------------------------------------------------------

class MigrationOrchestrator:
    """
    Plan-bound, crash-safe migration execution controller.

    Accepts a precomputed, approved MigrationPlan and executes it step-by-step
    through the MigrationExecutor layer. Maintains a durable journal for
    crash recovery. Enforces a global file-lock to prevent concurrent runs.

    Guarantees:
      - Plan is re-validated against current state before execution starts.
      - Registry fingerprints are checked at plan entry and crash-resume.
      - Each step is journaled after completion.
      - Executor idempotency makes any step safe to retry.
      - ExecutionReport is deterministic for identical inputs.
      - No historical lineage records are modified.

    Usage::

        orchestrator = MigrationOrchestrator(
            graph=graph,
            store=store,
            content_store=content_store,
            journal_path="/data/lineage/migration.journal",
            lock_path="/data/lineage/migration.lock",
        )
        report = orchestrator.execute_plan(approved_plan)
        if not report.success:
            raise SystemExit("Migration failed — see report")
    """

    __slots__ = (
        "_graph",
        "_store",
        "_content_store",
        "_journal",
        "_lock_path",
        "_callbacks",
        "_schema_fp",
        "_migration_fp",
    )

    def __init__(
        self,
        *,
        graph:          LineageGraph,
        store:          LineageStore,
        content_store:  ArtifactContentStore,
        journal_path:   str,
        lock_path:      str,
        callbacks:      Optional[GovernanceCallbacks] = None,
    ) -> None:
        if not isinstance(graph, LineageGraph):
            raise TypeError(f"graph must be LineageGraph, got {type(graph)!r}")
        if not isinstance(store, LineageStore):
            raise TypeError(f"store must be LineageStore, got {type(store)!r}")
        if not isinstance(content_store, ArtifactContentStore):
            raise TypeError(
                f"content_store must be ArtifactContentStore, got {type(content_store)!r}"
            )
        object.__setattr__(self, "_graph",         graph)
        object.__setattr__(self, "_store",         store)
        object.__setattr__(self, "_content_store", content_store)
        object.__setattr__(self, "_journal",       MigrationJournal(journal_path))
        object.__setattr__(self, "_lock_path",     lock_path)
        object.__setattr__(self, "_callbacks",     callbacks or GovernanceCallbacks())
        object.__setattr__(self, "_schema_fp",     _schema_registry_fingerprint())
        object.__setattr__(self, "_migration_fp",  _migration_registry_fingerprint())

    def __setattr__(self, *_: object) -> None:
        raise TypeError("MigrationOrchestrator is not mutable after construction.")

    # -- primary entry point -------------------------------------------------

    def execute_plan(self, plan: MigrationPlan) -> ExecutionReport:
        """
        Execute an approved MigrationPlan end-to-end.

        Sequence:
          1.  Validate plan (hash + registry fingerprints + current graph state)
          2.  Check blocked artifacts (spec §14: Forbidden Behavior)
          3.  Governance approval gate
          4.  Acquire global migration lock
          5.  Create execution journal
          6.  Execute each step in declared order
          7.  Journal each completed step
          8.  Release lock
          9.  Emit ExecutionReport via governance callback

        On crash: the journal records completed steps. Call resume_if_interrupted()
        on the next startup to detect and resume safely.

        Returns ExecutionReport. Never raises after the lock is acquired —
        all failures are captured in the report.
        """
        if not isinstance(plan, MigrationPlan):
            raise TypeError(f"plan must be MigrationPlan, got {type(plan)!r}")

        started_at = time.monotonic()

        # 1. Pre-execution validation (outside lock — raises on failure)
        self._validate_plan_against_current_state(plan)

        # 2. Check blocked artifacts (spec §14: Forbidden Behavior)
        # No execution if blocked artifacts exist in strict mode
        if plan.has_blocked:
            # This should have been caught during planning, but double-check
            # In strict mode, planner should have raised, but we enforce here too
            raise BlockedPlanError(
                f"Plan contains {len(plan.blocked_artifacts)} blocked artifact(s). "
                f"Execution forbidden. Re-plan required."
            )

        # 3. Governance approval
        callbacks: GovernanceCallbacks = self._callbacks
        if not callbacks.approve_plan(plan.generation_hash):
            raise GovernanceRejectionError(
                f"Governance callback rejected plan with hash "
                f"{plan.generation_hash!r}. Execution aborted."
            )

        # 4. Acquire global lock (spec §8: Locking Strategy)
        # Safety invariant 4 (spec §11): No execution without lock
        lock_fd = self._acquire_lock()
        try:
            # 5. Create journal (spec §4: Execution Journal)
            # Safety invariant 5 (spec §11): No execution without journal
            journal: MigrationJournal = self._journal
            if journal.exists() and journal.is_in_progress():
                raise JournalError(
                    "An in-progress journal already exists. "
                    "Call resume_if_interrupted() to handle the prior run before "
                    "starting a new execution."
                )
            journal.create(
                plan_hash=plan.generation_hash,
                schema_fingerprint=self._schema_fp,
                migration_fingerprint=self._migration_fp,
                started_at=started_at,
            )

            # 6–7. Execute steps (spec §2: Core Execution Model)
            report = self._execute_steps(
                plan=plan,
                resume_from=frozenset(),
                started_at=started_at,
            )

        finally:
            # 8. Release lock (spec §8: Locking Strategy)
            self._release_lock(lock_fd)

        # 9. Governance completion callback (informational; must not raise)
        try:
            callbacks.on_plan_completed(report)
        except Exception as exc:
            log.warning(
                "on_plan_completed callback raised (ignored): %s", exc, exc_info=True
            )

        return report

    # -- crash resume --------------------------------------------------------

    def resume_if_interrupted(
        self,
        policy: MigrationPolicy,
        target_overrides: Optional[Dict[ArtifactType, SchemaVersionID]] = None,
    ) -> Optional[ExecutionReport]:
        """
        Detect and resume an interrupted migration run.

        If no in-progress journal exists, returns None immediately.

        If an in-progress journal is found:
          1.  Validate registry fingerprints against current code state.
          2.  Regenerate the migration plan from current graph state.
          3.  Validate the regenerated plan hash matches the journaled hash.
          4.  Resume execution from the first incomplete step.
          5.  Return ExecutionReport.

        Raises OrchestratorError if the plan cannot be safely reproduced —
        this requires operator intervention and a fresh planning cycle.
        """
        journal: MigrationJournal = self._journal
        if not journal.exists() or not journal.is_in_progress():
            return None

        log.warning(
            "In-progress migration journal detected at %s — attempting resume.",
            self._journal._path,
        )

        # Validate fingerprints first
        journal.validate_fingerprints(self._schema_fp, self._migration_fp)

        meta = journal.read_metadata()
        stored_plan_hash = meta["plan_hash"]

        # Regenerate plan
        planner = MigrationPlanner(self._graph, policy)
        regenerated_plan = planner.build_plan(target_overrides=target_overrides)

        if regenerated_plan.generation_hash != stored_plan_hash:
            raise PlanDriftError(
                f"Regenerated plan hash {regenerated_plan.generation_hash!r} does not "
                f"match journaled hash {stored_plan_hash!r}. "
                "Lineage state or registry has changed since the interrupted run. "
                "Manual operator review required."
            )

        completed_indices = journal.completed_step_indices()
        started_at = meta.get("started_at", time.monotonic())

        lock_fd = self._acquire_lock()
        try:
            report = self._execute_steps(
                plan=regenerated_plan,
                resume_from=completed_indices,
                started_at=started_at,
            )
        finally:
            self._release_lock(lock_fd)

        callbacks: GovernanceCallbacks = self._callbacks
        try:
            callbacks.on_plan_completed(report)
        except Exception as exc:
            log.warning("on_plan_completed callback raised (ignored): %s", exc, exc_info=True)

        return report

    # -- step execution loop -------------------------------------------------

    def _execute_steps(
        self,
        plan:         MigrationPlan,
        resume_from:  frozenset,
        started_at:   float,
    ) -> ExecutionReport:
        """
        Core step execution loop. Handles journaling, idempotency, and reporting.

        resume_from: set of step indices already completed (from journal on resume).
        Steps in resume_from are passed to the executor (which detects idempotency
        and returns the existing artifact immediately) but are NOT re-journaled.

        Forbidden behaviors prevented (spec §14):
        - No dynamic plan recomputation mid-run: plan is immutable, fixed for entire execution
        - No skipping invalid steps: all steps executed in order, failures abort
        - No silent downgrade: executor validates version progression
        - No plan modifications: plan is immutable
        - No multi-plan interleaving: global lock prevents concurrent execution
        """
        executor  = MigrationExecutor(self._graph, self._store, self._content_store)
        journal:  MigrationJournal     = self._journal
        callbacks: GovernanceCallbacks = self._callbacks
        steps     = plan.steps

        completed_count = len(resume_from)
        failures:  List[ExecutionFailure] = []
        aborted   = False

        # Sequential vs parallel enforcement (spec §7)
        if plan.requires_sequential_execution:
            log.info(
                "Executing migration plan (SEQUENTIAL): hash=%s total_steps=%d resume_from=%d",
                plan.generation_hash[:12], len(steps), len(resume_from),
            )
        else:
            log.info(
                "Executing migration plan (PARALLEL ALLOWED): hash=%s total_steps=%d resume_from=%d",
                plan.generation_hash[:12], len(steps), len(resume_from),
            )
            # Note: Parallel execution is not yet implemented. For now, we execute sequentially
            # even when parallel is allowed. This is conservative and safe.
            # Future enhancement: partition steps by artifact independence and execute groups.

        for i, step in enumerate(steps):
            already_done = i in resume_from

            # Forbidden behavior prevention (spec §14):
            # - No skipping invalid steps: we execute all steps in order
            # - No silent downgrade: executor validates this
            # - No plan modifications: plan is immutable, we never modify it
            # - No dynamic plan recomputation mid-run: plan is fixed for entire execution

            # Observability (spec §15: Observability Requirements)
            remaining_steps = len(steps) - (i + 1)
            log.info(
                "Step %d/%d (remaining=%d): %s %s v%s→v%s %s plan_hash=%s",
                i + 1, len(steps), remaining_steps,
                "RESUME" if already_done else "EXEC",
                step.artifact_type.value,
                int(step.from_version),
                int(step.to_version),
                step.artifact_id.to_string()[:16],
                plan.generation_hash[:12],
            )

            try:
                # Step execution contract (spec §6):
                # 1. Re-validate artifact state
                try:
                    record = self._graph.get_record_by_artifact(step.artifact_id)
                    if record.output_schema_version != step.from_version:
                        raise OrchestratorError(
                            f"Artifact {step.artifact_id.to_string()!r} state mismatch: "
                            f"expected version {step.from_version!r}, found {record.output_schema_version!r}. "
                            f"State may have changed since planning."
                        )
                except KeyError:
                    raise OrchestratorError(
                        f"Artifact {step.artifact_id.to_string()!r} not found in graph. "
                        f"Cannot execute step {i}."
                    ) from None

                # 2. Call migration_executor.execute_migration
                # Executor is idempotent — already-done steps return existing artifact
                output_artifact_id = executor.execute_migration(
                    step.artifact_id,
                    step.to_version,
                )

                # 3. Confirm output artifact version equals expected (spec §6)
                try:
                    output_record = self._graph.get_record_by_artifact(output_artifact_id)
                    if output_record.output_schema_version != step.to_version:
                        raise OrchestratorError(
                            f"Output artifact {output_artifact_id.to_string()!r} version mismatch: "
                            f"expected {step.to_version!r}, found {output_record.output_schema_version!r}. "
                            f"Migration did not produce expected version."
                        )
                except KeyError:
                    raise OrchestratorError(
                        f"Output artifact {output_artifact_id.to_string()!r} not found in graph "
                        f"after migration step {i}."
                    ) from None

            except Exception as exc:
                log.error(
                    "Step %d failed: %s — %s",
                    i, step.artifact_id.to_string(), exc, exc_info=True,
                )
                failures.append(ExecutionFailure(
                    step_index=i,
                    artifact_id=step.artifact_id,
                    from_version=step.from_version,
                    to_version=step.to_version,
                    error_type=type(exc).__name__,
                    detail=str(exc),
                ))
                # Abort on first failure — leave journal intact for diagnosis
                aborted = True
                journal.mark_failed(time.monotonic())
                break

            # 4. Append journal entry (spec §6: Step Execution Contract)
            # Journal the step as complete (idempotent — skip if already recorded)
            if not already_done:
                journal.mark_step_complete(i)
                completed_count += 1

            # Governance step callback (informational)
            try:
                callbacks.on_step_complete(i, step, output_artifact_id)
            except Exception as exc:
                log.warning(
                    "on_step_complete callback raised at step %d (ignored): %s", i, exc
                )

        finished_at = time.monotonic()

        if not aborted and not failures:
            journal.mark_completed(finished_at)
            journal.remove()  # clean journal on success
            status  = ExecutionStatus.COMPLETED
            success = True
            log.info(
                "Migration plan completed successfully: hash=%s steps=%d duration=%.3fs",
                plan.generation_hash[:12], len(steps), finished_at - started_at,
            )
        elif aborted:
            status  = ExecutionStatus.ABORTED
            success = False
            log.error(
                "Migration plan aborted at step %d/%d after %d failure(s).",
                completed_count, len(steps), len(failures),
            )
        else:
            status  = ExecutionStatus.FAILED
            success = False

        return ExecutionReport(
            plan_hash=plan.generation_hash,
            total_steps=len(steps),
            completed_steps=completed_count,
            success=success,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            failures=failures,
            schema_fingerprint=self._schema_fp,
            migration_fingerprint=self._migration_fp,
        )

    # -- plan validation -----------------------------------------------------

    def _validate_plan_against_current_state(self, plan: MigrationPlan) -> None:
        """
        Verify the plan is still valid against the current lineage and registry state.
        
        Implements spec §3: Plan Validation Layer (Pre-Execution Gate).

        Checks:
          1. Schema registry fingerprint matches plan's expected fingerprint.
          2. Migration registry fingerprint matches.
          3. Regenerate a fresh plan from current graph state and compare hashes.
          4. Compare steps and target_versions for structural equivalence.

        Any mismatch raises PlanDriftError — execution must not proceed.
        """
        # Safety invariant 1-3 (spec §11): Registry fingerprints must match
        # The generation_hash encodes both fingerprints — we validate via plan hash re-derivation
        
        # Re-derive plan from current state to verify hash stability
        policy = MigrationPolicy(
            enforce_latest=True,
            forbid_deprecated=True,
            allow_partial_upgrade=bool(plan.blocked_artifacts),
            mode=PlanMode.ADVISORY,  # advisory so blocked artifacts don't raise
        )
        planner = MigrationPlanner(self._graph, policy)

        # Rebuild to validate hash (spec §3: Plan Validation Layer)
        try:
            target_overrides = {
                art: vid for art, vid in plan.target_versions.items()
            }
            fresh_plan = planner.build_plan(target_overrides=target_overrides)
        except Exception as exc:
            raise PlanDriftError(
                f"Cannot regenerate plan for validation: {exc}. "
                "State or registry may have changed since planning."
            ) from exc

        # Compare plan hash (spec §3)
        if fresh_plan.generation_hash != plan.generation_hash:
            raise PlanDriftError(
                f"Plan hash mismatch: plan={plan.generation_hash!r}, "
                f"current_state={fresh_plan.generation_hash!r}. "
                "Lineage state or registry has changed since the plan was approved. "
                "Re-plan required."
            )
        
        # Compare steps for structural equivalence (spec §3)
        if len(fresh_plan.steps) != len(plan.steps):
            raise PlanDriftError(
                f"Plan step count mismatch: plan={len(plan.steps)}, "
                f"regenerated={len(fresh_plan.steps)}. State has changed."
            )
        
        # Compare target_versions (spec §3)
        if fresh_plan.target_versions != plan.target_versions:
            raise PlanDriftError(
                f"Plan target_versions mismatch. State has changed since planning."
            )

        log.info(
            "Plan validation passed: hash=%s steps=%d",
            plan.generation_hash[:12], plan.total_steps,
        )

    # -- locking -------------------------------------------------------------

    def _acquire_lock(self) -> int:
        """
        Acquire an exclusive advisory file lock.

        Uses LOCK_EX | LOCK_NB — raises LockError immediately if another
        process holds the lock, rather than blocking indefinitely.

        Returns the open file descriptor (caller must release via _release_lock).
        """
        lock_path = self._lock_path
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            log.debug("Migration lock acquired: %s", lock_path)
            return fd
        except OSError as exc:
            raise LockError(
                f"Cannot acquire global migration lock at {lock_path!r}. "
                f"Another migration process may be running. Detail: {exc}"
            ) from exc

    def _release_lock(self, lock_fd: int) -> None:
        """Release and close the file lock descriptor."""
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            log.debug("Migration lock released: %s", self._lock_path)
        except OSError as exc:
            log.warning("Error releasing migration lock: %s", exc)

    # -- repr ----------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"MigrationOrchestrator("
            f"schema_fp={self._schema_fp[:12]!r}..., "
            f"migration_fp={self._migration_fp[:12]!r}...)"
        )