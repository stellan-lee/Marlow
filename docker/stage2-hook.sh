#!/bin/sh
# s6-overlay stage2 hook — runs as root after the supervision tree is
# up but before user services start. Handles UID/GID remap, volume
# chown, and config seeding.
#
# Per-service privilege drop happens inside each service's `run` script
# (and in main-wrapper.sh) via s6-setuidgid, not here.
#
# Wired into the image as /etc/cont-init.d/01-marlow-setup by the
# Dockerfile. The shim at docker/entrypoint.sh forwards to this script
# so external references to docker/entrypoint.sh still work.
#
# NB: cont-init.d scripts run with no arguments — the user's CMD args
# are NOT visible here. That's fine: we use Architecture B (s6-overlay
# main-program model), so main-wrapper.sh runs the CMD with full
# stdin/stdout/stderr access and handles arg parsing there.

set -eu

MARLOW_HOME="${MARLOW_HOME:-/opt/data}"
INSTALL_DIR="/opt/marlow"

# Drop to marlow via s6-setuidgid, but skip it when already non-root.
as_marlow() { [ "$(id -u)" = 0 ] || { "$@"; return; }; s6-setuidgid marlow "$@"; }

# --- Bootstrap MARLOW_HOME as root ---
# Create the directory (and any missing parents) while we still have root
# privileges so the chown checks below see real metadata and the later
# `s6-setuidgid marlow mkdir -p` block doesn't EACCES on root-owned
# ancestors. Without this, custom MARLOW_HOME paths whose parents only
# root can create (e.g. `MARLOW_HOME=/home/marlow/.marlow` in a Compose
# file, or any path under a fresh / not pre-populated by the image)
# fail on first boot with `mkdir: cannot create directory '/...': Permission
# denied` and the cont-init hook exits non-zero. Idempotent — `mkdir -p`
# is a no-op if the dir already exists. (#18482, salvages #18488)
mkdir -p "$MARLOW_HOME"

# Numeric UID/GID validation: must be digits only, 1000-65534
validate_uid_gid() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) [ "$1" -ge 1000 ] && [ "$1" -le 65534 ] ;;
    esac
}

# --- UID/GID remap ---
# Accept PUID/PGID as aliases for MARLOW_UID/MARLOW_GID.  NAS users (UGOS,
# Synology, unRAID) expect the LinuxServer.io PUID/PGID convention and
# bind-mount /opt/data from a host directory owned by their own UID; without
# this alias those vars are silently ignored and the s6-setuidgid drop to
# UID 10000 leaves the runtime unable to read the volume.  MARLOW_UID/
# MARLOW_GID still win when both are set.  See #15290, salvages #25872.
MARLOW_UID="${MARLOW_UID:-${PUID:-}}"
MARLOW_GID="${MARLOW_GID:-${PGID:-}}"

if [ -n "${MARLOW_UID:-}" ] && validate_uid_gid "$MARLOW_UID" && [ "$MARLOW_UID" != "$(id -u marlow)" ]; then
    echo "[stage2] Changing marlow UID to $MARLOW_UID"
    usermod -u "$MARLOW_UID" marlow
fi
if [ -n "${MARLOW_GID:-}" ] && validate_uid_gid "$MARLOW_GID" && [ "$MARLOW_GID" != "$(id -g marlow)" ]; then
    echo "[stage2] Changing marlow GID to $MARLOW_GID"
    # -o allows non-unique GID (e.g. macOS GID 20 "staff" may already
    # exist as "dialout" in the Debian-based container image).
    groupmod -o -g "$MARLOW_GID" marlow 2>/dev/null || true
fi

