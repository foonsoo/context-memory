from context_memory.wiki_rendering import render_wiki_markdown


def test_render_wiki_markdown_formats_citations_and_empty_sections():
    revision = {
        "status": "published",
        "revision_no": 2,
        "sections": {
            "current_position": [
                {
                    "claim": "Keep SQLite authoritative.",
                    "citations": {
                        "memory_id": "memory-1",
                        "source_event_ids": ["event-1", "event-2"],
                    },
                }
            ],
            "observed_outcomes": [
                {
                    "observed_outcome": "Exports remain reproducible.",
                    "outcome_citation": {
                        "memory_id": "memory-2",
                        "source_event_ids": ["event-3"],
                    },
                }
            ],
        },
    }

    markdown = render_wiki_markdown("Storage", "Owner: platform", revision)

    assert markdown.startswith("# Storage\n\nStatus: published · Revision 2\n")
    assert (
        "- Keep SQLite authoritative. "
        "(memory:memory-1 events:event-1,event-2)" in markdown
    )
    assert (
        "- Exports remain reproducible. "
        "(memory:memory-2 events:event-3)" in markdown
    )
    assert "## Why it exists\n\n_No cited material._" in markdown
    assert markdown.endswith("## Manual notes\n\nOwner: platform\n")


def test_render_wiki_markdown_canonicalizes_fallback_and_empty_notes():
    revision = {
        "status": "proposed",
        "revision_no": 1,
        "sections": {"current_position": [{"z": 2, "a": "가"}]},
    }

    markdown = render_wiki_markdown("Fallback", None, revision)

    assert '- {"a":"가","z":2} ()' in markdown
    assert markdown.endswith("## Manual notes\n\n_None._\n")
