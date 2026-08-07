# sandbox-claude-flutter:1

A Flutter-capable autocode sandbox image, used by autopilot to build+grade the
`skworld-app` (Flutter) repo. It extends `sandbox-claude:1` (which only carries
node + python/pytest + the claude CLI) with everything needed to run
`flutter test` **fully offline** inside the hardened sandbox.

## Why this exists

The autocode sandbox is deliberately hardened (see `../../src/skharness/autocode/sandbox.py`):

- `--read-only` root filesystem
- `HOME=/home/sbx` is an **empty tmpfs** at runtime (anything baked there is masked)
- runs as the **host uid** (not the image's `sbx` user)
- `--cap-drop ALL`, `--no-new-privileges` (no overlay/bind tricks available)
- egress only to an allowlist via a proxy sidecar (pub.dev is **not** allowlisted)

Flutter fights every one of those. This image resolves each, so grading works
with `--network none`:

| Obstacle | Fix |
|----------|-----|
| `flutter test` grade gate, no flutter in base image | bake a precached Flutter SDK at `/opt/flutter` |
| pub.dev blocked by egress allowlist | bake a warm `~/.pub-cache` at `/opt/pub-cache`, `PUB_OFFLINE=true` |
| `update_engine_version.sh` writes `bin/cache/engine.stamp` on read-only rootfs | `bin/cache` is an anon `VOLUME` (writable + prepopulated even under `--read-only`) |
| `bin/cache` files owned by root, container runs as host uid | `chmod -R a+rwX bin/cache` before the VOLUME so any uid can write |
| first `flutter pub get` re-resolves flutter_tools (its baked package_config points at the host `~/.pub-cache` path) and writes the read-only SDK | re-resolve flutter_tools against `/opt/pub-cache` **at build time** |
| app path-dep `../sk-pqc-dart` absent (only the single repo worktree is mounted at `/work`) | bake the sibling at `/sk-pqc-dart` (== `/work/../sk-pqc-dart`) |
| pub writes `$PUB_CACHE/active_roots` | make just that dir an anon `VOLUME` (keeps the 1.6G packages read-only) |
| `flutter test` runs an implicit online pub get (advisory check) | grade with `flutter test --no-pub` after an explicit `flutter pub get --offline` |

## Wiring (autopilot.yaml)

```yaml
  skworld-app:
    sandbox_image: sandbox-claude-flutter:1
    test_cmd: "flutter pub get --offline >/dev/null 2>&1; flutter test --no-pub"
    coverage_cmd: "flutter pub get --offline >/dev/null 2>&1; flutter test --no-pub --coverage"
```

`RepoSpec.sandbox_image` overrides the global `sandbox_image` per repo
(`adapters/base.py`: `getattr(repo, "sandbox_image", None) or self._image()`).

## Rebuild

`bash build.sh` (see its header for prereqs: `sandbox-claude:1`, a precached
`~/flutter`, and a warm `~/.pub-cache` completed for skworld-app @ origin/main
from a worktree placed as a sibling of `sk-pqc-dart`).

## Known rough edge

`flutter pub get --offline` may report "Changed N dependencies" and rewrite
`pubspec.lock` when the warm cache holds newer/older versions than the committed
lock. That churn can show up in a card's diff; re-warm the cache against the
current lock, or have the card's grade revert `pubspec.lock` if it did not
intend to change deps.