# --- Docker socket group membership (docker-in-docker / DooD) ---
# When the user bind-mounts the host Docker daemon socket
# (`-v /var/run/docker.sock:/var/run/docker.sock`) to use the `docker`
# terminal backend from inside the container, the socket is owned by the
# host's `docker` group (or root). The supervised marlow user (UID 10000)
# is not a member of any group that matches the socket's GID, so every
# `docker` invocation EACCES'es and `check_terminal_requirements()` fails.
# See #16703.
#
# Granting the supp group via `docker run --group-add <gid>` alone is
# NOT sufficient with our s6-setuidgid privilege drop: s6-setuidgid (and
# gosu, the older shim) calls initgroups() for the target user, which
# rebuilds the supplementary group list from /etc/group. Without an
# /etc/group entry whose GID matches the socket, the kernel-granted
# supp group is silently wiped between PID 1 and the dropped process.
# Confirmed empirically: `--group-add 998` alone leaves the dropped
# marlow process with `Groups: 10000` (998 gone); after this hook adds
# the entry, the dropped process has `Groups: 998 10000` as expected.
#
# Fix: detect the socket's GID at boot and ensure /etc/group has a
# matching entry that includes marlow. Idempotent across container
# restarts. Skipped silently when no socket is bind-mounted.
#
# Handles the awkward corner cases:
#   - socket owned by GID 0 (root) — some Podman setups; usermod -aG root
#   - socket GID already used by a known container group (e.g. tty=5):
#     reuse that group's name rather than creating a duplicate
#   - marlow is already a member of the right group (idempotent restart)
#   - chown/groupadd failures under rootless containers — non-fatal
for sock in /var/run/docker.sock /run/docker.sock; do
    [ -S "$sock" ] || continue
    sock_gid=$(stat -c '%g' "$sock" 2>/dev/null) || continue
    [ -n "$sock_gid" ] || continue
    # Already a member? Nothing to do.
    if id -G marlow 2>/dev/null | tr ' ' '\n' | grep -qx "$sock_gid"; then
        echo "[stage2] marlow already in group $sock_gid for $sock"
        break
    fi
    # Resolve or create a group name for this GID.
    sock_group=$(getent group "$sock_gid" 2>/dev/null | cut -d: -f1)
    if [ -z "$sock_group" ]; then
        sock_group="hostdocker"
        if ! groupadd -g "$sock_gid" "$sock_group" 2>/dev/null; then
            echo "[stage2] Warning: groupadd -g $sock_gid $sock_group failed; skipping docker socket group setup"
            break
        fi
        echo "[stage2] Created group $sock_group (GID $sock_gid) for Docker socket"
    fi
    if usermod -aG "$sock_group" marlow 2>/dev/null; then
        echo "[stage2] Added marlow to group $sock_group (GID $sock_gid) for $sock"
    else
        echo "[stage2] Warning: usermod -aG $sock_group marlow failed; docker backend may fail with EACCES"
    fi
    break
done

# --- Fix ownership of data volume ---
# When MARLOW_UID is remapped or the top-level $MARLOW_HOME isn't owned by
# the runtime marlow UID, restore ownership to marlow — but ONLY for the
# directories marlow actually writes to. The full $MARLOW_HOME may be a
# host-mounted bind containing unrelated user files; `chown -R` would
# silently destroy host ownership of those (see issue #19788).
#
# The canonical list of marlow-owned subdirs is the same one the s6-setuidgid
# mkdir -p block below seeds. Keep them in sync if the seed list changes.
actual_marlow_uid=$(id -u marlow)
needs_chown=false
if [ "$(stat -c %u "$MARLOW_HOME" 2>/dev/null)" != "$actual_marlow_uid" ]; then
    needs_chown=true
fi
if [ "$needs_chown" = true ]; then
    echo "[stage2] Fixing ownership of $MARLOW_HOME (targeted) to marlow ($actual_marlow_uid)"
    # In rootless Podman the container's "root" is mapped to an
    # unprivileged host UID — chown will fail. That's fine: the volume
    # is already owned by the mapped user on the host side.
    #
    # Top-level $MARLOW_HOME: chown the directory itself (not its contents)
    # so marlow can mkdir new subdirs but bind-mounted host files keep
    # their existing ownership.
    chown marlow:marlow "$MARLOW_HOME" 2>/dev/null || \
        echo "[stage2] Warning: chown $MARLOW_HOME failed (rootless container?) — continuing"
    # Marlow-owned subdirs: recursive chown is safe here because these are
    # created and managed exclusively by marlow (see the s6-setuidgid mkdir
    # -p block below for the canonical list).
    for sub in cron sessions logs hooks memories skills plans workspace home profiles; do
        if [ -e "$MARLOW_HOME/$sub" ]; then
            chown -R marlow:marlow "$MARLOW_HOME/$sub" 2>/dev/null || \
                echo "[stage2] Warning: chown $MARLOW_HOME/$sub failed (rootless container?) — continuing"
        fi
    done
    # Marlow-owned trees under $INSTALL_DIR must be re-chowned when the UID
    # is remapped — otherwise:
    #   - .venv: lazy_deps.py cannot install platform packages (discord.py,
    #     telegram, slack, etc.) with EACCES (#15012, #21100)
    #   - node_modules: root-level dependencies (puppeteer, web tooling)
    #     that runtime code may walk/update.
    # The set mirrors the build-time `chown -R marlow:marlow` line in the
    # Dockerfile — keep them in sync if the Dockerfile chown set changes.
    # These are under $INSTALL_DIR (not $MARLOW_HOME), so the bind-mount
    # concern doesn't apply — recursive is fine.
    chown -R marlow:marlow \
        "$INSTALL_DIR/.venv" \
        "$INSTALL_DIR/node_modules" \
        2>/dev/null || \
        echo "[stage2] Warning: chown of build trees failed (rootless container?) — continuing"
