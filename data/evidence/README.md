# Evidence corpus (spec section 11 — retrieval layer)

**Synthetic demo data — generic, demo-authored guideline snippets. NOT real clinical
guidance and not copied from any real guideline. Not for patient care.**

`evidence_corpus.json` contains 30 snippets. Each snippet:

```json
{
  "id": "ev-001",                  // stable id — care-plan evidence_refs point here
  "title": "...",
  "body": "...",                    // ~80-130 words, generic content
  "topic_tags": ["hypertension"],  // used for retrieval/debugging
  "synthetic": true,
  "source_label": "Synthetic demo guideline — not for clinical use"
}
```

Topic coverage: hypertension management, ACE-inhibitor side effects (dizziness,
cough, angioedema), potassium monitoring on ACEi/ARB, CKD monitoring (eGFR/UACR),
type 2 diabetes basics, home vs pharmacy-kiosk BP measurement, medication
adherence, and follow-up intervals.

The backend retrieval layer (BM25 or small embeddings — spec section 11) indexes
`body` + `title` and returns snippet ids as `evidence_refs` on care-plan actions.
`evidence_refs` must be genuinely retrieval-produced, never decorative.
