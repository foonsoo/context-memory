from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import retrieval, wiki_lint
from .audit_serialization import verify_audit_chain_bundle
from .clock import utc_now
from .context_assembly import ContextAssembler
from .contracts import (
    MEMORY_TYPES,
    PROMOTABLE_EVENT_KINDS,
)
from .decision_assembly import DecisionAssembler
from .embeddings import (
    EmbeddingProvider,
    LocalHashEmbedding,
    SentenceTransformerEmbedding,
)
from .persistence import (
    CheckpointRepository,
    InvestigationRepository,
    MaintenanceRepository,
    MemoryRepository,
    OperationsRepository,
    ProjectEvidenceRepository,
    RetrievalRepository,
    ReviewRepository,
    TransferRepository,
    WikiRepository,
)
from .persistence.primitives import row_dict
from .retrieval import retrieval_gate, select_project_candidate
from .serialization import canonical, canonical_digest
from .validation import normalize_test_results

DISCOVERY_MIN_CONFIDENCE = retrieval.DISCOVERY_MIN_CONFIDENCE
DISCOVERY_AUTO_SELECT_CONFIDENCE = retrieval.DISCOVERY_AUTO_SELECT_CONFIDENCE
DISCOVERY_MIN_MARGIN = retrieval.DISCOVERY_MIN_MARGIN
DISCOVERY_PROJECT_CANDIDATE_LIMIT = retrieval.DISCOVERY_PROJECT_CANDIDATE_LIMIT
NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY = (
    retrieval.NEGATIVE_VECTOR_ONLY_MIN_SIMILARITY
)
NEGATIVE_VECTOR_ONLY_MIN_SEPARATION = (
    retrieval.NEGATIVE_VECTOR_ONLY_MIN_SEPARATION
)
LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT = (
    retrieval.LOCAL_HASH_FALLBACK_CANDIDATE_LIMIT
)
LOCAL_HASH_FALLBACK_TIME_LIMIT_MS = retrieval.LOCAL_HASH_FALLBACK_TIME_LIMIT_MS
SOURCE_REINSPECTION_AGE_DAYS = wiki_lint.SOURCE_REINSPECTION_AGE_DAYS

TYPES = MEMORY_TYPES
STATUSES = {
    "proposed",
    "active",
    "superseded",
    "disputed",
    "expired",
    "rejected",
}
RELATIONS = {"supersedes", "disputes", "supports", "depends_on", "related_to"}
CHECKPOINT_MODES = {"interim", "final"}
CHECKPOINT_REASONS = {
    "context_budget",
    "elapsed",
    "material_change",
    "completed",
    "manual",
}


def now() -> str:
    return utc_now(datetime)


def current_datetime() -> datetime:
    """Return time through the patchable timestamp contract."""
    return datetime.fromisoformat(now())


def uid() -> str:
    return str(uuid.uuid4())


class MemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.path = Path(db_path).expanduser().resolve()
        self._secure_directory()
        self.conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=10
        )
        self.conn.row_factory = sqlite3.Row
        self.project_evidence = ProjectEvidenceRepository(
            self, now, current_datetime, uid
        )
        self.retrieval_repository = RetrievalRepository(
            self, now, current_datetime
        )
        self.review = ReviewRepository(self, now, uid)
        self.transfer = TransferRepository(self)
        self.checkpoints = CheckpointRepository(
            self, now, uid, current_datetime
        )
        self.context_assembler = ContextAssembler(self)
        self.decision_assembler = DecisionAssembler(
            self, now, current_datetime
        )
        self.investigations = InvestigationRepository(self, now, uid)
        self.maintenance = MaintenanceRepository(
            self, now, uid, current_datetime
        )
        self.memories = MemoryRepository(self, now, uid)
        self.operations = OperationsRepository(self, now, uid)
        self.wiki = WikiRepository(self, now, uid, current_datetime)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.migrate()
        configured_mode = os.environ.get(
            "CONTEXT_MEMORY_EMBEDDINGS", "local-hash"
        ).strip()
        mode = configured_mode.casefold()
        embeddings_enabled = mode not in {
            "0",
            "false",
            "off",
            "disabled",
            "none",
        }
        if embedding_provider:
            self.embedding_provider = embedding_provider
        elif mode in {"neural", "sentence-transformers"}:
            model = os.environ.get(
                "CONTEXT_MEMORY_EMBEDDING_MODEL", ""
            ).strip()
            if not model:
                raise ValueError(
                    "CONTEXT_MEMORY_EMBEDDING_MODEL is required when "
                    "CONTEXT_MEMORY_EMBEDDINGS=neural"
                )
            self.embedding_provider = SentenceTransformerEmbedding(
                model,
                device=os.environ.get("CONTEXT_MEMORY_EMBEDDING_DEVICE")
                or None,
            )
        else:
            self.embedding_provider = (
                LocalHashEmbedding() if embeddings_enabled else None
            )
        if self.embedding_provider:
            with self.tx() as cx:
                for memory in cx.execute("SELECT * FROM memories"):
                    self._index_embedding(cx, dict(memory))

    def _secure_directory(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.path.parent, stat.S_IRWXU)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def migrate(self) -> None:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER"
            " PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        here = Path(__file__).resolve()
        roots = [
            here.parents[2] / "migrations",
            here.parents[1] / "migrations",
        ]
        root = next(
            (candidate for candidate in roots if candidate.is_dir()), roots[0]
        )
        if not root.is_dir():
            raise RuntimeError(
                "database migrations are missing from this installation"
            )
        applied = {
            r[0]
            for r in self.conn.execute("SELECT version FROM schema_migrations")
        }
        for file in sorted(root.glob("[0-9][0-9][0-9]_*.sql")):
            version = int(file.name.split("_", 1)[0])
            if version in applied:
                continue
            script = file.read_text(encoding="utf-8")
            # executescript owns its transaction; migration is recorded
            # only
            # after all statements succeed.
            self.conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + "\nINSERT INTO schema_migrations"
                f" VALUES({version},'{now()}');\nCOMMIT;"
            )

    def _row(self, query: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self.conn.execute(query, args).fetchone()
        return row_dict(row)

    def _audit(
        self,
        cx: sqlite3.Connection,
        project_id: str | None,
        kind: str,
        entity_id: str,
        action: str,
        snapshot: Any,
    ) -> None:
        cx.execute(
            "INSERT INTO"
            " audit_log(project_id,entity_type,entity_id,action,"
            "snapshot_json,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (project_id, kind, entity_id, action, canonical(snapshot), now()),
        )

    def _idem(
        self, operation: str, key: str | None, request: Any
    ) -> dict[str, Any] | None:
        if not key:
            return None
        row = self._row(
            "SELECT request_hash,response_json FROM idempotency_keys WHERE"
            " operation=? AND key=?",
            (operation, key),
        )
        if not row:
            return None
        digest = canonical_digest(request)
        if digest != row["request_hash"]:
            raise ValueError("idempotency key reused with a different request")
        return json.loads(row["response_json"])

    def _save_idem(
        self,
        cx: sqlite3.Connection,
        operation: str,
        key: str | None,
        request: Any,
        response: Any,
    ) -> None:
        if key:
            cx.execute(
                "INSERT INTO idempotency_keys VALUES(?,?,?,?,?)",
                (
                    operation,
                    key,
                    canonical_digest(request),
                    canonical(response),
                    now(),
                ),
            )

    def create_project(
        self,
        slug: str,
        name: str | None = None,
        description: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.project_evidence.create_project(
            slug, name, description, idempotency_key
        )

    def list_projects(self) -> list[dict[str, Any]]:
        return self.project_evidence.list_projects()

    @staticmethod
    def _normalize_project_alias(kind: str, value: str) -> str:
        return ProjectEvidenceRepository._normalize_project_alias(kind, value)

    def set_project_alias(
        self, project_id: str, kind: str, value: str
    ) -> dict[str, Any]:
        return self.project_evidence.set_project_alias(project_id, kind, value)

    def list_project_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return self.project_evidence.list_project_aliases(project_id)

    def _workspace_identities(self, path: str) -> dict[str, str]:
        return self.project_evidence._workspace_identities(path)

    def _register_project_identities(
        self, project_id: str, identities: dict[str, str]
    ) -> None:
        self.project_evidence._register_project_identities(
            project_id, identities
        )

    def _related_project_ids(self, project_id: str) -> list[str]:
        return self.project_evidence._related_project_ids(project_id)

    def _discovery_project_candidates(
        self,
        project_id: str,
        query_tokens: list[str],
        lexical: list[dict[str, Any]],
    ) -> list[str]:
        return self.project_evidence._discovery_project_candidates(
            project_id, query_tokens, lexical
        )

    def create_scope(
        self, project_id: str, name: str, path: str | None = None
    ) -> dict[str, Any]:
        return self.project_evidence.create_scope(project_id, name, path)

    def resolve_project(self, cwd: str) -> dict[str, Any]:
        return self.project_evidence.resolve_project(cwd)

    def start_session(
        self,
        project_id: str,
        client: str = "codex",
        scope_id: str | None = None,
        external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict[str, Any]:
        return self.project_evidence.start_session(
            project_id, client, scope_id, external_id, metadata
        )

    def end_session(
        self,
        session_id: str,
        summary: str | None = None,
        extract_candidates: bool = True,
    ) -> dict[str, Any]:
        return self.project_evidence.end_session(
            session_id, summary, extract_candidates
        )

    @staticmethod
    def _terms(text: str) -> set[str]:
        return {
            token
            for token in re.findall(
                r"[\w-]+", text.casefold(), flags=re.UNICODE
            )
            if len(token) > 1
        }

    @classmethod
    def _text_similarity(cls, left: str, right: str) -> float:
        a, b = cls._terms(left), cls._terms(right)
        return len(a & b) / len(a | b) if a and b else 0.0

    def extract_session_candidates(self, session_id: str) -> dict[str, Any]:
        return self.review.extract_session_candidates(session_id)

    def review_queue(self, project_id: str) -> list[dict[str, Any]]:
        return self.review.review_queue(project_id)

    def propose_correction(
        self,
        project_id: str,
        memory_id: str,
        content: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return self.review.propose_correction(
            project_id, memory_id, content, title
        )

    def review_candidate(
        self,
        memory_id: str,
        action: str,
        related_memory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return self.review.review_candidate(
            memory_id, action, related_memory_id, note
        )

    def record_event(
        self,
        project_id: str,
        kind: str,
        content: str,
        session_id: str | None = None,
        scope_id: str | None = None,
        source_uri: str | None = None,
        metadata: dict | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.project_evidence.record_event(
            project_id,
            kind,
            content,
            session_id,
            scope_id,
            source_uri,
            metadata,
            idempotency_key,
        )

    def create_investigation(
        self,
        project_id: str,
        question: str,
        reason: str,
        decision_to_inform: str,
        constraints: list[str] | None = None,
        initiator: str = "unknown",
        scope_id: str | None = None,
        investigation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.investigations.create_investigation(
            project_id,
            question,
            reason,
            decision_to_inform,
            constraints,
            initiator,
            scope_id,
            investigation_id,
            idempotency_key,
        )

    def record_source_analysis(
        self,
        investigation_id: str,
        source: dict[str, Any],
        claims: list[dict[str, Any]],
        session_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.investigations.record_source_analysis(
            investigation_id, source, claims, session_id, idempotency_key
        )

    def get_investigation(
        self, investigation_id: str, source_analysis_id: str | None = None
    ) -> dict[str, Any]:
        result = self.investigations.get_investigation(
            investigation_id, source_analysis_id
        )
        if not result:
            raise KeyError("investigation not found")
        return result

    def request_source_reinspection(
        self,
        source_analysis_id: str,
        reason: str,
        details: str | None = None,
        known_source_version: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.investigations.request_source_reinspection(
            source_analysis_id,
            reason,
            details,
            known_source_version,
            idempotency_key,
        )

    def complete_investigation(self, investigation_id: str) -> dict[str, Any]:
        return self.investigations.complete_investigation(investigation_id)

    def create_wiki_page(
        self,
        project_id: str,
        topic: str,
        title: str,
        scope_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.wiki.create_wiki_page(
            project_id, topic, title, scope_id, idempotency_key
        )

    def set_wiki_notes(
        self, page_id: str, manual_notes: str
    ) -> dict[str, Any]:
        return self.wiki.set_wiki_notes(page_id, manual_notes)

    @staticmethod
    def _wiki_sections(brief: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_position": brief["current_decisions"],
            "why_it_exists": brief["rationale"],
            "governing_constraints": brief["constraints"],
            "considered_alternatives": brief["alternatives"],
            "trade_offs": brief["expected_vs_observed"],
            "decision_timeline": brief["history"],
            "observed_outcomes": brief["outcomes"],
            "open_questions": brief["open_questions"],
        }

    def generate_wiki_revision(
        self,
        page_id: str,
        question: str,
        char_budget: int = 6000,
        generation_metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.wiki.generate_wiki_revision(
            page_id,
            question,
            char_budget,
            generation_metadata,
            idempotency_key,
        )

    @staticmethod
    def _wiki_revision_result(
        item: dict[str, Any], citations: list[tuple[str, int, str, str]]
    ) -> dict[str, Any]:
        result = dict(item)
        result["sections"] = json.loads(result.pop("sections_json"))
        result["generation"] = json.loads(result.pop("generation_json"))
        result["citations"] = [
            {"section": s, "ordinal": o, "memory_id": m, "event_id": e}
            for s, o, m, e in sorted(citations)
        ]
        return result

    def transition_wiki_revision(
        self, revision_id: str, status: str, reason: str = ""
    ) -> dict[str, Any]:
        return self.wiki.transition_wiki_revision(revision_id, status, reason)

    def get_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.get_wiki_revision(revision_id)

    def lint_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.lint_wiki_revision(revision_id)

    def get_wiki_page(self, page_id: str) -> dict[str, Any]:
        return self.wiki.get_wiki_page(page_id)

    def browse_wiki(
        self,
        project_id: str,
        page_id: str | None = None,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.wiki.browse_wiki(
            project_id, page_id, scope_id, limit, offset
        )

    def render_wiki_revision(self, revision_id: str) -> dict[str, Any]:
        return self.wiki.render_wiki_revision(revision_id)

    def export_wiki_markdown(
        self,
        project_id: str,
        scope_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self.wiki.export_wiki_markdown(
            project_id, scope_id, limit, offset
        )

    def _stale_wiki_revisions_for_memory(
        self, cx: sqlite3.Connection, memory_id: str, reason: str
    ) -> list[str]:
        return self.wiki._stale_wiki_revisions_for_memory(
            cx, memory_id, reason
        )

    @staticmethod
    def _add_promotion_advisory(item: dict[str, Any]) -> None:
        if not item.get("session_id"):
            return
        eligible = item.get("kind") in PROMOTABLE_EVENT_KINDS
        item["promotion"] = {
            "eligible": eligible,
            "automatic_at_session_end": eligible,
            "promotable_kinds": list(PROMOTABLE_EVENT_KINDS),
        }
        if not eligible:
            item["promotion"]["warning"] = (
                "Event kind"
                f" '{item.get('kind')}'"
                " is preserved as immutable evidence but is not automatically"
                " converted to a proposed memory at session_end. Record new"
                " evidence with a promotable kind if a memory candidate is"
                " intended; do not rewrite this event."
            )

    def create_checkpoint(
        self,
        project_id: str,
        mode: str,
        reason: str,
        goal: str,
        idempotency_key: str,
        session_id: str | None = None,
        scope_id: str | None = None,
        completed: list[str] | None = None,
        next_step: str | None = None,
        blockers: list[str] | None = None,
        source_event_cursor: int | None = None,
        context_usage: float | None = None,
        repository_path: str | None = None,
        test_results: list[dict[str, Any]] | None = None,
        verified_event_ids: list[str] | None = None,
        handoff_title: str | None = None,
        handoff_content: str | None = None,
        previous_handoff_memory_id: str | None = None,
        commit: str | None = None,
    ) -> dict[str, Any]:
        return self.checkpoints.create_checkpoint(
            project_id,
            mode,
            reason,
            goal,
            idempotency_key,
            session_id,
            scope_id,
            completed,
            next_step,
            blockers,
            source_event_cursor,
            context_usage,
            repository_path,
            test_results,
            verified_event_ids,
            handoff_title,
            handoff_content,
            previous_handoff_memory_id,
            commit,
        )

    def evaluate_checkpoint(
        self,
        project_id: str,
        context_usage: float | None = None,
        session_id: str | None = None,
        repository_path: str | None = None,
        goal: str = "",
        completed: list[str] | None = None,
        next_step: str | None = None,
        blockers: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.checkpoints.evaluate_checkpoint(
            project_id,
            context_usage,
            session_id,
            repository_path,
            goal,
            completed,
            next_step,
            blockers,
        )

    def _checkpoint_recovery_hash(
        self,
        project_id: str,
        cursor: int,
        goal: str,
        completed: list[str],
        next_step: str | None,
        blockers: list[str],
        repository: dict[str, Any] | None,
    ) -> str:
        return self.checkpoints.recovery_hash(
            project_id,
            cursor,
            goal,
            completed,
            next_step,
            blockers,
            repository,
        )

    @staticmethod
    def _normalize_test_results(
        results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return normalize_test_results(results)

    @staticmethod
    def _repository_facts(path: str) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("repository_path must be an existing directory")

        def git(*args: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise ValueError(
                    "repository_path must identify a Git worktree"
                )
            return completed.stdout.rstrip("\n")

        top_level = str(Path(git("rev-parse", "--show-toplevel")).resolve())
        head = git("rev-parse", "HEAD")
        branch_value = (
            git("symbolic-ref", "--quiet", "--short", "HEAD")
            if subprocess.run(
                ["git", "-C", str(root), "symbolic-ref", "--quiet", "HEAD"],
                capture_output=True,
            ).returncode
            == 0
            else None
        )
        changed = []
        entries = git(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        ).split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            if not entry:
                break
            status, path_value = entry[:2], entry[3:]
            changed.append({"path": path_value, "status": status})
            index += 2 if "R" in status or "C" in status else 1
        return {
            "root": top_level,
            "head": head,
            "branch": branch_value,
            "dirty": bool(changed),
            "changed_files": changed,
        }

    def read_events_since(
        self,
        project_id: str,
        cursor: int = 0,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.project_evidence.read_events_since(
            project_id, cursor, kinds, scope_id, limit
        )

    @staticmethod
    def _receipt_stream(
        kinds: list[str] | None, scope_id: str | None
    ) -> tuple[str, str, list[str] | None]:
        return ProjectEvidenceRepository._receipt_stream(kinds, scope_id)

    def poll_events(
        self,
        project_id: str,
        consumer_id: str,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.project_evidence.poll_events(
            project_id, consumer_id, kinds, scope_id, limit
        )

    def acknowledge_events(
        self,
        project_id: str,
        consumer_id: str,
        cursor: int,
        kinds: list[str] | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        return self.project_evidence.acknowledge_events(
            project_id, consumer_id, cursor, kinds, scope_id
        )

    def upsert_memory(
        self,
        project_id: str,
        title: str,
        content: str,
        memory_type: str = "other",
        status: str = "proposed",
        confidence: float = 0.5,
        importance: float = 0.5,
        scope_id: str | None = None,
        source_event_ids: list[str] | None = None,
        valid_from: str | None = None,
        valid_until: str | None = None,
        tags: list[str] | None = None,
        observed_at: str | None = None,
        last_confirmed_at: str | None = None,
        visibility: str | None = None,
        memory_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self.memories.upsert_memory(
            project_id,
            title,
            content,
            memory_type,
            status,
            confidence,
            importance,
            scope_id,
            source_event_ids,
            valid_from,
            valid_until,
            tags,
            observed_at,
            last_confirmed_at,
            visibility,
            memory_id,
            idempotency_key,
        )

    def _provider_name(self) -> str | None:
        return self.memories.provider_name()

    def _index_embedding(
        self, cx: sqlite3.Connection, memory: dict[str, Any]
    ) -> None:
        self.memories.index_embedding(cx, memory)

    def transition(
        self,
        memory_id: str,
        status: str,
        related_memory_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        return self.memories.transition(
            memory_id, status, related_memory_id, note
        )

    def set_search_aliases(
        self, project_id: str, term: str, aliases: list[str]
    ) -> dict[str, Any]:
        return self.memories.set_search_aliases(project_id, term, aliases)

    def list_search_aliases(self, project_id: str) -> list[dict[str, Any]]:
        return self.memories.list_search_aliases(project_id)

    def create_relation(
        self,
        project_id: str,
        from_memory_id: str,
        to_memory_id: str,
        relation: str,
        note: str = "",
    ) -> dict[str, Any]:
        return self.memories.create_relation(
            project_id,
            from_memory_id,
            to_memory_id,
            relation,
            note,
        )

    def traverse(
        self,
        project_id: str,
        memory_id: str,
        max_depth: int = 2,
        direction: str = "both",
        relations: list[str] | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.memories.traverse(
            project_id,
            memory_id,
            max_depth,
            direction,
            relations,
            statuses,
        )

    def search(
        self,
        project_id: str,
        query: str,
        limit: int = 10,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        discover_projects: bool = False,
    ) -> list[dict[str, Any]]:
        return self.retrieval_repository.search(
            project_id, query, limit, statuses, scope_id, discover_projects
        )

    def _aggregate_project_candidates(
        self, memories: list[dict[str, Any]], current_project_id: str
    ) -> list[dict[str, Any]]:
        return self.retrieval_repository._aggregate_project_candidates(
            memories, current_project_id
        )

    @staticmethod
    def _select_project_candidate(
        candidates: list[dict[str, Any]],
    ) -> tuple[str | None, str, float]:
        return select_project_candidate(candidates)

    def record_memory_feedback(
        self, memory_id: str, signal: str
    ) -> dict[str, Any]:
        return self.memories.record_feedback(memory_id, signal)

    @staticmethod
    def _retrieval_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        return retrieval_gate(candidates)

    def get_context(
        self,
        project_id: str,
        query: str,
        char_budget: int = 6000,
        statuses: list[str] | None = None,
        scope_id: str | None = None,
        event_cursor: int | None = None,
        event_kinds: list[str] | None = None,
        event_limit: int = 20,
        event_char_budget: int = 2000,
        discover_projects: bool = True,
        response_format: str = "legacy",
    ) -> dict[str, Any]:
        return self.context_assembler.get_context(
            project_id,
            query,
            char_budget,
            statuses,
            scope_id,
            event_cursor,
            event_kinds,
            event_limit,
            event_char_budget,
            discover_projects,
            response_format,
        )

    def decision_context(
        self,
        project_id: str,
        question: str,
        char_budget: int = 6000,
        scope_id: str | None = None,
        discover_projects: bool = True,
    ) -> dict[str, Any]:
        return self.decision_assembler.decision_context(
            project_id,
            question,
            char_budget,
            scope_id,
            discover_projects,
        )

    def _rerank_decision_candidates(
        self, question: str, memories: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.decision_assembler._rerank_decision_candidates(
            question, memories
        )

    def _expand_decision_seeds(
        self, project_id: str, context: dict[str, Any], scope_id: str | None
    ) -> None:
        self.decision_assembler._expand_decision_seeds(
            project_id, context, scope_id
        )

    def _decision_outcome_comparisons(
        self, retrieved_memory_ids: list[str]
    ) -> list[dict[str, Any]]:
        return self.decision_assembler._decision_outcome_comparisons(
            retrieved_memory_ids
        )

    def get_policy(self, project_id: str) -> dict[str, Any]:
        item = self.maintenance.get_policy(project_id)
        if not item:
            raise KeyError("project not found")
        return item

    def set_policy(
        self,
        project_id: str,
        max_context_chars: int | None = None,
        max_context_items: int | None = None,
        audit_keep_entries: int | None = None,
        terminal_memory_days: int | None = None,
        checkpoint_soft_usage: float | None = None,
        checkpoint_hard_usage: float | None = None,
        checkpoint_elapsed_seconds: int | None = None,
        checkpoint_event_count: int | None = None,
        checkpoint_max_age_seconds: int | None = None,
        checkpoint_cooldown_seconds: int | None = None,
        checkpoint_hysteresis: float | None = None,
        maintenance_interval_seconds: int | None = None,
        message_ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        return self.maintenance.set_policy(
            project_id,
            max_context_chars,
            max_context_items,
            audit_keep_entries,
            terminal_memory_days,
            checkpoint_soft_usage,
            checkpoint_hard_usage,
            checkpoint_elapsed_seconds,
            checkpoint_event_count,
            checkpoint_max_age_seconds,
            checkpoint_cooldown_seconds,
            checkpoint_hysteresis,
            maintenance_interval_seconds,
            message_ttl_seconds,
        )

    def search_health(self, project_id: str) -> dict[str, Any]:
        return self.maintenance.search_health(project_id)

    def get_source(self, event_id: str) -> dict[str, Any]:
        item = self.project_evidence.get_event(event_id)
        if not item:
            raise KeyError("source event not found")
        return item

    def maintain(self, project_id: str, apply: bool = False) -> dict[str, Any]:
        return self.maintenance.maintain(project_id, apply)

    def maintenance_status(self, project_id: str) -> dict[str, Any]:
        return self.maintenance.status(project_id)

    def export_audit_chain(self, project_id: str) -> dict[str, Any]:
        return self.maintenance.export_audit_chain(project_id)

    @staticmethod
    def verify_audit_chain(
        bundle: dict[str, Any], expected_head_digest: str | None = None
    ) -> dict[str, Any]:
        """Verify an exported chain without opening its source database.

        Checkpoint digests commit to compacted audit rows that are
        intentionally absent. A separately recorded head
        digest anchors the exported chain and
        detects replacement of the bundle as a whole.
        """
        return verify_audit_chain_bundle(bundle, expected_head_digest)

    def maintain_scheduled(self, project_id: str) -> dict[str, Any]:
        return self.maintenance.maintain_scheduled(project_id)

    def backup_to(
        self, output_path: str | Path, encryption_passphrase: str | None = None
    ) -> dict[str, Any]:
        return self.operations.backup_to(output_path, encryption_passphrase)

    def export_project(self, project_id: str) -> list[dict[str, Any]]:
        return self.transfer.export_project(project_id)

    def import_project(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self.transfer.import_project(records)

    def rebuild_fts(self, project_id: str | None = None) -> dict[str, Any]:
        return self.operations.rebuild_fts(project_id)

    def audit(self, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        return self.project_evidence.audit_entries(entity_type, entity_id)
