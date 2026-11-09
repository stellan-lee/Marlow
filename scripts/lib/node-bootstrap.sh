#!/usr/bin/env bash
# ============================================================================
# scripts/lib/node-bootstrap.sh
# ----------------------------------------------------------------------------
# Sourceable helper: ensure Node.js >= MIN_VERSION is available for browser tooling
# (React + Ink) and browser tools.
#
# Strategy (first hit wins — respects the user's existing tooling):
#   1. modern `node` already on PATH
#   2. ~/.marlow/node/ from a prior Marlow-managed install
#   3. fnm, proto, nvm (in that order) if the user already uses a version manager
#   4. macOS Homebrew
#   5. pinned nodejs.org tarball into ~/.marlow/node/ (always works, zero shell rc edits)
#
# Usage:
#   source scripts/lib/node-bootstrap.sh
#   ensure_node   # returns 0 on success, non-zero on failure
#   if [ "$MARLOW_NODE_AVAILABLE" = true ]; then ...; fi
#
# Env inputs (set before sourcing to override defaults):
#   MARLOW_NODE_MIN_VERSION   (default: 20)   — accepted on PATH
#   MARLOW_NODE_TARGET_MAJOR  (default: 22)   — installed when we install
#   MARLOW_HOME               (default: $HOME/.marlow)
# ============================================================================

MARLOW_NODE_MIN_VERSION="${MARLOW_NODE_MIN_VERSION:-20}"
MARLOW_NODE_TARGET_MAJOR="${MARLOW_NODE_TARGET_MAJOR:-22}"
MARLOW_HOME="${MARLOW_HOME:-$HOME/.marlow}"
MARLOW_NODE_AVAILABLE=false

# ---------------------------------------------------------------------------
# Logging — prefer the host script's log_* helpers when present
# ---------------------------------------------------------------------------

_nb_log()  { declare -F log_info    >/dev/null 2>&1 && log_info    "$*" || printf '→ %s\n' "$*" >&2; }
_nb_ok()   { declare -F log_success >/dev/null 2>&1 && log_success "$*" || printf '✓ %s\n' "$*" >&2; }
_nb_warn() { declare -F log_warn    >/dev/null 2>&1 && log_warn    "$*" || printf '⚠ %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# Platform + version helpers
# ---------------------------------------------------------------------------

_nb_node_major() {
    local v
    v=$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)
    [[ "$v" =~ ^[0-9]+$ ]] && echo "$v" || echo 0
}

_nb_have_modern_node() {
    command -v node >/dev/null 2>&1 || return 1
    [ "$(_nb_node_major)" -ge "$MARLOW_NODE_MIN_VERSION" ]
}

# ---------------------------------------------------------------------------
# Version-manager paths — respect what the user already uses
# ---------------------------------------------------------------------------

_nb_try_fnm() {
    command -v fnm >/dev/null 2>&1 || return 1
    _nb_log "fnm detected — installing Node $MARLOW_NODE_TARGET_MAJOR..."
    eval "$(fnm env 2>/dev/null)" || true
    fnm install "$MARLOW_NODE_TARGET_MAJOR" >/dev/null 2>&1 || return 1
    fnm use     "$MARLOW_NODE_TARGET_MAJOR" >/dev/null 2>&1 || return 1
    _nb_have_modern_node || return 1
    _nb_ok "Node $(node --version) activated via fnm"
    return 0
}

_nb_try_proto() {
    command -v proto >/dev/null 2>&1 || return 1
    _nb_log "proto detected — installing Node $MARLOW_NODE_TARGET_MAJOR..."
    proto install node "$MARLOW_NODE_TARGET_MAJOR" >/dev/null 2>&1 || return 1
    _nb_have_modern_node || return 1
    _nb_ok "Node $(node --version) activated via proto"
    return 0
}

_nb_try_nvm() {
    local nvm_sh="${NVM_DIR:-$HOME/.nvm}/nvm.sh"
    [ -s "$nvm_sh" ] || return 1
    # shellcheck source=/dev/null
    \. "$nvm_sh" >/dev/null 2>&1 || return 1
    _nb_log "nvm detected — installing Node $MARLOW_NODE_TARGET_MAJOR..."
    nvm install "$MARLOW_NODE_TARGET_MAJOR" >/dev/null 2>&1 || return 1
    nvm use     "$MARLOW_NODE_TARGET_MAJOR" >/dev/null 2>&1 || return 1
    _nb_have_modern_node || return 1
    _nb_ok "Node $(node --version) activated via nvm"
    return 0
}

# ---------------------------------------------------------------------------
# Platform package managers
# ---------------------------------------------------------------------------

_nb_try_brew() {
    [ "$(uname -s)" = "Darwin" ] || return 1
    command -v brew >/dev/null 2>&1 || return 1
    _nb_log "Installing Node via Homebrew..."
    brew install "node@${MARLOW_NODE_TARGET_MAJOR}" >/dev/null 2>&1 \
        || brew install node >/dev/null 2>&1 \
        || return 1
    brew link --overwrite --force "node@${MARLOW_NODE_TARGET_MAJOR}" >/dev/null 2>&1 || true
    _nb_have_modern_node || return 1
    _nb_ok "Node $(node --version) installed via Homebrew"
    return 0
}

