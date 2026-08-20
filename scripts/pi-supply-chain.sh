#!/usr/bin/env bash
# Generate local supply-chain evidence. This does not claim signing unless a key is supplied.
set -euo pipefail

image="${1:?usage: pi-supply-chain.sh IMAGE OUTPUT_DIR [COSIGN_KEY]}"
output_dir="${2:?usage: pi-supply-chain.sh IMAGE OUTPUT_DIR [COSIGN_KEY]}"
signing_key="${3:-}"

for tool in docker syft grype; do
  command -v "$tool" >/dev/null || { echo "missing required tool: $tool" >&2; exit 127; }
done
mkdir -p "$output_dir"
image_id="$(docker image inspect "$image" --format '{{.Id}}')"
printf '%s\n' "$image_id" >"$output_dir/image-id.txt"
syft "$image" -o cyclonedx-json="$output_dir/sbom.cdx.json"
grype "sbom:$output_dir/sbom.cdx.json" -o json >"$output_dir/vulnerabilities.json"
python - "$image" "$image_id" >"$output_dir/provenance.json" <<'PY'
import json, sys
print(json.dumps({
    "_type": "https://in-toto.io/Statement/v1",
    "subject": [{"name": sys.argv[1], "digest": {"docker-image-id": sys.argv[2]}}],
    "predicateType": "https://slsa.dev/provenance/v1",
    "predicate": {"buildType": "skharness.local-docker", "signed": False},
}, sort_keys=True, indent=2))
PY
if [[ -n "$signing_key" ]]; then
  command -v cosign >/dev/null || { echo "missing required tool: cosign" >&2; exit 127; }
  cosign sign-blob --key "$signing_key" --bundle "$output_dir/provenance.sigstore.json" \
    "$output_dir/provenance.json"
fi
