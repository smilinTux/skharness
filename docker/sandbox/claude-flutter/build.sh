#!/usr/bin/env bash
# Build sandbox-claude-flutter:1 = sandbox-claude:1 + precached Flutter SDK +
# warm pub-cache + the sk-pqc-dart sibling path-dep, so `flutter test` grades
# OFFLINE inside the hardened autocode sandbox.
#
# Prereqs on the build host:
#   - sandbox-claude:1 already built (docker/sandbox/claude/Dockerfile)
#   - a local Flutter SDK at $FLUTTER_ROOT (default ~/flutter), precached
#   - a warm pub-cache at ~/.pub-cache COMPLETE for skworld-app @ origin/main.
#     Warm it (once, online) from a worktree placed as a SIBLING of sk-pqc-dart:
#       cd ~/clawd/skcapstone-repos/skworld-app
#       git worktree add -f ../_app-warmcache origin/main
#       (cd ../_app-warmcache && ~/flutter/bin/flutter pub get)   # completes ~/.pub-cache
#       git worktree remove --force ../_app-warmcache
#   - the sk-pqc-dart repo checked out next to skworld-app
#
# The relative-path warm-up matters: skworld-app's pubspec has `path: ../sk-pqc-dart`,
# so pub only resolves when the worktree is a sibling of sk-pqc-dart.
set -euo pipefail

FLUTTER_ROOT="${FLUTTER_ROOT:-$HOME/flutter}"
PUB_CACHE_DIR="${PUB_CACHE_DIR:-$HOME/.pub-cache}"
SKPQC_DIR="${SKPQC_DIR:-$HOME/clawd/skcapstone-repos/sk-pqc-dart}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BD="${BUILD_DIR:-$HOME/flutter-sbx-build}"

echo "staging build context -> $BD"
rm -rf "$BD"; mkdir -p "$BD"
cp -al "$FLUTTER_ROOT"   "$BD/flutter"     2>/dev/null || rsync -a --exclude .git "$FLUTTER_ROOT/"   "$BD/flutter/"
cp -al "$PUB_CACHE_DIR"  "$BD/pub-cache"   2>/dev/null || rsync -a               "$PUB_CACHE_DIR/"  "$BD/pub-cache/"
cp -a  "$SKPQC_DIR"      "$BD/sk-pqc-dart"
rm -rf "$BD/sk-pqc-dart/.git" "$BD/sk-pqc-dart/.dart_tool" "$BD/flutter/.git"
cp "$HERE/Dockerfile" "$HERE/entrypoint.sh" "$BD/"

echo "building sandbox-claude-flutter:1"
cd "$BD" && docker build -t sandbox-claude-flutter:1 .
echo "done: sandbox-claude-flutter:1"
