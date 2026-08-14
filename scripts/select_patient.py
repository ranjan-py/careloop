#!/usr/bin/env python3
"""CareLoop — Synthea candidate selector (spec section 6.1).

Scans data/synthea/output/fhir/*.json (Synthea FHIR R4 patient bundles) and
picks the best base-chart candidate for the John Miller overlay.

Scoring (higher is better):
  +4  hypertension Condition present
  +4  type 2 diabetes Condition present
  +3  lisinopril MedicationRequest present
  +3  metformin MedicationRequest present
  +1  male
  +1  age 55-65 at runtime

The winner is copied to data/synthea/selected_bundle.json, which
scripts/scenario_overlay.py consumes via --base. The overlay — not Synthea —
guarantees every scripted demo fact; this selector only finds a plausible
background chart.

Usage:
  python3 scripts/select_patient.py [--fhir-dir data/synthea/output/fhir] [--out data/synthea/selected_bundle.json]

SYNTHETIC DATA ONLY — Synthea output contains no real PHI.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FHIR_DIR = REPO_ROOT / "data" / "synthea" / "output" / "fhir"
DEFAULT_OUT = REPO_ROOT / "data" / "synthea" / "selected_bundle.json"

HTN_HINTS = ("hypertension",)
T2D_HINTS = ("type 2 diabetes", "diabetes mellitus type 2", "type ii diabetes")


def _resources(bundle: dict, rtype: str):
    for entry in bundle.get("entry", []):
        res = entry.get("resource", {})
        if res.get("resourceType") == rtype:
            yield res


def _concept_text(concept: dict) -> str:
    parts = [concept.get("text", "")]
    parts += [c.get("display", "") for c in concept.get("coding", [])]
    return " ".join(parts).lower()


def score_bundle(bundle: dict) -> tuple[int, dict]:
    detail = {
        "hypertension": False,
        "type_2_diabetes": False,
        "lisinopril": False,
        "metformin": False,
        "male": False,
        "age_55_65": False,
        "age": None,
        "name": None,
    }

    for cond in _resources(bundle, "Condition"):
        text = _concept_text(cond.get("code", {}))
        if any(h in text for h in HTN_HINTS):
            detail["hypertension"] = True
        if any(h in text for h in T2D_HINTS):
            detail["type_2_diabetes"] = True

    for med in _resources(bundle, "MedicationRequest"):
        text = _concept_text(med.get("medicationCodeableConcept", {}))
        if "lisinopril" in text:
            detail["lisinopril"] = True
        if "metformin" in text:
            detail["metformin"] = True

    for patient in _resources(bundle, "Patient"):
        detail["male"] = patient.get("gender") == "male"
        names = patient.get("name", [])
        if names:
            detail["name"] = " ".join(names[0].get("given", []) + [names[0].get("family", "")])
        birth = patient.get("birthDate")
        if birth:
            try:
                b = date.fromisoformat(birth)
                today = date.today()
                age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
                detail["age"] = age
                detail["age_55_65"] = 55 <= age <= 65
            except ValueError:
                pass
        break

    score = (
        4 * detail["hypertension"]
        + 4 * detail["type_2_diabetes"]
        + 3 * detail["lisinopril"]
        + 3 * detail["metformin"]
        + 1 * detail["male"]
        + 1 * detail["age_55_65"]
    )
    return score, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fhir-dir", type=Path, default=DEFAULT_FHIR_DIR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    if not args.fhir_dir.is_dir():
        print(f"ERROR: no Synthea FHIR output at {args.fhir_dir}", file=sys.stderr)
        print("Run scripts/generate_synthea.sh first, or skip Synthea entirely with:", file=sys.stderr)
        print("  python3 scripts/scenario_overlay.py --fallback", file=sys.stderr)
        return 2

    candidates = []
    for path in sorted(args.fhir_dir.glob("*.json")):
        # Synthea also writes hospitalInformation*/practitionerInformation* files.
        if path.name.startswith(("hospitalInformation", "practitionerInformation")):
            continue
        try:
            bundle = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  skip {path.name}: {exc}", file=sys.stderr)
            continue
        if bundle.get("resourceType") != "Bundle":
            continue
        score, detail = score_bundle(bundle)
        candidates.append((score, path, detail))

    if not candidates:
        print(f"ERROR: no parseable patient bundles in {args.fhir_dir}", file=sys.stderr)
        return 2

    candidates.sort(key=lambda t: (-t[0], t[1].name))
    print(f"Scored {len(candidates)} candidate bundle(s):")
    for score, path, detail in candidates[:10]:
        flags = ",".join(k for k in ("hypertension", "type_2_diabetes", "lisinopril", "metformin") if detail[k])
        print(f"  score={score:2d}  {path.name}  age={detail['age']}  [{flags or 'no target features'}]")

    best_score, best_path, best_detail = candidates[0]
    shutil.copyfile(best_path, args.out)
    print(f"\nSelected: {best_path.name} (score {best_score}) -> {args.out}")
    if not (best_detail["hypertension"] and best_detail["type_2_diabetes"]):
        print("WARNING: best candidate lacks HTN and/or T2D — the overlay will still guarantee "
              "every scripted fact, but consider a larger cohort (SYNTHEA_POPULATION=50).")
    print("Next: python3 scripts/scenario_overlay.py --base data/synthea/selected_bundle.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
