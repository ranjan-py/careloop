#!/usr/bin/env bash
# CareLoop — dockerized Synthea cohort generation (spec section 6.1).
#
# Generates a small synthetic cohort biased toward hypertension + type 2
# diabetes (male, 55-65) and exports FHIR R4 bundles to data/synthea/output/.
# Then run scripts/select_patient.py to pick the best candidate bundle, and
# scripts/scenario_overlay.py to apply the deterministic John Miller overlay.
#
# The scripted demo beats NEVER come from raw Synthea output — only from the
# overlay. If this script is blocked (no network, slow jar download), skip it:
#   python3 scripts/scenario_overlay.py --fallback
# is the spec-sanctioned fallback (spec 6.1).
#
# SYNTHETIC DATA ONLY. No real PHI is involved at any point.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNTHEA_DIR="${REPO_ROOT}/data/synthea"
OUTPUT_DIR="${SYNTHEA_DIR}/output"
JAR="${SYNTHEA_DIR}/synthea-with-dependencies.jar"

# Pinned upstream artifact (Synthea's continuously-published master build).
JAR_URL="https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar"

JAVA_IMAGE="${SYNTHEA_JAVA_IMAGE:-eclipse-temurin:21-jre}"
POPULATION="${SYNTHEA_POPULATION:-25}"   # small cohort — we only need one good candidate
SEED="${SYNTHEA_SEED:-4242}"             # deterministic across runs

mkdir -p "${SYNTHEA_DIR}" "${OUTPUT_DIR}"

if [[ ! -f "${JAR}" ]]; then
  echo ">> Downloading Synthea jar (~200 MB, one-time) to ${JAR}"
  curl -fSL --retry 3 -o "${JAR}.tmp" "${JAR_URL}"
  mv "${JAR}.tmp" "${JAR}"
else
  echo ">> Using cached Synthea jar at ${JAR}"
fi

echo ">> Running Synthea in Docker (${JAVA_IMAGE}) — population ${POPULATION}, male 55-65"
# Module filter biases the cohort toward the conditions we need:
#   hypertension*  -> essential hypertension module
#   metabolic*     -> metabolic syndrome modules (type 2 diabetes progression)
# Colon-separated per Synthea's -m module-filter syntax; core modules always run.
docker run --rm \
  -v "${SYNTHEA_DIR}:/synthea" \
  -w /synthea \
  "${JAVA_IMAGE}" \
  java -jar synthea-with-dependencies.jar \
    -s "${SEED}" \
    -p "${POPULATION}" \
    -g M \
    -a 55-65 \
    -m "hypertension*:metabolic*" \
    --exporter.baseDirectory /synthea/output \
    --exporter.fhir.export true \
    --exporter.hospital.fhir.export false \
    --exporter.practitioner.fhir.export false \
    --exporter.csv.export false \
    --exporter.years_of_history 10 \
    --generate.only_alive_patients true \
    Massachusetts

COUNT="$(find "${OUTPUT_DIR}/fhir" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')"
echo ">> Synthea export complete: ${COUNT} FHIR R4 bundle(s) in ${OUTPUT_DIR}/fhir"
echo ">> Next steps:"
echo "   python3 scripts/select_patient.py"
echo "   python3 scripts/scenario_overlay.py --base data/synthea/selected_bundle.json"
