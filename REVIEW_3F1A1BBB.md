# Independent Review: SKHarness PR 70 (Card 3f1a1bbb)

**Reviewer**: pi-glm-chiap08-3f1a1bbb  
**PR**: https://github.com/smilinTux/skharness/pull/70  
**PR Head**: 7d8512eb3c22d3be793645e493f0fdc24b199c81  
**Origin/main**: 9cfdd0e9b84f54d26e5f09a33e3ff51a40c81979  
**Verdict**: PASS

## 1. Fresh Readback Pins (ACCEPTANCE CRITERION 1)

| Pin | Value | Status |
|-----|-------|--------|
| PR Head | 7d8512eb3c22d3be793645e493f0fdc24b199c81 | ✓ Verified |
| Origin/main | 9cfdd0e9b84f54d26e5f09a33e3ff51a40c81979 | ✓ Verified |
| Merge Base | c70003f30d934fb99f64074b0829ab13035d9650 | ✓ Verified |
| Base Tree | eee8f4ea1dad16c631db67e046aef7e779122207 | ✓ Verified |
| Candidate Tree | 70cf7226fd14a371183e9e1cd247159960c3b361 | ✓ Verified |
| Patch SHA256 | 9c5514141995f08f206f6e78e3456213b5f08a66180caf996b72a4e375fb0ced | ✓ Verified |

### Five-Path Manifest

1. CHANGELOG.md - 862b852f98cea393d24e3457f24fe08062836af139ae08a99686a7a6da203758
2. src/skharness/autocode/calibration.py - b45a33616b9772dd2c80ca6333d045851ee659f44b6f06db8a44e9764cfa298a
3. src/skharness/autocode/digest.py - 234580ee4415e4dc978368544f9fcc15a1dc9626d4c30b3160878ac67362b9bc
4. tests/test_autocode_calibration.py - d0707b3701bcc3f008110b224aa1972a7407a8d6631a015fafabf6eca888a30e
5. tests/test_autocode_escalation.py - a25b173d49ccfe7a8ff46d7c72b81c2247d7c254e2e3ac466c8113086fe4bc0b

### Hosted Check Conclusions

- GitGuardian Security Checks: pass
- build: pass
- compat-3-10: pass
- docs-check: pass
- gitleaks: pass
- lint: pass
- test: pass

## 2. Independent Diff Review (ACCEPTANCE CRITERION 2)

### Calibration is Reporting-Only

**Proven**: The calibration module (340 lines) is a pure reporting seam that:
- Imports ONLY Python's standard library (hashlib, json, os, datetime, pathlib, typing)
- Has NO imports from buckets, grading, sensitivity, engineering, resolver, golden_set, orchestrator
- Opens no files for writing (AST analysis confirms only mode="r" or None)
- Calls NO routing or gating functions (no bucket_for_payload, no twin_gate_passed, no queue_decision, no ratify_entry)
- Carries `authority: "report_only"` on ALL candidates
- Exposes only three human dispositions: promote, dismiss, defer

### Escalation Denylist Enforcement

**Proven**: The escalation denylist test (test_autocode_escalation.py) was extended to include:
- "calibration_candidate", "calibration_report" in the forbidden vocabulary
- "from .calibration", "from skharness.autocode.calibration", "import calibration"

**Verification**: No routing modules import calibration:
- buckets.py: NO MATCH
- grading.py: NO MATCH
- sensitivity.py: NO MATCH
- engineering.py: COMMENT ONLY (no import)
- resolver.py: NO MATCH
- harness.py: NO MATCH
- claude_code.py: NO MATCH
- direct.py: NO MATCH
- fleet_dispatch.py: NO MATCH
- sandbox_proxy.py: NO MATCH
- autoscale.py: NO MATCH

### Overgrade Inference Excluded

**Proven**: The module deliberately excludes overgrade candidates:
- Test `test_quick_large_model_pass_does_not_create_an_overgrade_candidate` confirms this
- Comments explain: "A first-round pass under a large model cannot distinguish an easy card from a card that the large model made look easy. That confound is unresolved, so treating a quick pass as an overgrade signal would manufacture evidence."

### Backlog Count and Age Exposed

**Proven**: The digest line includes:
- `backlog_count`: Total candidates in queue
- `shown_count`: Number shown in digest (default 3)
- `oldest_days`: Age of oldest candidate
- Format: "showing 3 of 14, oldest 41 days [promote/dismiss/defer]."

### Sensitivity Overrides Captured

**Proven**: Every `meta.sensitivity_override` is captured as a review candidate:
- Invalid overrides are sanitized to "invalid" (never leak arbitrary sensitive strings)
- Override values are NOT rendered in the digest line (only card_id is used)
- Candidates carry `authority: "report_only"`

### Cannot Route or Gate

**Proven**: Multiple tests confirm:
- `test_reporting_module_has_no_writer_gate_or_route_import` - AST analysis passes
- `test_calibration_payload_cannot_change_dispatch_or_gate_result` - EngineeringExecutor dispatch is unchanged with calibration payload
- `test_no_routing_module_mentions_the_escalation_vocabulary` - No routing module mentions calibration vocabulary

## 3. Focused Checks (ACCEPTANCE CRITERION 3)

| Check | Result |
|-------|--------|
| calibration (9 tests) | 9 passed |
| digest (5 selected tests) | 5 passed, 2 deselected |
| escalation (denylist) | 37 passed |
| engineering/outcome/run_record | 94 passed |
| Ruff lint | All checks passed |
| Git diff --check | passed |
| Python compilation | OK |

## 4. Verdict (ACCEPTANCE CRITERION 4)

**PASS**

The calibration module is a pure reporting seam that:
1. Reads only from existing retry sinks (health events, outcome rows, RunRecords)
2. Generates only undergrade candidates (overgrade deliberately excluded)
3. Captures sensitivity overrides without interpretation
4. Exposes backlog count and age in the numbered digest
5. Cannot influence routing or gating (enforced by imports denylist and tests)
6. Marks all candidates as `authority: "report_only"`
7. Supports only three human dispositions: promote, dismiss, defer

## Immutable Evidence

- PR: https://github.com/smilinTux/skharness/pull/70
- Evidence published to: ~/.skcapstone/evidence/work/3f1a1bbb/
- Patch SHA256: 9c5514141995f08f206f6e78e3456213b5f08a66180caf996b72a4e375fb0ced

## Rollback

Revert commit `012260f5fd9a4ecd996d25a3689aa96c494aaa94`. This removes only the reporting module, digest line, and boundary tests. It does not alter routing, gates, rubric data, live flags, or runtime state.

## Known Limitations

- Black formatting check indicates style differences, but this does not affect functionality or safety boundaries

## Compliance Statement

- No merge, push, install, live_execution, automerge, model request, service, credential, protected data, deployment, or external action performed.
- Work published to durable shared location for independent verification.
- Review only. No modifications made to the candidate.
