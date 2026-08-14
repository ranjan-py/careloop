"""Output-degeneration guard (user-reported gpt-5-nano failure mode)."""

from app.ai.reports import _MIN_BODY_CHARS, sanitize_generated_text

CLEAN_DOC = (
    "Here's what to do next after today's visit:\n"
    "- Schedule a follow-up in 4-6 weeks to review your home blood pressure logs.\n"
    "- We will review your use of lisinopril and any side effects.\n"
    "- Start a simple daily blood pressure log.\n"
    "- We will order labs to recheck kidney function and diabetes control."
)

# The observed degeneration, verbatim structure: valid document, then brace
# runs, then self-referential apologies, then more braces and a dangling "{".
OBSERVED_GARBAGE = (
    CLEAN_DOC
    + '"} } } } } } } } } } } } } } } } } } } } } } } } } } } } } } } }'
    + "}}} } } } } } }}}}- continuous retry prevented by token limit. The "
    "content above is valid. The extraneous trailing braces are not intended; "
    "ensure final answer is clean JSON with two fields. Sorry for confusion. "
    "Here's the corrected output.} } } } } } } } } } } } } } } } } } "
    + "}}- The above output is garbled. I'll provide a clean JSON now. "
    "70 tokens.  Sorry.  Let's finalize: {"
)


class TestSanitizer:
    def test_clean_text_untouched(self):
        cleaned, issues = sanitize_generated_text(CLEAN_DOC)
        assert cleaned == CLEAN_DOC.strip()
        assert issues == []

    def test_observed_garbage_trimmed_to_document(self):
        cleaned, issues = sanitize_generated_text(OBSERVED_GARBAGE)
        assert issues, "degeneration must be detected"
        assert "kidney function and diabetes control" in cleaned
        assert "}" not in cleaned.split("diabetes control")[-1]
        assert "Sorry" not in cleaned
        assert "clean JSON" not in cleaned
        assert "garbled" not in cleaned
        assert len(cleaned) >= _MIN_BODY_CHARS

    def test_brace_run_detected(self):
        cleaned, issues = sanitize_generated_text("Take your meds daily. } } } } } } }")
        assert cleaned == "Take your meds daily."
        assert any("brace-run" in i for i in issues)

    def test_meta_marker_line_removed(self):
        text = "Do the labs this week.\nSorry for confusion. Here's the corrected output."
        cleaned, issues = sanitize_generated_text(text)
        assert cleaned == "Do the labs this week."
        assert any("meta-commentary" in i for i in issues)

    def test_isolated_braces_in_prose_survive(self):
        # A brace or two inside legitimate prose must not trigger the guard.
        text = "Your reading was 150/95 (see the log)."
        cleaned, issues = sanitize_generated_text(text)
        assert cleaned == text
        assert issues == []

    def test_destroyed_document_reports_short(self):
        cleaned, _ = sanitize_generated_text("} } } } } } } } } } {")
        assert len(cleaned) < _MIN_BODY_CHARS
