"""Research investigation persistence and read-model assembly."""

import json
import sqlite3
from typing import Any


class InvestigationRepository:
    """Own investigation provenance read queries."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get_investigation(
        self, investigation_id: str, source_analysis_id: str | None = None
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM investigations WHERE id=?", (investigation_id,)
        ).fetchone()
        if not row:
            return None
        investigation = dict(row)
        investigation["constraints"] = json.loads(
            investigation.pop("constraints_json")
        )
        condition, arguments = (
            (" AND id=?", (investigation_id, source_analysis_id))
            if source_analysis_id
            else ("", (investigation_id,))
        )
        analyses = []
        for analysis_row in self.connection.execute(
            "SELECT * FROM source_analyses WHERE investigation_id=?"
            + condition
            + " ORDER BY created_at,id",
            arguments,
        ):
            analysis = dict(analysis_row)
            analysis["reinspection_requests"] = [
                dict(item)
                for item in self.connection.execute(
                    "SELECT * FROM source_reinspection_requests WHERE"
                    " source_analysis_id=? ORDER BY requested_at,id",
                    (analysis["id"],),
                )
            ]
            analysis["claims"] = []
            for claim_row in self.connection.execute(
                "SELECT * FROM investigation_claims WHERE"
                " source_analysis_id=? ORDER BY ordinal",
                (analysis["id"],),
            ):
                claim = dict(claim_row)
                claim["evidence"] = [
                    dict(link)
                    for link in self.connection.execute(
                        """SELECT l.relation,c.source_analysis_id,
                    c.claim_key,c.event_id,c.memory_id
                    FROM investigation_claim_links l
                    JOIN investigation_claims c ON c.id=l.from_claim_id
                    WHERE l.to_claim_id=? ORDER BY c.claim_key""",
                        (claim["id"],),
                    )
                ]
                analysis["claims"].append(claim)
            analyses.append(analysis)
        return {
            "contract_version": "research-provenance/v1",
            "investigation": investigation,
            "source_analyses": analyses,
            "idempotent": source_analysis_id is not None,
        }