fi

# Always reset ownership of $MARLOW_HOME/profiles to marlow on every
# boot. Profile dirs and files can land owned by root when commands
# are invoked via `docker exec <container> marlow …` (which defaults
# to root unless `-u` is passed), and that breaks the cont-init
# reconciler (02-reconcile-profiles) which runs as marlow and walks
# the profiles dir. Idempotent; skipped on rootless containers where
# chown would fail.
if [ -d "$MARLOW_HOME/profiles" ]; then
    chown -R marlow:marlow "$MARLOW_HOME/profiles" 2>/dev/null || true
fi

# Reset ownership of marlow-owned top-level state files on every boot.
# The targeted data-volume chown above only covers marlow-owned
# *subdirectories*; loose state files living directly under $MARLOW_HOME
# are missed. When those files are created or rewritten by
# `docker exec <container> marlow …` (root unless `-u` is passed) they
# land root-owned, and the unprivileged marlow runtime then hits
# PermissionError on next startup (e.g. gateway.lock / state.db /
# auth.json), producing a gateway restart loop.
#
# We use an explicit allowlist rather than a blanket `find -user root`
# sweep so host-owned files in a bind-mounted $MARLOW_HOME are never
# touched — same targeted-ownership contract as the subdir chown above
# (issue #19788, PR #19795). The list mirrors the top-level *file*
# entries of marlow_cli.profile_distribution.USER_OWNED_EXCLUDE plus the
# runtime lock files; keep them in sync if that set changes.
for f in \
    auth.json auth.lock .env \
    state.db state.db-shm state.db-wal \
    marlow_state.db \
    response_store.db response_store.db-shm response_store.db-wal \
    gateway.pid gateway.lock gateway_state.json processes.json \
    active_profile; do
    if [ -e "$MARLOW_HOME/$f" ]; then
        chown marlow:marlow "$MARLOW_HOME/$f" 2>/dev/null || true
    fi
done

# --- config.yaml permissions ---
# Ensure config.yaml is readable by the marlow runtime user even if it
# was edited on the host after initial ownership setup.
if [ -f "$MARLOW_HOME/config.yaml" ]; then
    chown marlow:marlow "$MARLOW_HOME/config.yaml" 2>/dev/null || true
    chmod 640 "$MARLOW_HOME/config.yaml" 2>/dev/null || true
fi

# --- Seed directory structure as marlow user ---
# Run as marlow via s6-setuidgid so dirs end up owned correctly (matters
# under rootless Podman where chown back to root would fail).
#
# Use direct `mkdir -p` invocation (no `sh -c "..."` wrapper) so the
# shell isn't a second interpreter — defends against $MARLOW_HOME values
# containing shell metacharacters. PR #30136 review item O2.
as_marlow mkdir -p \
    "$MARLOW_HOME/cron" \
    "$MARLOW_HOME/sessions" \
    "$MARLOW_HOME/logs" \
    "$MARLOW_HOME/hooks" \
    "$MARLOW_HOME/memories" \
    "$MARLOW_HOME/skills" \
    "$MARLOW_HOME/plans" \
    "$MARLOW_HOME/workspace" \
    "$MARLOW_HOME/home"

# --- Install-method stamp (read by detect_install_method() in marlow status) ---
# Preserved from the tini-era entrypoint (PR #27843). Must be written as
# the marlow user so ownership matches the file's documented owner.
# tee is invoked directly via s6-setuidgid (no `sh -c` wrapper) for the
# same shell-metacharacter safety described above.
printf 'docker\n' | as_marlow tee "$MARLOW_HOME/.install_method" >/dev/null \
    || true

