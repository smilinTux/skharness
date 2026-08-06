#!/bin/sh
# Sandbox is hardened: read-only rootfs, HOME=/home/sbx is an EMPTY tmpfs, runs as
# host uid. Point flutter + pub at the baked read-only trees; give flutter a
# writable config/analytics home on the tmpfs. Fully precached + PUB_OFFLINE means
# no writes to the SDK/cache are needed for `flutter pub get`/`flutter test`.
export FLUTTER_ROOT=/opt/flutter
export PUB_CACHE=/opt/pub-cache
export PATH="/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:${PATH}"
export PUB_OFFLINE=true
export FLUTTER_SUPPRESS_ANALYTICS=true
export CI=true
mkdir -p "$HOME/.config/flutter" "$HOME/.dart" "$HOME/.dart-tool" 2>/dev/null || true
exec "$@"
