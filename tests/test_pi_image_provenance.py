import importlib.metadata
import json
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
PREFLIGHT = ROOT / "docker/sandbox/pi/python-test-preflight"
PREFLIGHT_API = runpy.run_path(str(PREFLIGHT))
VALIDATE_BUILD_CONTRACT = PREFLIGHT_API["validate_build_contract"]
VERSION_FAILURES = PREFLIGHT_API["version_failures"]


def test_local_build_uses_only_the_explicit_non_release_fallback():
    assert (
        VALIDATE_BUILD_CONTRACT(
            build_mode="development",
            version="0.0.0+local",
            release_tag="local",
            revision="unknown",
        )
        == []
    )
    assert VALIDATE_BUILD_CONTRACT(
        build_mode="development",
        version="0.3.37",
        release_tag="v0.3.37",
        revision="a" * 40,
    ) == [
        "development builds must use the explicit provenance fallback "
        "version=0.0.0+local tag=local revision=unknown"
    ]


@pytest.mark.parametrize(
    ("version", "release_tag", "revision"),
    [
        ("", "v0.3.37", "a" * 40),
        ("0.0.0", "v0.0.0", "a" * 40),
        ("0.3.37.dev1", "v0.3.37.dev1", "a" * 40),
        ("0.3.37+local", "v0.3.37+local", "a" * 40),
        ("0.3.37", "v0.3.38", "a" * 40),
        ("0.3.37", "v0.3.37", "short"),
    ],
)
def test_release_build_rejects_ambiguous_or_mismatched_provenance(
    version, release_tag, revision
):
    assert VALIDATE_BUILD_CONTRACT(
        build_mode="release",
        version=version,
        release_tag=release_tag,
        revision=revision,
    )


def test_release_build_accepts_exact_semver_tag_and_commit():
    assert (
        VALIDATE_BUILD_CONTRACT(
            build_mode="release",
            version="0.3.37",
            release_tag="v0.3.37",
            revision="0123456789abcdef0123456789abcdef01234567",
        )
        == []
    )


def test_preflight_joins_expected_baked_and_installed_versions(tmp_path, monkeypatch):
    provenance = tmp_path / "image-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "build_mode": "release",
                "version": "0.3.37",
                "tag": "v0.3.37",
                "revision": "0123456789abcdef0123456789abcdef01234567",
            }
        )
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.3.37")
    assert VERSION_FAILURES(expected_version="0.3.37", provenance_file=provenance) == []

    failures = VERSION_FAILURES(expected_version="0.3.38", provenance_file=provenance)
    assert "expected skharness version '0.3.38', image records '0.3.37'" in failures
    assert "installed skharness version '0.3.37', expected '0.3.38'" in failures


def test_dockerfile_binds_package_record_and_labels_to_one_contract():
    dockerfile = (ROOT / "docker/sandbox/pi/Dockerfile").read_text()
    assert "ARG SKHARNESS_BUILD_MODE=development" in dockerfile
    assert "ARG SKHARNESS_VERSION=0.0.0+local" in dockerfile
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=${SKHARNESS_VERSION}" in dockerfile
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKHARNESS=0.0.0" not in dockerfile
    assert 'org.opencontainers.image.version="${SKHARNESS_VERSION}"' in dockerfile
    assert 'org.opencontainers.image.ref.name="${SKHARNESS_RELEASE_TAG}"' in dockerfile
    assert 'org.opencontainers.image.revision="${SKHARNESS_REVISION}"' in dockerfile
    assert "/opt/skharness/pi/image-provenance.json" in dockerfile
    assert "--validate-build-contract" in dockerfile


def test_tag_workflow_passes_exact_release_contract_to_every_target():
    workflow = (ROOT / ".github/workflows/pi-image.yml").read_text()
    assert "fetch-depth: 0" in workflow and "fetch-tags: true" in workflow
    assert "refs/tags/*" in workflow
    assert "tagged_commit" in workflow and '!= "$GITHUB_SHA"' in workflow
    assert "SKHARNESS_BUILD_MODE=release" in workflow
    assert "SKHARNESS_VERSION=${{ steps.release.outputs.version }}" in workflow
    assert "SKHARNESS_RELEASE_TAG=${{ steps.release.outputs.tag }}" in workflow
    assert "SKHARNESS_REVISION=${{ steps.release.outputs.revision }}" in workflow
    assert '"$IMAGE@$DIGEST" --expected-version "$VERSION"' in workflow