# ---------------------------------------------------------------------------
# Bundled binary fallback — always works, no shell rc edits
# ---------------------------------------------------------------------------

_nb_install_bundled_node() {
    local arch node_arch os_name node_os
    arch=$(uname -m)
    case "$arch" in
        x86_64)        node_arch="x64"    ;;
        aarch64|arm64) node_arch="arm64"  ;;
        armv7l)        node_arch="armv7l" ;;
        *)
            _nb_warn "Unsupported arch ($arch) — install Node.js manually: https://nodejs.org/"
            return 1
            ;;
    esac

    os_name=$(uname -s)
    case "$os_name" in
        Linux*)  node_os="linux"  ;;
        Darwin*) node_os="darwin" ;;
        *)
            _nb_warn "Unsupported OS ($os_name) — install Node.js manually: https://nodejs.org/"
            return 1
            ;;
    esac

    local index_url="https://nodejs.org/dist/latest-v${MARLOW_NODE_TARGET_MAJOR}.x/"
    local tarball
    tarball=$(curl -fsSL "$index_url" \
        | grep -oE "node-v${MARLOW_NODE_TARGET_MAJOR}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz" \
        | head -1)
    if [ -z "$tarball" ]; then
        tarball=$(curl -fsSL "$index_url" \
            | grep -oE "node-v${MARLOW_NODE_TARGET_MAJOR}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.gz" \
            | head -1)
    fi
    if [ -z "$tarball" ]; then
        _nb_warn "Could not resolve Node $MARLOW_NODE_TARGET_MAJOR binary for $node_os-$node_arch"
        return 1
    fi

    local tmp
    tmp=$(mktemp -d)
    _nb_log "Downloading $tarball..."
    curl -fsSL "${index_url}${tarball}" -o "$tmp/$tarball" || {
        _nb_warn "Download failed"; rm -rf "$tmp"; return 1
    }

    _nb_log "Extracting to $MARLOW_HOME/node/..."
    if [[ "$tarball" == *.tar.xz ]]; then
        tar xf  "$tmp/$tarball" -C "$tmp" || { rm -rf "$tmp"; return 1; }
    else
        tar xzf "$tmp/$tarball" -C "$tmp" || { rm -rf "$tmp"; return 1; }
    fi

    local extracted
    extracted=$(find "$tmp" -maxdepth 1 -type d -name 'node-v*' 2>/dev/null | head -1)
    if [ ! -d "$extracted" ]; then
        _nb_warn "Extraction produced no node-v* directory"
        rm -rf "$tmp"
        return 1
    fi

    mkdir -p "$MARLOW_HOME"
    rm -rf "$MARLOW_HOME/node"
    mv "$extracted" "$MARLOW_HOME/node"
    rm -rf "$tmp"

    mkdir -p "$HOME/.local/bin"
    ln -sf "$MARLOW_HOME/node/bin/node" "$HOME/.local/bin/node"
    ln -sf "$MARLOW_HOME/node/bin/npm"  "$HOME/.local/bin/npm"
    ln -sf "$MARLOW_HOME/node/bin/npx"  "$HOME/.local/bin/npx"
    export PATH="$MARLOW_HOME/node/bin:$PATH"

    _nb_have_modern_node || return 1
    _nb_ok "Node $(node --version) installed to $MARLOW_HOME/node/"
    return 0
}

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

ensure_node() {
    MARLOW_NODE_AVAILABLE=false

    if _nb_have_modern_node; then
        _nb_ok "Node $(node --version) found"
        MARLOW_NODE_AVAILABLE=true
        return 0
    fi

    if [ -x "$MARLOW_HOME/node/bin/node" ]; then
        export PATH="$MARLOW_HOME/node/bin:$PATH"
        if _nb_have_modern_node; then
            _nb_ok "Node $(node --version) found (Marlow-managed)"
            MARLOW_NODE_AVAILABLE=true
            return 0
        fi
    fi

    # Version managers first — respect the user's existing setup.
    _nb_try_fnm   && { MARLOW_NODE_AVAILABLE=true; return 0; }
    _nb_try_proto && { MARLOW_NODE_AVAILABLE=true; return 0; }
    _nb_try_nvm   && { MARLOW_NODE_AVAILABLE=true; return 0; }

    # Platform package managers.
    _nb_try_brew && { MARLOW_NODE_AVAILABLE=true; return 0; }

    # Last resort: pinned nodejs.org tarball.
    _nb_install_bundled_node && { MARLOW_NODE_AVAILABLE=true; return 0; }

    _nb_warn "Node.js install failed — browser tools will be unavailable."
    _nb_warn "Install manually: https://nodejs.org/en/download/  (or: \`brew install node\`, \`fnm install $MARLOW_NODE_TARGET_MAJOR\`, etc.)"
    return 1
}