# --- Seed config files (only on first boot) ---
seed_one() {
    dest=$1
    src=$2
    if [ ! -f "$MARLOW_HOME/$dest" ] && [ -f "$INSTALL_DIR/$src" ]; then
        as_marlow cp "$INSTALL_DIR/$src" "$MARLOW_HOME/$dest"
    fi
}
seed_one ".env" ".env.example"
seed_one "config.yaml" "cli-config.yaml.example"
seed_one "SOUL.md" "docker/SOUL.md"

# .env holds API keys and secrets — restrict to owner-only access. Applied
# unconditionally (not only on first-seed) so a host-mounted .env that was
# created with a permissive umask gets tightened on every container start.
if [ -f "$MARLOW_HOME/.env" ]; then
    chown marlow:marlow "$MARLOW_HOME/.env" 2>/dev/null || true
    chmod 600 "$MARLOW_HOME/.env" 2>/dev/null || true
fi

# auth.json: bootstrap from env on first boot only. Same semantics as the
# pre-s6 entrypoint — the [ ! -f ] guard is critical to avoid clobbering
# rotated refresh tokens on container restart.
if [ ! -f "$MARLOW_HOME/auth.json" ] && [ -n "${MARLOW_AUTH_JSON_BOOTSTRAP:-}" ]; then
    printf '%s' "$MARLOW_AUTH_JSON_BOOTSTRAP" > "$MARLOW_HOME/auth.json"
    chown marlow:marlow "$MARLOW_HOME/auth.json" 2>/dev/null || true
    chmod 600 "$MARLOW_HOME/auth.json"
fi

# --- Discover agent-browser's Chromium binary ---
# The image's Dockerfile runs `npx playwright install chromium`, which
# populates ``$PLAYWRIGHT_BROWSERS_PATH`` (=/opt/marlow/.playwright) with
# a ``chromium_headless_shell-<build>/chrome-headless-shell-linux64/``
# directory. agent-browser (the runtime CLI Marlow spawns for the
# browser tool) doesn't recognise this layout in its own cache scan and
# fails with "Auto-launch failed: Chrome not found" — even though the
# binary is right there (#15697).
#
# Fix: locate the binary at boot and export ``AGENT_BROWSER_EXECUTABLE_PATH``
# via /run/s6/container_environment so the `with-contenv` shebang on
# main-wrapper.sh propagates it into the supervised ``marlow`` process
# and thence to agent-browser subprocesses.
#
# - Skipped when the user has already set ``AGENT_BROWSER_EXECUTABLE_PATH``
#   (lets users override with a system Chrome install).
# - Filename-matched (not path-matched): the chromium dir contains many
#   shared libraries (libGLESv2.so, libEGL.so, ...) which inherit the
#   executable bit from Playwright's tarball but are NOT browser binaries.
#   We only accept files whose basename is chrome / chromium /
#   chrome-headless-shell / headless_shell / chromium-browser. Compare
#   PR #18635's earlier ``find | grep -Ei 'chrome|chromium'`` which would
#   match the path ``.../chrome-headless-shell-linux64/libGLESv2.so`` and
#   pick a .so.
# - Quietly skipped when $PLAYWRIGHT_BROWSERS_PATH doesn't exist (e.g.
#   custom builds that strip Playwright).
if [ -z "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ] && \
        [ -n "${PLAYWRIGHT_BROWSERS_PATH:-}" ] && \
        [ -d "$PLAYWRIGHT_BROWSERS_PATH" ]; then
    browser_bin=$(find "$PLAYWRIGHT_BROWSERS_PATH" -type f -executable \
        \( -name 'chrome' -o -name 'chromium' \
           -o -name 'chrome-headless-shell' -o -name 'headless_shell' \
           -o -name 'chromium-browser' \) \
        2>/dev/null | head -n 1)
    if [ -n "$browser_bin" ]; then
        echo "[stage2] Found agent-browser Chromium binary: $browser_bin"
        # Write to s6's container_environment so with-contenv picks it
        # up for all supervised services (main-marlow and gateways).
        # Idempotent: each boot overwrites with the current path.
        # Some container runtimes / s6-overlay versions do not create the
        # envdir before cont-init hooks run, so create it defensively.
        mkdir -p /run/s6/container_environment
        printf '%s' "$browser_bin" > /run/s6/container_environment/AGENT_BROWSER_EXECUTABLE_PATH
    else
        echo "[stage2] Warning: no Chromium binary under $PLAYWRIGHT_BROWSERS_PATH; browser tool may fail"
    fi
fi

echo "[stage2] Setup complete; starting user services"
