# Flutter-capable autocode sandbox: sandbox-claude:1 + a precached Flutter SDK,
# a warm pub-cache, and the external sibling path-dep, so `flutter test` grades
# entirely OFFLINE inside the hardened sandbox (read-only rootfs, tmpfs $HOME,
# host uid, caps dropped, egress-proxied). Used by autopilot for the skworld-app
# repo (repo_map sandbox_image: sandbox-claude-flutter:1).
#
# Build context is assembled by build.sh (stages ./flutter, ./pub-cache,
# ./sk-pqc-dart, ./entrypoint.sh). See README.md for the why behind each layer.
FROM sandbox-claude:1
USER root
COPY flutter /opt/flutter
COPY pub-cache /opt/pub-cache
# The app declares a path dependency on ../sk-pqc-dart; the sandbox only mounts
# the single-repo worktree at /work, so ../sk-pqc-dart == /sk-pqc-dart. Bake it.
COPY sk-pqc-dart /sk-pqc-dart
COPY entrypoint.sh /usr/local/bin/flutter-entrypoint.sh
RUN chmod +x /usr/local/bin/flutter-entrypoint.sh \
    && git config --system --add safe.directory /opt/flutter \
    && chmod -R a+rX /opt/flutter /opt/pub-cache /sk-pqc-dart \
    && chmod -R a+rwX /opt/flutter/bin/cache
ENV FLUTTER_ROOT=/opt/flutter PUB_CACHE=/opt/pub-cache PUB_OFFLINE=true \
    PATH="/opt/flutter/bin:/opt/flutter/bin/cache/dart-sdk/bin:/usr/local/bin:/usr/bin:/bin"
# Re-resolve flutter_tools against the CONTAINER pub-cache path (/opt/pub-cache)
# at build time (writable rootfs here), so its baked package_config absolute
# paths are valid at runtime. Without this the first `flutter pub get` re-resolves
# flutter_tools and tries to write the read-only SDK, hanging the run.
RUN mkdir -p /tmp/bh \
    && cd /opt/flutter/packages/flutter_tools \
    && HOME=/tmp/bh dart pub get --offline 2>&1 | tail -2 \
    && HOME=/tmp/bh flutter --version 2>&1 | head -1 \
    && chmod -R a+rwX /opt/flutter/packages/flutter_tools/.dart_tool 2>/dev/null || true
# pub tracks project roots in $PUB_CACHE/active_roots (a write); keep the 1.6G
# packages read-only and make only this tiny dir writable via an anon VOLUME.
RUN mkdir -p /opt/pub-cache/active_roots && chmod -R a+rwX /opt/pub-cache/active_roots
# bin/cache holds the precached dart-sdk+engine AND the runtime lockfile/stamps
# flutter must write. Under --read-only rootfs an image VOLUME stays read-WRITE
# and is initialized (a+rwX) from this layer, so any host uid can write it.
VOLUME ["/opt/flutter/bin/cache", "/opt/pub-cache/active_roots"]
USER sbx
ENTRYPOINT ["/usr/local/bin/flutter-entrypoint.sh"]
