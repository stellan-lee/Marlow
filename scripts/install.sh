#!/bin/bash
# ============================================================================
# Marlow Agent Installer
# ============================================================================
# Installation script for Linux and macOS using uv.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/stellan-lee/Marlow/main/scripts/install.sh | bash
#
# Or with options:
#   curl -fsSL ... | bash -s -- --no-venv --skip-setup
#
# ============================================================================

set -e

# Guard against environment leakage when the installer is launched from another
# Python-driven tool session (e.g. Marlow terminal tool). A pre-set PYTHONPATH
# can force pip/entrypoints to import a different checkout than the one being
# installed, which makes fresh installs appear broken or stale.
if [ -n "${PYTHONPATH:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONPATH during install to avoid module shadowing"
    unset PYTHONPATH
fi
if [ -n "${PYTHONHOME:-}" ]; then
    echo "⚠ Ignoring inherited PYTHONHOME during install"
    unset PYTHONHOME
fi

# Prevent uv from discovering config files (uv.toml, pyproject.toml) from the
# wrong user's home directory when running under sudo -u <user>.  See #21269.
export UV_NO_CONFIG=1

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Configuration
REPO_URL_SSH="git@github.com:stellan-lee/Marlow.git"
REPO_URL_HTTPS="https://github.com/stellan-lee/Marlow.git"
if [ -n "${MARLOW_HOME:-}" ]; then
    MARLOW_HOME_EXPLICIT=true
else
    MARLOW_HOME="$HOME/.marlow"
    MARLOW_HOME_EXPLICIT=false
fi
LEGACY_HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
# INSTALL_DIR is resolved AFTER arg parsing and OS detection so we can pick an
# FHS-style layout for root installs.  Track whether the user gave us an
# explicit directory — if so we never override it.
if [ -n "${MARLOW_INSTALL_DIR:-}" ]; then
    INSTALL_DIR="$MARLOW_INSTALL_DIR"
    INSTALL_DIR_EXPLICIT=true
else
    INSTALL_DIR=""
    INSTALL_DIR_EXPLICIT=false
fi
PYTHON_VERSION="3.11"
NODE_VERSION="22"

# FHS-style root install layout (set by resolve_install_layout when applicable):
#   code at /usr/local/lib/marlow-agent, command at /usr/local/bin/marlow,
#   data still at /root/.marlow (MARLOW_HOME).  Matches Claude Code / Codex CLI
#   and keeps Docker bind-mounted /root/ volumes lean.
ROOT_FHS_LAYOUT=false
DETECTED_BROWSER_EXECUTABLE=""

# Options
USE_VENV=true
RUN_SETUP=true
SKIP_BROWSER=false
NO_SKILLS=false
BRANCH="main"
INSTALL_COMMIT=""
ENSURE_DEPS=""
POSTINSTALL_MODE=false
MANIFEST_MODE=false
STAGE_NAME=""
JSON_OUTPUT=false
NON_INTERACTIVE=false

# Detect non-interactive mode (e.g. curl | bash)
# When stdin is not a terminal, read -p will fail with EOF,
# causing set -e to silently abort the entire script.
if [ -t 0 ]; then
    IS_INTERACTIVE=true
else
    IS_INTERACTIVE=false
fi

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-venv)
            USE_VENV=false
            shift
            ;;
        --skip-setup)
            RUN_SETUP=false
            shift
            ;;
        --skip-browser|--no-playwright)
            SKIP_BROWSER=true
            shift
            ;;
        --no-skills)
            NO_SKILLS=true
            shift
            ;;
        --branch|-Branch)
            BRANCH="$2"
            shift 2
            ;;
        --commit|-Commit)
            INSTALL_COMMIT="$2"
            shift 2
            ;;
        --manifest|-Manifest)
            MANIFEST_MODE=true
            shift
            ;;
        --stage|-Stage)
            STAGE_NAME="$2"
            shift 2
            ;;
        --json|-Json)
            JSON_OUTPUT=true
            shift
            ;;
        --non-interactive|-NonInteractive)
            NON_INTERACTIVE=true
            shift
            ;;
        --dir)
            INSTALL_DIR="$2"
            INSTALL_DIR_EXPLICIT=true
            shift 2
            ;;
        --marlow-home)
            MARLOW_HOME="$2"
            MARLOW_HOME_EXPLICIT=true
            shift 2
            ;;
        --ensure)
            ENSURE_DEPS="$2"
            shift 2
            ;;
        --postinstall)
            POSTINSTALL_MODE=true
            shift
            ;;
        -h|--help)
            echo "Marlow Agent Installer"
            echo ""
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-venv      Don't create virtual environment"
            echo "  --skip-setup   Skip interactive setup wizard"
            echo "  --skip-browser Skip Playwright/Chromium install (browser tools won't work)"
            echo "  --no-skills    Start with a blank slate — seed no bundled skills, and"
            echo "                   write \$MARLOW_HOME/.no-bundled-skills so future"
            echo "                   'marlow update' runs never inject bundled skills either"
            echo "  --branch NAME  Git branch to install (default: main)"
            echo "  --commit SHA   Pin checkout to a specific commit after clone/update"
            echo "  --manifest     Print desktop bootstrap stage manifest as JSON"
            echo "  --stage NAME   Run one desktop bootstrap stage"
            echo "  --json         Print a JSON result frame for --stage"
            echo "  --non-interactive  Skip stages that require user input"
            echo "  --dir PATH     Installation directory"
            echo "                   default (non-root):  ~/.marlow/marlow-agent"
            echo "                   default (root, Linux): /usr/local/lib/marlow-agent"
            echo "  --marlow-home PATH  Data directory (default: ~/.marlow, or \$MARLOW_HOME)"
            echo "  -h, --help     Show this help"
            echo ""
            echo "Notes:"
            echo "  When running as root on Linux, Marlow installs the code under"
            echo "  /usr/local/lib/marlow-agent and links the command into"
            echo "  /usr/local/bin/marlow (FHS layout — matches Claude Code / Codex CLI)."
            echo "  Data, config, sessions, and logs still live in \$MARLOW_HOME"
            echo "  (default /root/.marlow).  This keeps Docker bind-mounted volumes"
            echo "  small and ensures the command is on PATH for all shells."
            echo "  Existing installs at \$MARLOW_HOME/marlow-agent are preserved in-place."
            echo "  --ensure DEPS  Install only specified deps (comma-separated)"
            echo "                   Supported: node, browser, ripgrep"
            echo "                   Does NOT clone repo or create venv"
            echo "  --postinstall  Run post-install setup only (for pip users)"
            echo "                   Installs optional deps + runs marlow setup"
            echo "                   Does NOT clone repo or create venv"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# ============================================================================
# Helper functions
# ============================================================================

print_banner() {
    echo ""
    echo -e "${MAGENTA}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│             ⚕ Marlow Agent Installer                    │"
    echo "├─────────────────────────────────────────────────────────┤"
    echo "│  A self-improving open source AI agent.                 │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
}

log_info() {
    echo -e "${CYAN}→${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

json_escape() {
    # Enough for short installer status strings; avoids requiring jq during
    # pre-install bootstrap.
    printf '%s' "$1" | tr '\n' ' ' | sed \
        -e 's/\\/\\\\/g' \
        -e 's/"/\\"/g'
}

# npm rewrites tracked package-lock.json files non-deterministically during
# `npm install` / `npm run pack`. On a managed install those diffs are never
# intentional, but they leave the checkout dirty — which forces `marlow update`
# to autostash on every run and makes branch switches fragile. Restore them so
# a fresh install ends with a clean tree. Best-effort; only touches lockfiles.
restore_dirty_lockfiles() {
    local repo="${1:-$INSTALL_DIR}"
    [ -n "$repo" ] && [ -d "$repo/.git" ] || return 0
    command -v git >/dev/null 2>&1 || return 0
    local dirty
    dirty=$(git -C "$repo" diff --name-only 2>/dev/null | grep 'package-lock\.json$' || true)
    [ -z "$dirty" ] && return 0
    echo "$dirty" | while IFS= read -r f; do
        [ -n "$f" ] && git -C "$repo" checkout -- "$f" 2>/dev/null || true
    done
}

emit_manifest() {
    printf '%s' '{"protocol_version":1,"stages":[{"name":"prerequisites","title":"System prerequisites","category":"runtime","needs_user_input":false},{"name":"repository","title":"Download Marlow Agent","category":"runtime","needs_user_input":false},{"name":"venv","title":"Create Python virtual environment","category":"runtime","needs_user_input":false},{"name":"python-deps","title":"Install Python dependencies","category":"runtime","needs_user_input":false},{"name":"node-deps","title":"Install browser-tool dependencies","category":"runtime","needs_user_input":false},{"name":"path","title":"Install marlow command","category":"runtime","needs_user_input":false},{"name":"config","title":"Prepare config and skills","category":"configuration","needs_user_input":false},{"name":"setup","title":"Configure API keys and settings","category":"configuration","needs_user_input":true},{"name":"gateway","title":"Configure gateway service","category":"configuration","needs_user_input":true},{"name":"complete","title":"Finish install","category":"runtime","needs_user_input":false}]}'
    printf '\n'
}

stage_needs_user_input() {
    case "$1" in
        setup|gateway) return 0 ;;
        *) return 1 ;;
    esac
}

emit_stage_json() {
    local stage="$1"
    local ok="$2"
    local skipped="${3:-false}"
    local reason="${4:-}"
    local escaped_reason
    escaped_reason="$(json_escape "$reason")"
    if [ -n "$escaped_reason" ]; then
        printf '{"ok":%s,"stage":"%s","skipped":%s,"reason":"%s"}\n' "$ok" "$stage" "$skipped" "$escaped_reason"
    else
        printf '{"ok":%s,"stage":"%s","skipped":%s}\n' "$ok" "$stage" "$skipped"
    fi
}

prompt_yes_no() {
    local question="$1"
    local default="${2:-yes}"
    local prompt_suffix
    local answer=""

    # Use case patterns (not ${var,,}) so this works on bash 3.2 (macOS /bin/bash).
    case "$default" in
        [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) prompt_suffix="[Y/n]" ;;
        *) prompt_suffix="[y/N]" ;;
    esac

    if [ "$NON_INTERACTIVE" = true ]; then
        answer=""
    elif [ "$IS_INTERACTIVE" = true ]; then
        read -r -p "$question $prompt_suffix " answer || answer=""
    elif [ -r /dev/tty ] && [ -w /dev/tty ]; then
        printf "%s %s " "$question" "$prompt_suffix" > /dev/tty
        IFS= read -r answer < /dev/tty || answer=""
    else
        answer=""
    fi

    answer="${answer#"${answer%%[![:space:]]*}"}"
    answer="${answer%"${answer##*[![:space:]]}"}"

    if [ -z "$answer" ]; then
        case "$default" in
            [yY]|[yY][eE][sS]|[tT][rR][uU][eE]|1) return 0 ;;
            *) return 1 ;;
        esac
    fi

    case "$answer" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# Carry a pre-rename installation's user state into Marlow without moving or
# deleting anything. The old source checkout and helper binaries are excluded;
# they are not user data and copying them would preserve the broken editable
# install that this migration is intended to escape. Existing Marlow files win,
# making the operation idempotent and safe to rerun.
migrate_legacy_hermes_state() {
    if [ "$MARLOW_HOME_EXPLICIT" = true ]; then
        return 0
    fi
    if [ ! -d "$LEGACY_HERMES_HOME" ] || [ "$LEGACY_HERMES_HOME" = "$MARLOW_HOME" ]; then
        return 0
    fi
    if [ -e "$MARLOW_HOME" ] && [ "$LEGACY_HERMES_HOME" -ef "$MARLOW_HOME" ]; then
        return 0
    fi

    local marker="$MARLOW_HOME/.legacy-migration-complete"
    if [ -f "$marker" ]; then
        return 0
    fi

    log_info "Existing pre-Marlow installation detected; preserving its user data..."
    mkdir -p "$MARLOW_HOME"

    local copied=0
    local legacy_item
    local item_name
    local target_item
    while IFS= read -r -d '' legacy_item; do
        item_name="${legacy_item##*/}"
        case "$item_name" in
            hermes-agent|bin|.install_method|.update_*)
                continue
                ;;
        esac
        # Also skip any differently named source checkout.
        if [ -d "$legacy_item/.git" ]; then
            continue
        fi
        # Runtime sockets and pipes are not durable user state and copying them
        # can block or create unusable endpoints in the new home.
        if [ -S "$legacy_item" ] || [ -p "$legacy_item" ]; then
            continue
        fi

        target_item="$MARLOW_HOME/$item_name"
        if [ -e "$target_item" ] || [ -L "$target_item" ]; then
            continue
        fi
        cp -pR "$legacy_item" "$target_item"
        copied=$((copied + 1))
    done < <(find "$LEGACY_HERMES_HOME" -mindepth 1 -maxdepth 1 -print0)

    {
        echo "source=$LEGACY_HERMES_HOME"
        echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$marker"
    chmod 600 "$marker"

    log_success "Preserved $copied legacy state item(s) in $MARLOW_HOME"
    log_info "The old checkout remains at $LEGACY_HERMES_HOME for manual rollback."
}

retire_legacy_hermes_launcher() {
    local command_link_dir="$1"
    local legacy_launcher="$command_link_dir/hermes"
    if [ ! -e "$legacy_launcher" ] && [ ! -L "$legacy_launcher" ]; then
        return 0
    fi

    local legacy_owned=false
    local legacy_target=""
    if [ -L "$legacy_launcher" ]; then
        legacy_target="$(readlink "$legacy_launcher" 2>/dev/null || true)"
        case "$legacy_target" in
            *"/.hermes/hermes-agent/"*) legacy_owned=true ;;
        esac
    elif grep -qE '(\.hermes/hermes-agent|hermes-agent/venv|from marlow_cli\.main import main)' \
            "$legacy_launcher" 2>/dev/null; then
        legacy_owned=true
    fi

    if [ "$legacy_owned" = true ]; then
        local legacy_backup="$legacy_launcher.marlow-migration-backup"
        if [ -e "$legacy_backup" ] || [ -L "$legacy_backup" ]; then
            legacy_backup="$legacy_backup.$(date -u +%Y%m%d-%H%M%S)"
        fi
        mv "$legacy_launcher" "$legacy_backup"
        log_warn "Retired the obsolete 'hermes' launcher; use 'marlow' from now on."
        log_info "Legacy launcher backup: $legacy_backup"
    fi
}

# Decide where the repo checkout + venv live, and where the `marlow` command
# symlink goes.  Called after detect_os so $OS/$DISTRO are known.
#
# Defaults:
#   - Non-root, any OS:       INSTALL_DIR = $MARLOW_HOME/marlow-agent
#                             command link in $HOME/.local/bin
#   - Root on Linux (new):    INSTALL_DIR = /usr/local/lib/marlow-agent
#                             command link in /usr/local/bin
#                             (unless a legacy install already exists at
#                              $MARLOW_HOME/marlow-agent — then preserve it)
#
# Always no-op when the user set --dir or $MARLOW_INSTALL_DIR.
resolve_install_layout() {
    migrate_legacy_hermes_state

    if [ "$INSTALL_DIR_EXPLICIT" = true ]; then
        log_info "Install directory: $INSTALL_DIR (explicit)"
        return 0
    fi

    # Root on Linux: prefer FHS layout unless a legacy install already exists.
    # macOS root installs keep the legacy layout because /usr/local/ on macOS
    # is Homebrew territory and we don't want to fight that.
    if [ "$OS" = "linux" ] && [ "$(id -u)" -eq 0 ]; then
        if [ -d "$MARLOW_HOME/marlow-agent/.git" ]; then
            INSTALL_DIR="$MARLOW_HOME/marlow-agent"
            log_info "Existing install detected at $INSTALL_DIR — keeping legacy layout"
            log_info "  (new root installs use /usr/local/lib/marlow-agent)"
            return 0
        fi
        INSTALL_DIR="/usr/local/lib/marlow-agent"
        ROOT_FHS_LAYOUT=true
        # Place uv-managed Python under /usr/local/share so the venv interpreter
        # is world-readable.  Default uv paths land in /root/.local/share/uv,
        # which non-root users can't traverse — leaving the shared
        # /usr/local/bin/marlow wrapper unable to exec the bad-interpreter venv
        # python.  See #21457.
        export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/usr/local/share/uv/python}"
        export UV_PYTHON_BIN_DIR="${UV_PYTHON_BIN_DIR:-/usr/local/share/uv/bin}"
        log_info "Root install on Linux — using FHS layout"
        log_info "  Code:    $INSTALL_DIR"
        log_info "  Command: /usr/local/bin/marlow"
        log_info "  Data:    $MARLOW_HOME (unchanged)"
        log_info "  uv Python: $UV_PYTHON_INSTALL_DIR (world-readable)"
        return 0
    fi

    # Default: non-root → legacy user-scoped layout.
    INSTALL_DIR="$MARLOW_HOME/marlow-agent"
}

get_command_link_dir() {
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo "/usr/local/bin"
    else
        echo "$HOME/.local/bin"
    fi
}

get_command_link_display_dir() {
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo '/usr/local/bin'
    else
        echo '~/.local/bin'
    fi
}

get_marlow_command_path() {
    local link_dir
    link_dir="$(get_command_link_dir)"
    if [ -x "$link_dir/marlow" ]; then
        echo "$link_dir/marlow"
    else
        echo "marlow"
    fi
}

# ============================================================================
# System detection
# ============================================================================

detect_os() {
    case "$(uname -s)" in
        Linux*)
            OS="linux"
            if [ -f /etc/os-release ]; then
                . /etc/os-release
                DISTRO="$ID"
            else
                DISTRO="unknown"
            fi
            ;;
        Darwin*)
            OS="macos"
            DISTRO="macos"
            ;;
        *)
            OS="unknown"
            DISTRO="unknown"
            log_warn "Unknown operating system"
            ;;
    esac

    log_success "Detected: $OS ($DISTRO)"
}

# ============================================================================
# Dependency checks
# ============================================================================

install_uv() {
    # Marlow owns its own uv at $MARLOW_HOME/bin/uv.  Always install there —
    # no PATH probing, no conda guards, no multi-location resolution chains.
    # The runtime update path (marlow_cli/managed_uv.py) looks in the same
    # place, so install.sh and `marlow update` stay in sync.
    local _managed_uv="$MARLOW_HOME/bin/uv"

    if [ -x "$_managed_uv" ]; then
        UV_CMD="$_managed_uv"
        UV_VERSION=$($UV_CMD --version 2>/dev/null)
        log_success "Managed uv found ($UV_VERSION)"
        return 0
    fi

    log_info "Installing managed uv into $MARLOW_HOME/bin ..."
    mkdir -p "$MARLOW_HOME/bin"

    # Two-stage: download the installer, then run it.  Piping
    # `curl | sh` masks curl failures (sh exits 0 on empty stdin)
    # and conflates network errors with installer errors.
    local _uv_install_log _uv_installer
    _uv_install_log="$(mktemp 2>/dev/null || echo "/tmp/marlow-uv-install.$$.log")"
    _uv_installer="$(mktemp 2>/dev/null || echo "/tmp/marlow-uv-installer.$$.sh")"
    if ! curl -LsSf https://astral.sh/uv/install.sh -o "$_uv_installer" 2>"$_uv_install_log"; then
        log_error "Failed to download uv installer from https://astral.sh/uv/install.sh"
        log_info "curl output:"
        sed 's/^/    /' "$_uv_install_log" >&2
        log_info "Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        rm -f "$_uv_install_log" "$_uv_installer"
        exit 1
    fi
    # UV_UNMANAGED_INSTALL tells the astral installer to place the binary
    # directly into $MARLOW_HOME/bin instead of ~/.local/bin.
    if UV_UNMANAGED_INSTALL="$MARLOW_HOME/bin" sh "$_uv_installer" >>"$_uv_install_log" 2>&1; then
        rm -f "$_uv_installer"
        if [ -x "$_managed_uv" ]; then
            UV_CMD="$_managed_uv"
        else
            log_error "uv installer reported success but binary not found at $_managed_uv"
            log_info "Installer output:"
            sed 's/^/    /' "$_uv_install_log" >&2
            rm -f "$_uv_install_log"
            exit 1
        fi
        rm -f "$_uv_install_log"
        UV_VERSION=$($UV_CMD --version 2>/dev/null)
        log_success "Managed uv installed ($UV_VERSION)"
    else
        log_error "Failed to install uv"
        log_info "Installer output:"
        sed 's/^/    /' "$_uv_install_log" >&2
        log_info "Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        rm -f "$_uv_install_log" "$_uv_installer"
        exit 1
    fi
}

check_python() {
    log_info "Checking Python $PYTHON_VERSION..."

    # Let uv handle Python — it can download and manage Python versions
    # First check if a suitable Python is already available
    if PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION" 2>/dev/null)"; then
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python found: $PYTHON_FOUND_VERSION"
        return 0
    fi

    # Python not found — use uv to install it (no sudo needed!)
    log_info "Python $PYTHON_VERSION not found, installing via uv..."
    if "$UV_CMD" python install "$PYTHON_VERSION"; then
        PYTHON_PATH="$("$UV_CMD" python find "$PYTHON_VERSION")"
        PYTHON_FOUND_VERSION="$("$PYTHON_PATH" --version 2>/dev/null)"
        log_success "Python installed: $PYTHON_FOUND_VERSION"
    else
        log_error "Failed to install Python $PYTHON_VERSION"
        log_info "Install Python $PYTHON_VERSION manually, then re-run this script"
        exit 1
    fi
}

# Best-effort automatic git provisioning. Git is required to clone the repo,
# and a fresh "normie" machine with no developer tools won't have it. Returns 0
# if git is available afterwards, non-zero otherwise (caller prints manual
# instructions and aborts).
attempt_install_git() {
    case "$OS" in
        macos)
            # Prefer Homebrew — fully headless when present.
            if command -v brew >/dev/null 2>&1; then
                log_info "Installing Git via Homebrew..."
                brew install git >/dev/null 2>&1 || true
                command -v git >/dev/null 2>&1 && return 0
            fi
            # Fall back to Apple Command Line Tools, which provide git AND the
            # compiler some Python wheels need. `xcode-select --install` pops a
            # system dialog (Apple gates CLT behind it — it cannot be fully
            # silent without MDM), so we trigger it and poll for git to appear.
            if command -v xcode-select >/dev/null 2>&1; then
                log_info "Requesting Apple Command Line Tools (provides git + compiler)..."
                log_info "If a macOS dialog appears, click \"Install\" and accept the license."
                xcode-select --install >/dev/null 2>&1 || true
                local waited=0
                local timeout=900
                while [ "$waited" -lt "$timeout" ]; do
                    if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
                        return 0
                    fi
                    sleep 5
                    waited=$((waited + 5))
                    if [ $((waited % 60)) -eq 0 ]; then
                        log_info "Still waiting for Command Line Tools install ($((waited / 60))m)..."
                    fi
                done
            fi
            return 1
            ;;
        linux)
            local sudo_cmd=""
            if [ "$(id -u 2>/dev/null || echo 1000)" -ne 0 ]; then
                command -v sudo >/dev/null 2>&1 && sudo_cmd="sudo"
            fi
            case "$DISTRO" in
                ubuntu|debian)
                    log_info "Installing Git via apt..."
                    $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
                    $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null 2>&1 || true
                    ;;
                fedora)
                    log_info "Installing Git via dnf..."
                    $sudo_cmd dnf install -y git >/dev/null 2>&1 || true
                    ;;
                arch)
                    log_info "Installing Git via pacman..."
                    $sudo_cmd pacman -S --noconfirm git >/dev/null 2>&1 || true
                    ;;
                *)
                    return 1
                    ;;
            esac
            command -v git >/dev/null 2>&1 && return 0
            return 1
            ;;
    esac
    return 1
}

check_git() {
    log_info "Checking Git..."

    # On fresh macOS /usr/bin/git is a stub that exits non-zero until CLT is installed.
    if command -v git &> /dev/null && git --version &> /dev/null; then
        GIT_VERSION=$(git --version | awk '{print $3}')
        log_success "Git $GIT_VERSION found"
        return 0
    fi

    log_error "Git not found"

    # Try to install it automatically before giving up (parity with install.ps1).
    log_info "Attempting to install Git automatically..."
    if attempt_install_git; then
        GIT_VERSION=$(git --version | awk '{print $3}')
        log_success "Git $GIT_VERSION installed"
        return 0
    fi

    log_warn "Could not install Git automatically. Please install it manually:"

    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian)
                    log_info "  sudo apt update && sudo apt install git"
                    ;;
                fedora)
                    log_info "  sudo dnf install git"
                    ;;
                arch)
                    log_info "  sudo pacman -S git"
                    ;;
                *)
                    log_info "  Use your package manager to install git"
                    ;;
            esac
            ;;
        macos)
            log_info "  xcode-select --install"
            log_info "  Or: brew install git"
            ;;
    esac

    exit 1
}

check_node() {
    log_info "Checking Node.js (for browser tools)..."

    if command -v node &> /dev/null; then
        local found_ver=$(node --version)
        log_success "Node.js $found_ver found"
        HAS_NODE=true
        return 0
    fi

    # Check our own managed install from a previous run
    if [ -x "$MARLOW_HOME/node/bin/node" ]; then
        export PATH="$MARLOW_HOME/node/bin:$PATH"
        local found_ver=$("$MARLOW_HOME/node/bin/node" --version)
        log_success "Node.js $found_ver found (Marlow-managed)"
        HAS_NODE=true
        return 0
    fi

    log_info "Node.js not found — installing Node.js $NODE_VERSION LTS..."
    install_node
}

install_node() {
    local arch=$(uname -m)
    local node_arch
    case "$arch" in
        x86_64)        node_arch="x64"    ;;
        aarch64|arm64) node_arch="arm64"  ;;
        armv7l)        node_arch="armv7l" ;;
        *)
            log_warn "Unsupported architecture ($arch) for Node.js auto-install"
            log_info "Install manually: https://nodejs.org/en/download/"
            HAS_NODE=false
            return 0
            ;;
    esac

    local node_os
    case "$OS" in
        linux) node_os="linux"  ;;
        macos) node_os="darwin" ;;
        *)
            log_warn "Unsupported OS for Node.js auto-install"
            HAS_NODE=false
            return 0
            ;;
    esac

    # Resolve the latest v22.x.x tarball name from the index page
    local index_url="https://nodejs.org/dist/latest-v${NODE_VERSION}.x/"
    local tarball_name
    tarball_name=$(curl -fsSL "$index_url" \
        | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.xz" \
        | head -1)

    # Fallback to .tar.gz if .tar.xz not available
    if [ -z "$tarball_name" ]; then
        tarball_name=$(curl -fsSL "$index_url" \
            | grep -oE "node-v${NODE_VERSION}\.[0-9]+\.[0-9]+-${node_os}-${node_arch}\.tar\.gz" \
            | head -1)
    fi

    if [ -z "$tarball_name" ]; then
        log_warn "Could not find Node.js $NODE_VERSION binary for $node_os-$node_arch"
        log_info "Install manually: https://nodejs.org/en/download/"
        HAS_NODE=false
        return 0
    fi

    local download_url="${index_url}${tarball_name}"
    local tmp_dir
    tmp_dir=$(mktemp -d)

    log_info "Downloading $tarball_name..."
    if ! curl -fsSL "$download_url" -o "$tmp_dir/$tarball_name"; then
        log_warn "Download failed"
        rm -rf "$tmp_dir"
        HAS_NODE=false
        return 0
    fi

    log_info "Extracting to ~/.marlow/node/..."
    if [[ "$tarball_name" == *.tar.xz ]]; then
        tar xf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    else
        tar xzf "$tmp_dir/$tarball_name" -C "$tmp_dir"
    fi

    local extracted_dir
    extracted_dir=$(ls -d "$tmp_dir"/node-v* 2>/dev/null | head -1)

    if [ ! -d "$extracted_dir" ]; then
        log_warn "Extraction failed"
        rm -rf "$tmp_dir"
        HAS_NODE=false
        return 0
    fi

    # Place into ~/.marlow/node/ and symlink binaries to ~/.local/bin/
    rm -rf "$MARLOW_HOME/node"
    mkdir -p "$MARLOW_HOME"
    mv "$extracted_dir" "$MARLOW_HOME/node"
    rm -rf "$tmp_dir"

    mkdir -p "$HOME/.local/bin"
    ln -sf "$MARLOW_HOME/node/bin/node" "$HOME/.local/bin/node"
    ln -sf "$MARLOW_HOME/node/bin/npm"  "$HOME/.local/bin/npm"
    ln -sf "$MARLOW_HOME/node/bin/npx"  "$HOME/.local/bin/npx"

    export PATH="$MARLOW_HOME/node/bin:$PATH"

    local installed_ver
    installed_ver=$("$MARLOW_HOME/node/bin/node" --version 2>/dev/null)
    log_success "Node.js $installed_ver installed to ~/.marlow/node/"
    HAS_NODE=true
}

check_network_prerequisites() {
    log_info "Checking internet connectivity for package install and web tools..."

    local url
    local failed=false
    local checks=("https://pypi.org/simple/" "https://duckduckgo.com/")

    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl not found; skipping connectivity probes"
        return 0
    fi

    for url in "${checks[@]}"; do
        if ! curl -fsSI --max-time 8 "$url" >/dev/null 2>&1; then
            failed=true
            log_warn "Could not reach $url"
        fi
    done

    if [ "$failed" = false ]; then
        log_success "Internet connectivity looks good"
        return 0
    fi

    log_warn "Network checks failed. Marlow install may complete, but web search and dependency downloads can fail."
    log_info "Verify internet/DNS and retry if dependency installation fails."
}

install_system_packages() {
    # Detect what's missing
    HAS_RIPGREP=false
    local need_ripgrep=false

    log_info "Checking ripgrep (fast file search)..."
    if command -v rg &> /dev/null; then
        log_success "$(rg --version | head -1) found"
        HAS_RIPGREP=true
    else
        need_ripgrep=true
    fi

    # Nothing to install — done
    if [ "$need_ripgrep" = false ]; then
        return 0
    fi

    # Build a human-readable description + package list
    local desc_parts=()
    local pkgs=()
    if [ "$need_ripgrep" = true ]; then
        desc_parts+=("ripgrep for faster file search")
        pkgs+=("ripgrep")
    fi
    local description
    description=$(IFS=" and "; echo "${desc_parts[*]}")

    # ── macOS: brew ──
    if [ "$OS" = "macos" ]; then
        if command -v brew &> /dev/null; then
            log_info "Installing ${pkgs[*]} via Homebrew..."
            if brew install "${pkgs[@]}"; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                return 0
            fi
        fi
        log_warn "Could not auto-install (brew not found or install failed)"
        log_info "Install manually: brew install ${pkgs[*]}"
        return 0
    fi

    # ── Linux: resolve package manager command ──
    local pkg_install=""
    case "$DISTRO" in
        ubuntu|debian) pkg_install="apt install -y"   ;;
        fedora)        pkg_install="dnf install -y"   ;;
        arch)          pkg_install="pacman -S --noconfirm" ;;
    esac

    if [ -n "$pkg_install" ]; then
        local install_cmd="$pkg_install ${pkgs[*]}"

        # Prevent needrestart/whiptail dialogs from blocking non-interactive installs
        case "$DISTRO" in
            ubuntu|debian) export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a ;;
        esac

        # Already root — just install
        if [ "$(id -u)" -eq 0 ]; then
            log_info "Installing ${pkgs[*]}..."
            if $install_cmd; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                return 0
            fi
        # Passwordless sudo — just install
        elif command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
            log_info "Installing ${pkgs[*]}..."
            if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                return 0
            fi
        # sudo needs password — ask once for everything
        elif command -v sudo &> /dev/null; then
            if [ "$IS_INTERACTIVE" = true ]; then
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Marlow Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}? (requires sudo)" "no"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd; then
                        [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                        return 0
                    fi
                fi
            elif (: </dev/tty) 2>/dev/null; then
                # Non-interactive (e.g. curl | bash) but a terminal is available.
                # Read the prompt from /dev/tty (same approach the setup wizard uses).
                # Probe by actually opening /dev/tty: a bare existence test passes
                # in Docker builds where the device node is in the mount namespace
                # but opening fails with ENXIO. See #16746.
                echo ""
                log_info "sudo is needed ONLY to install optional system packages (${pkgs[*]}) via your package manager."
                log_info "Marlow Agent itself does not require or retain root access."
                if prompt_yes_no "Install ${description}?" "yes"; then
                    if sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a $install_cmd < /dev/tty; then
                        [ "$need_ripgrep" = true ] && HAS_RIPGREP=true && log_success "ripgrep installed"
                        return 0
                    fi
                fi
            else
                log_warn "Non-interactive mode and no terminal available — cannot install system packages"
                log_info "Install manually after setup completes: sudo $install_cmd"
            fi
        fi
    fi

    # ── Fallback for ripgrep: cargo ──
    if [ "$need_ripgrep" = true ] && [ "$HAS_RIPGREP" = false ]; then
        if command -v cargo &> /dev/null; then
            log_info "Trying cargo install ripgrep (no sudo needed)..."
            if cargo install ripgrep; then
                log_success "ripgrep installed via cargo"
                HAS_RIPGREP=true
            fi
        fi
    fi

    # ── Show manual instructions for anything still missing ──
    if [ "$HAS_RIPGREP" = false ] && [ "$need_ripgrep" = true ]; then
        log_warn "ripgrep not installed (file search will use grep fallback)"
        show_manual_install_hint "ripgrep"
    fi

}

show_manual_install_hint() {
    local pkg="$1"
    log_info "To install $pkg manually:"
    case "$OS" in
        linux)
            case "$DISTRO" in
                ubuntu|debian) log_info "  sudo apt install $pkg" ;;
                fedora)        log_info "  sudo dnf install $pkg" ;;
                arch)          log_info "  sudo pacman -S $pkg"   ;;
                *)             log_info "  Use your package manager or visit the project homepage" ;;
            esac
            ;;
        macos) log_info "  brew install $pkg" ;;
    esac
}

# ============================================================================
# Installation
# ============================================================================

clone_repo() {
    log_info "Installing to $INSTALL_DIR..."

    if [ -d "$INSTALL_DIR" ]; then
        if [ -d "$INSTALL_DIR/.git" ]; then
            log_info "Existing installation found, updating..."
            cd "$INSTALL_DIR"

            local autostash_ref=""
            if [ -n "$(git status --porcelain)" ]; then
                local stash_name
                stash_name="marlow-install-autostash-$(date -u +%Y%m%d-%H%M%S)"
                log_info "Local changes detected, stashing before update..."
                git stash push --include-untracked -m "$stash_name"
                autostash_ref="stash@{0}"
            fi

            git fetch origin
            git checkout "$BRANCH"
            git pull --ff-only origin "$BRANCH"

            if [ -n "$autostash_ref" ]; then
                local restore_now="yes"
                if [ -t 0 ] && [ -t 1 ]; then
                    echo
                    log_warn "Local changes were stashed before updating."
                    log_warn "Restoring them may reapply local customizations onto the updated codebase."
                    printf "Restore local changes now? [Y/n] "
                    read -r restore_answer
                    case "$restore_answer" in
                        ""|y|Y|yes|YES|Yes) restore_now="yes" ;;
                        *) restore_now="no" ;;
                    esac
                fi

                if [ "$restore_now" = "yes" ]; then
                    log_info "Restoring local changes..."
                    if git stash apply "$autostash_ref"; then
                        git stash drop "$autostash_ref" >/dev/null
                        log_warn "Local changes were restored on top of the updated codebase."
                        log_warn "Review git diff / git status if Marlow behaves unexpectedly."
                    else
                        log_error "Update succeeded, but restoring local changes failed. Your changes are still preserved in git stash."
                        log_info "Resolve manually with: git stash apply $autostash_ref"
                        exit 1
                    fi
                else
                    log_info "Skipped restoring local changes."
                    log_info "Your changes are still preserved in git stash."
                    log_info "Restore manually with: git stash apply $autostash_ref"
                fi
            fi
        else
            log_error "Directory exists but is not a git repository: $INSTALL_DIR"
            log_info "Remove it or choose a different directory with --dir"
            exit 1
        fi
    else
        # Try SSH first (for private repo access), fall back to HTTPS
        # GIT_SSH_COMMAND disables interactive prompts and sets a short timeout
        # so SSH fails fast instead of hanging when no key is configured.
        log_info "Trying SSH clone..."
        if GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5" \
           git clone --branch "$BRANCH" "$REPO_URL_SSH" "$INSTALL_DIR" 2>/dev/null; then
            log_success "Cloned via SSH"
        else
            rm -rf "$INSTALL_DIR" 2>/dev/null  # Clean up partial SSH clone
            log_info "SSH failed, trying HTTPS..."
            if git clone --branch "$BRANCH" "$REPO_URL_HTTPS" "$INSTALL_DIR"; then
                log_success "Cloned via HTTPS"
            else
                log_error "Failed to clone repository"
                exit 1
            fi
        fi
    fi

    cd "$INSTALL_DIR"

    if [ -n "$INSTALL_COMMIT" ]; then
        log_info "Pinning checkout to commit $INSTALL_COMMIT..."
        if ! git cat-file -e "$INSTALL_COMMIT^{commit}" 2>/dev/null; then
            git fetch origin "$INSTALL_COMMIT" || true
        fi
        git checkout --detach "$INSTALL_COMMIT"
    fi

    log_success "Repository ready"
}

setup_venv() {
    if [ "$USE_VENV" = false ]; then
        log_info "Skipping virtual environment (--no-venv)"
        return 0
    fi

    log_info "Creating virtual environment with Python $PYTHON_VERSION..."

    if [ -d "venv" ]; then
        log_info "Virtual environment already exists, recreating..."
        rm -rf venv
    fi

    # uv creates the venv and pins the Python version in one step
    $UV_CMD venv venv --python "$PYTHON_VERSION"

    log_success "Virtual environment ready (Python $PYTHON_VERSION)"
}

install_deps() {
    log_info "Installing dependencies..."

    if [ "$USE_VENV" = true ]; then
        # Tell uv to install into our venv (no need to activate)
        export VIRTUAL_ENV="$INSTALL_DIR/venv"
    fi

    # On Debian/Ubuntu, some Python packages need build tools.
    # Check and offer to install them if missing.
    if [ "$DISTRO" = "ubuntu" ] || [ "$DISTRO" = "debian" ]; then
        local need_build_tools=false
        for pkg in gcc python3-dev libffi-dev; do
            if ! dpkg -s "$pkg" &>/dev/null; then
                need_build_tools=true
                break
            fi
        done
        if [ "$need_build_tools" = true ]; then
            log_info "Some build tools may be needed for Python packages..."
            if command -v sudo &> /dev/null; then
                if sudo -n true 2>/dev/null; then
                    sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                    log_success "Build tools installed"
                else
                    log_info "sudo is needed ONLY to install build tools (build-essential, python3-dev, libffi-dev) via apt."
                    log_info "Marlow Agent itself does not require or retain root access."
                    if prompt_yes_no "Install build tools?" "yes"; then
                        sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a apt-get install -y -qq build-essential python3-dev libffi-dev >/dev/null 2>&1 || true
                        log_success "Build tools installed"
                    fi
                fi
            fi
        fi
    fi

    # Install the main package in editable mode with all extras.
    #
    # Hash-verified install (Tier 0) — when uv.lock is present, prefer
    # `uv sync --locked`. The lockfile records SHA256 hashes for every
    # transitive, so a compromised transitive (different hash than what
    # we shipped) is REJECTED by the resolver. This is the *only* path
    # that protects against the "direct dep is fine, but the dep's dep
    # got worm-poisoned overnight" failure mode. All `uv pip install`
    # tiers below re-resolve transitives fresh from PyPI without any
    # hash verification — they exist to keep installs working when the
    # lockfile is stale, missing, or out-of-sync with the current
    # extras spec, NOT because they're equivalent in posture.
    if [ -f "uv.lock" ]; then
        log_info "Trying tier: hash-verified (uv.lock) ..."
        log_info "(this resolves + downloads the curated [all] set — first run on a"
        log_info " fresh venv can take 1-5 minutes; uv prints progress below)"
        # Stream uv's progress directly to the user instead of swallowing
        # it with `2>"$(mktemp)"`.  Two reasons:
        #   1. `--extra all --locked` against a fresh venv has to pull
        #      every transitive — silencing stderr makes the install
        #      look frozen for minutes on slow networks. Users see
        #      "Trying tier: hash-verified ..." and assume it's hung.
        #   2. The previous `2>"$(mktemp)"` substituted the path at
        #      command-build time but never saved it, so on failure the
        #      uv error message was unreachable — the user just got the
        #      generic "lockfile may be stale" warning.
        #
        # Critical flag choice: `--extra all`, NOT `--all-extras`.
        #   --all-extras = every [project.optional-dependencies] key.
        #                  This bypasses the curated `[all]` extra
        #                  entirely and pulls extras outside the curated set.
        #   --extra all  = install just the `[all]` extra's contents.
        #                  This respects the curation in pyproject.toml.
        # uv's own progress UI handles TTY detection and downgrades
        # gracefully when stdout/stderr aren't terminals.
        if UV_PROJECT_ENVIRONMENT="$INSTALL_DIR/venv" $UV_CMD sync --extra all --locked; then
            log_success "Main package installed (hash-verified via uv.lock)"
            log_success "All dependencies installed"
            return 0
        fi
        log_warn "uv.lock sync failed (see uv output above), falling back to PyPI resolve..."
    else
        log_info "uv.lock not found — falling back to PyPI resolve (no hash verification)"
    fi

    # Multi-tier fallback. The point of the tiers is that ONE compromised
    # PyPI package (a worm-poisoned release that gets quarantined, like
    # mistralai 2.4.6 in May 2026) shouldn't be able to silently demote a
    # fresh install all the way down to "core only" — the user should keep
    # everything else they signed up for.
    #
    # Tier 1: [all] — the curated extra in pyproject.toml.
    # Tier 2: [all] minus the currently-broken extras list (_BROKEN_EXTRAS).
    #         Edit _BROKEN_EXTRAS below when something on PyPI breaks; this
    #         lets users keep the rest of [all] when one transitive is
    #         unavailable. The list of [all]'s contents is parsed from
    #         pyproject.toml at runtime — there is NO hand-mirrored copy
    #         to drift out of sync. If you want to change what [all]
    #         contains, edit pyproject.toml only.
    # Tier 3: bare `.` — last-resort so at least the core CLI launches.
    #         A separate PyPI-only tier has no remaining content.
    local _BROKEN_EXTRAS=()  # populate when an extra becomes unresolvable

    # Parse [project.optional-dependencies].all from pyproject.toml.
    # tomllib is stdlib on Python 3.11+ which uv's bootstrap guarantees.
    # Falls back to a hand list if parse fails — defensive only.
    local _ALL_EXTRAS_CSV
    _ALL_EXTRAS_CSV="$(
        "$PYTHON_PATH" - <<'PY' 2>/dev/null
import re, sys, tomllib
try:
    with open("pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    specs = data["project"]["optional-dependencies"]["all"]
    extras = []
    for s in specs:
        m = re.search(r"marlow-agent\[([\w-]+)\]", s)
        if m:
            extras.append(m.group(1))
    print(",".join(extras))
except Exception as e:
    print("", file=sys.stderr)
    sys.exit(1)
PY
    )"
    if [ -z "$_ALL_EXTRAS_CSV" ]; then
        log_warn "Could not parse [all] from pyproject.toml; falling back to .[all] only."
        _ALL_EXTRAS_CSV=""
    fi

    # Build "[all] minus broken" spec by filtering the parsed list.
    local _SAFE_SPEC=".[all]"
    if [ -n "$_ALL_EXTRAS_CSV" ] && [ "${#_BROKEN_EXTRAS[@]}" -gt 0 ]; then
        local _SAFE_EXTRAS=()
        local _e _b _skip
        IFS=',' read -ra _ALL_EXTRAS_ARR <<< "$_ALL_EXTRAS_CSV"
        for _e in "${_ALL_EXTRAS_ARR[@]}"; do
            _skip=false
            for _b in "${_BROKEN_EXTRAS[@]}"; do
                if [ "$_e" = "$_b" ]; then _skip=true; break; fi
            done
            if [ "$_skip" = false ]; then _SAFE_EXTRAS+=("$_e"); fi
        done
        _SAFE_SPEC=".[$(IFS=,; echo "${_SAFE_EXTRAS[*]}")]"
    fi

    ALL_INSTALL_LOG=$(mktemp)
    local _installed=false
    local _tier_name=""

    install_tier() {
        local name="$1"; local spec="$2"
        log_info "Trying tier: $name ..."
        if $UV_CMD pip install -e "$spec" 2>"$ALL_INSTALL_LOG"; then
            log_success "Main package installed ($name)"
            _installed=true
            _tier_name="$name"
            return 0
        fi
        log_warn "Tier '$name' failed. Top of pip output:"
        head -5 "$ALL_INSTALL_LOG" | sed 's/^/    /' >&2
        return 1
    }

    install_tier "all" ".[all]" \
        || install_tier "all minus known-broken (${_BROKEN_EXTRAS[*]:-none})" "$_SAFE_SPEC" \
        || install_tier "core only (no extras)" "."

    rm -f "$ALL_INSTALL_LOG"

    if [ "$_installed" = false ]; then
        log_error "Package installation failed even with no extras."
        log_info "Check that build tools are installed: sudo apt install build-essential python3-dev"
        log_info "Then re-run: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        exit 1
    fi

    if [ "$_tier_name" != "all" ]; then
        log_warn "Note: installed via fallback tier ($_tier_name)."
        log_info "Some optional features may be missing. After resolving any"
        log_info "PyPI/network issue, re-run: $UV_CMD pip install -e '.[all]'"
    fi

    log_success "Main package installed"

    log_success "All dependencies installed"
}

setup_path() {
    log_info "Setting up marlow command..."

    if [ "$USE_VENV" = true ]; then
        MARLOW_BIN="$INSTALL_DIR/venv/bin/marlow"
    else
        MARLOW_BIN="$(which marlow 2>/dev/null || echo "")"
        if [ -z "$MARLOW_BIN" ]; then
            log_warn "marlow not found on PATH after install"
            return 0
        fi
    fi

    # Verify the entry point script was actually generated
    if [ ! -x "$MARLOW_BIN" ]; then
        log_warn "marlow entry point not found at $MARLOW_BIN"
        log_info "This usually means the pip install didn't complete successfully."
        log_info "Try: cd $INSTALL_DIR && uv pip install -e '.[all]'"
        return 0
    fi

    local command_link_dir
    local command_link_display_dir
    command_link_dir="$(get_command_link_dir)"
    command_link_display_dir="$(get_command_link_display_dir)"

    # Create a user-facing shim for the marlow command.
    # We intentionally clear PYTHONPATH/PYTHONHOME here so inherited env vars
    # can't make this launcher import modules from another checkout.
    mkdir -p "$command_link_dir"
    # Older installs created this path as a symlink to $MARLOW_BIN. Without
    # the rm, `cat >` follows the symlink and overwrites the venv pip entry
    # point with this shim — making `exec "$MARLOW_BIN"` self-recurse. (#21454)
    rm -f "$command_link_dir/marlow"
    cat > "$command_link_dir/marlow" <<EOF
#!/usr/bin/env bash
unset PYTHONPATH
unset PYTHONHOME
exec "$MARLOW_BIN" "\$@"
EOF
    chmod +x "$command_link_dir/marlow"
    log_success "Installed marlow launcher → $command_link_display_dir/marlow"

    # Retire only a launcher positively identified as belonging to the old
    # project. The helper preserves it as a backup and ignores unrelated tools.
    retire_legacy_hermes_launcher "$command_link_dir"

    # FHS layout: /usr/local/bin is normally on PATH for login shells (via
    # /etc/profile pathmunge), but on RHEL/CentOS/Rocky/Alma 8+ non-login
    # interactive root shells (su, sudo -s, tmux panes, some web terminals)
    # only source /etc/bashrc, which does NOT add /usr/local/bin — and
    # /root/.bash_profile doesn't either.  So verify with `command -v` and
    # fall back to writing a PATH guard into /root/.bashrc when needed.
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        export PATH="$command_link_dir:$PATH"
        # Probe a fresh non-login interactive bash the way the user will use it.
        # `bash -i -c` sources ~/.bashrc but NOT ~/.bash_profile or /etc/profile,
        # which is the exact scenario where RHEL root loses /usr/local/bin.
        if env -i HOME="$HOME" TERM="${TERM:-dumb}" bash -i -c 'command -v marlow' \
                >/dev/null 2>&1; then
            log_info "/usr/local/bin is already on PATH for all shells"
            log_success "marlow command ready"
            return 0
        fi

        log_info "marlow not on PATH in non-login shells (common on RHEL-family)"
        PATH_LINE='export PATH="/usr/local/bin:$PATH"'
        PATH_COMMENT='# Marlow Agent — ensure /usr/local/bin is on PATH (RHEL non-login shells)'
        for SHELL_CONFIG in "$HOME/.bashrc" "$HOME/.bash_profile"; do
            [ -f "$SHELL_CONFIG" ] || continue
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null \
                    | grep -qE 'PATH=.*(/usr/local/bin|\$command_link_dir)'; then
                echo "" >> "$SHELL_CONFIG"
                echo "$PATH_COMMENT" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added /usr/local/bin to PATH in $SHELL_CONFIG"
            fi
        done
        log_success "marlow command ready"
        return 0
    fi

    # Check if ~/.local/bin is on PATH; if not, add it to shell config.
    # Detect the user's actual login shell (not the shell running this script,
    # which is always bash when piped from curl).
    if ! echo "$PATH" | tr ':' '\n' | grep -q "^$command_link_dir$"; then
        SHELL_CONFIGS=()
        IS_FISH=false
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        case "$LOGIN_SHELL" in
            zsh)
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                [ -f "$HOME/.zprofile" ] && SHELL_CONFIGS+=("$HOME/.zprofile")
                # If neither exists, create ~/.zshrc (common on fresh macOS installs)
                if [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
                    touch "$HOME/.zshrc"
                    SHELL_CONFIGS+=("$HOME/.zshrc")
                fi
                ;;
            bash)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.bash_profile" ] && SHELL_CONFIGS+=("$HOME/.bash_profile")
                ;;
            fish)
                # fish uses ~/.config/fish/config.fish and fish_add_path — not export PATH=
                IS_FISH=true
                FISH_CONFIG="$HOME/.config/fish/config.fish"
                mkdir -p "$(dirname "$FISH_CONFIG")"
                touch "$FISH_CONFIG"
                ;;
            *)
                [ -f "$HOME/.bashrc" ] && SHELL_CONFIGS+=("$HOME/.bashrc")
                [ -f "$HOME/.zshrc" ] && SHELL_CONFIGS+=("$HOME/.zshrc")
                ;;
        esac
        # Also ensure ~/.profile has it (sourced by login shells on
        # Ubuntu/Debian even when ~/.bashrc is skipped).
        [ "$IS_FISH" = "false" ] && [ -f "$HOME/.profile" ] && SHELL_CONFIGS+=("$HOME/.profile")

        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

        for SHELL_CONFIG in "${SHELL_CONFIGS[@]}"; do
            if ! grep -v '^[[:space:]]*#' "$SHELL_CONFIG" 2>/dev/null | grep -qE 'PATH=.*\.local/bin'; then
                echo "" >> "$SHELL_CONFIG"
                echo "# Marlow Agent — ensure ~/.local/bin is on PATH" >> "$SHELL_CONFIG"
                echo "$PATH_LINE" >> "$SHELL_CONFIG"
                log_success "Added ~/.local/bin to PATH in $SHELL_CONFIG"
            fi
        done

        # fish uses fish_add_path instead of export PATH=...
        if [ "$IS_FISH" = "true" ]; then
            if ! grep -q 'fish_add_path.*\.local/bin' "$FISH_CONFIG" 2>/dev/null; then
                echo "" >> "$FISH_CONFIG"
                echo "# Marlow Agent — ensure ~/.local/bin is on PATH" >> "$FISH_CONFIG"
                echo 'fish_add_path "$HOME/.local/bin"' >> "$FISH_CONFIG"
                log_success "Added ~/.local/bin to PATH in $FISH_CONFIG"
            fi
        fi

        if [ "$IS_FISH" = "false" ] && [ ${#SHELL_CONFIGS[@]} -eq 0 ]; then
            log_warn "Could not detect shell config file to add ~/.local/bin to PATH"
            log_info "Add manually: $PATH_LINE"
        fi
    else
        log_info "~/.local/bin already on PATH"
    fi

    # Export for current session so marlow works immediately
    export PATH="$command_link_dir:$PATH"

    log_success "marlow command ready"
}

copy_config_templates() {
    log_info "Setting up configuration files..."

    # Create ~/.marlow directory structure (config at top level, code in subdir)
    mkdir -p "$MARLOW_HOME"/{cron,sessions,logs,pairing,hooks,image_cache,audio_cache,memories,skills}

    # Create .env at ~/.marlow/.env (top level, easy to find)
    if [ ! -f "$MARLOW_HOME/.env" ]; then
        if [ -f "$INSTALL_DIR/.env.example" ]; then
            cp "$INSTALL_DIR/.env.example" "$MARLOW_HOME/.env"
            log_success "Created ~/.marlow/.env from template"
        else
            touch "$MARLOW_HOME/.env"
            log_success "Created ~/.marlow/.env"
        fi
    else
        log_info "~/.marlow/.env already exists, keeping it"
    fi
    # Restrict .env permissions — this file holds API keys and tokens.
    # 0600 ensures only the file owner can read/write, matching standard
    # practice for credential files (.netrc, .aws/credentials, .ssh/config).
    chmod 600 "$MARLOW_HOME/.env"
    configure_browser_env_from_system_browser

    # Create config.yaml at ~/.marlow/config.yaml (top level, easy to find)
    if [ ! -f "$MARLOW_HOME/config.yaml" ]; then
        if [ -f "$INSTALL_DIR/cli-config.yaml.example" ]; then
            cp "$INSTALL_DIR/cli-config.yaml.example" "$MARLOW_HOME/config.yaml"
            log_success "Created ~/.marlow/config.yaml from template"
        fi
    else
        log_info "~/.marlow/config.yaml already exists, keeping it"
    fi

    # Create SOUL.md if it doesn't exist (global persona file)
    if [ ! -f "$MARLOW_HOME/SOUL.md" ]; then
        cat > "$MARLOW_HOME/SOUL.md" << 'SOUL_EOF'
# Marlow Agent Persona

<!--
This file defines the agent's personality and tone.
The agent will embody whatever you write here.
Edit this to customize how Marlow communicates with you.

Examples:
  - "You are a warm, playful assistant who uses kaomoji occasionally."
  - "You are a concise technical expert. No fluff, just facts."
  - "You speak like a friendly coworker who happens to know everything."

This file is loaded fresh each message -- no restart needed.
Delete the contents (or this file) to use the default personality.
-->
SOUL_EOF
        log_success "Created ~/.marlow/SOUL.md (edit to customize personality)"
    fi

    log_success "Configuration directory ready: ~/.marlow/"

    # Seed bundled skills into ~/.marlow/skills/ (manifest-based, one-time per skill)
    if [ "$NO_SKILLS" = true ]; then
        # Blank-slate install: write the opt-out marker and skip seeding.
        # skills_sync.py and `marlow update` both honor this marker, so the
        # default profile stays empty across future updates too.
        printf '%s\n' \
            "This profile opted out of bundled-skill seeding (installed with --no-skills)." \
            "Delete this file to re-enable sync on the next 'marlow update'." \
            > "$MARLOW_HOME/.no-bundled-skills" 2>/dev/null || true
        log_info "Skipping bundled skills (--no-skills). Wrote $MARLOW_HOME/.no-bundled-skills"
        log_info "  Future 'marlow update' runs will not inject bundled skills. Delete the marker to opt back in."
    else
        log_info "Syncing bundled skills to ~/.marlow/skills/ ..."
        if "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/tools/skills_sync.py" 2>/dev/null; then
            log_success "Skills synced to ~/.marlow/skills/"
        else
            # Fallback: simple directory copy if Python sync fails
            if [ -d "$INSTALL_DIR/skills" ] && [ ! "$(ls -A "$MARLOW_HOME/skills/" 2>/dev/null | grep -v '.bundled_manifest')" ]; then
                cp -r "$INSTALL_DIR/skills/"* "$MARLOW_HOME/skills/" 2>/dev/null || true
                log_success "Skills copied to ~/.marlow/skills/"
            fi
        fi
    fi
}

find_system_browser() {
    # Prefer a user-specified browser path, then common Linux/macOS Chrome and
    # Chromium command names.  Arch-family distributions commonly ship plain
    # `chromium`, while Debian-family systems often use `chromium-browser`.
    if [ -n "${AGENT_BROWSER_EXECUTABLE_PATH:-}" ]; then
        if [ -x "$AGENT_BROWSER_EXECUTABLE_PATH" ]; then
            echo "$AGENT_BROWSER_EXECUTABLE_PATH"
            return 0
        fi
        if command -v "$AGENT_BROWSER_EXECUTABLE_PATH" >/dev/null 2>&1; then
            command -v "$AGENT_BROWSER_EXECUTABLE_PATH"
            return 0
        fi
    fi

    local candidate
    for candidate in google-chrome google-chrome-stable chromium chromium-browser chrome; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done

    if [ "$(uname)" = "Darwin" ]; then
        for app in \
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
            "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
            if [ -x "$app" ]; then
                echo "$app"
                return 0
            fi
        done
    fi

    return 1
}

run_browser_install_with_timeout() {
    local timeout_seconds="$1"
    shift

    if command -v timeout >/dev/null 2>&1; then
        timeout "$timeout_seconds" "$@"
    else
        "$@"
    fi
}

configure_browser_env_from_system_browser() {
    local env_file="$MARLOW_HOME/.env"
    local browser_path="${DETECTED_BROWSER_EXECUTABLE:-}"

    if [ -z "$browser_path" ]; then
        browser_path="$(find_system_browser 2>/dev/null || true)"
    fi

    if [ -z "$browser_path" ]; then
        return 0
    fi

    mkdir -p "$MARLOW_HOME"
    if [ ! -f "$env_file" ]; then
        touch "$env_file"
    fi

    if grep -q '^AGENT_BROWSER_EXECUTABLE_PATH=' "$env_file" 2>/dev/null; then
        log_info "AGENT_BROWSER_EXECUTABLE_PATH already configured"
        return 0
    fi

    {
        echo ""
        echo "# Marlow Agent browser tools — use the system Chrome/Chromium binary."
        echo "AGENT_BROWSER_EXECUTABLE_PATH=$browser_path"
    } >> "$env_file"
    log_success "Configured browser tools to use $browser_path"
}

install_node_deps() {
    if [ "$HAS_NODE" = false ]; then
        log_info "Skipping Node.js dependencies (Node not installed)"
        return 0
    fi

    if [ -f "$INSTALL_DIR/package.json" ]; then
        log_info "Installing Node.js dependencies (browser tools)..."
        cd "$INSTALL_DIR"
        npm install --silent 2>/dev/null || {
            log_warn "npm install failed (browser tools may not work)"
        }
        log_success "Node.js dependencies installed"

        # Install Playwright browser + system dependencies.
        # Playwright's --with-deps only supports apt-based systems natively.
        # For Arch/Manjaro we install the system libs via pacman first.
        # Other systems must install Chromium dependencies manually.
        if [ "$SKIP_BROWSER" = true ]; then
            log_info "Skipping Playwright/Chromium install (--skip-browser)"
            log_info "Browser tools will be unavailable until you run manually:"
            log_info "  cd $INSTALL_DIR && npx playwright install chromium"
            log_info "On apt-based systems, an admin also needs to run:"
            log_info "  sudo npx playwright install-deps chromium"
        else
        log_info "Installing browser engine (Playwright Chromium)..."
        DETECTED_BROWSER_EXECUTABLE="$(find_system_browser 2>/dev/null || true)"
        if [ -n "$DETECTED_BROWSER_EXECUTABLE" ]; then
            log_success "Found system Chrome/Chromium at $DETECTED_BROWSER_EXECUTABLE"
            log_info "Skipping Playwright browser download; Marlow will use the system browser."
        else
            case "$DISTRO" in
                ubuntu|debian|raspbian|pop|linuxmint|elementary|zorin|kali|parrot)
                    # Use --with-deps only when sudo is available non-interactively
                    # (root, or a user with passwordless sudo). Non-sudo users
                    # — typical for systemd service accounts and unprivileged
                    # operator users — would otherwise get blocked on an
                    # interactive sudo prompt that they can't satisfy. Fall back
                    # to the browser-only install in that case, and print the
                    # exact command the admin needs to run separately.
                    if [ "$(id -u)" -eq 0 ] || (command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null); then
                        log_info "Installing Playwright Chromium with system dependencies..."
                        cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install --with-deps chromium 2>/dev/null || {
                            log_warn "Playwright browser installation failed — browser tools will not work."
                            log_warn "Try running manually: cd $INSTALL_DIR && npx playwright install --with-deps chromium"
                        }
                    else
                        log_warn "No sudo available — skipping system-library install (--with-deps)."
                        log_info "Ask an administrator to run, one time, as root:"
                        log_info "  sudo npx playwright install-deps chromium"
                        log_info "  (from $INSTALL_DIR, after Node.js deps are installed)"
                        log_info "Installing Chromium binary into this user's Playwright cache..."
                        cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install chromium 2>/dev/null || {
                            log_warn "Playwright browser installation failed — browser tools will not work."
                            log_warn "Try running manually: cd $INSTALL_DIR && npx playwright install chromium"
                        }
                    fi
                    ;;
                arch|manjaro|cachyos|endeavouros|garuda)
                    if command -v pacman &> /dev/null; then
                        log_info "Arch-family distro detected — installing Chromium system dependencies via pacman..."
                        if command -v sudo &> /dev/null && sudo -n true 2>/dev/null; then
                            sudo NEEDRESTART_MODE=a pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        elif [ "$(id -u)" -eq 0 ]; then
                            pacman -S --noconfirm --needed \
                                nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib >/dev/null 2>&1 || true
                        else
                            log_warn "Cannot install browser deps without sudo. Run manually:"
                            log_warn "  sudo pacman -S nss atk at-spi2-core cups libdrm libxkbcommon mesa pango cairo alsa-lib"
                        fi
                    fi
                    cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install chromium 2>/dev/null || {
                        log_warn "Playwright browser installation failed — browser tools will not work."
                    }
                    ;;
                fedora|rhel|centos|rocky|alma)
                    log_warn "Playwright does not support automatic dependency installation on RPM-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo dnf install nss atk at-spi2-core cups-libs libdrm libxkbcommon mesa-libgbm pango cairo alsa-lib"
                    cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install chromium 2>/dev/null || {
                        log_warn "Playwright browser installation failed — install dependencies above and retry."
                    }
                    ;;
                opensuse*|sles)
                    log_warn "Playwright does not support automatic dependency installation on zypper-based systems."
                    log_info "Install Chromium system dependencies manually before using browser tools:"
                    log_info "  sudo zypper install mozilla-nss libatk-1_0-0 at-spi2-core cups-libs libdrm2 libxkbcommon0 Mesa-libgbm1 pango cairo libasound2"
                    cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install chromium 2>/dev/null || {
                        log_warn "Playwright browser installation failed — install dependencies above and retry."
                    }
                    ;;
                *)
                    log_warn "Playwright does not support automatic dependency installation on $DISTRO."
                    log_info "Install Chromium/browser system dependencies for your distribution, then run:"
                    log_info "  cd $INSTALL_DIR && npx playwright install chromium"
                    log_info "Browser tools will not work until dependencies are installed."
                    cd "$INSTALL_DIR" && run_browser_install_with_timeout 600 npx playwright install chromium 2>/dev/null || true
                    ;;
            esac
        fi
        fi
        log_success "Browser engine setup complete"
    fi

    # Keep the checkout clean so `marlow update` doesn't autostash every run.
    restore_dirty_lockfiles "$INSTALL_DIR"
}

run_setup_wizard() {
    if [ "$RUN_SETUP" = false ]; then
        log_info "Skipping setup wizard (--skip-setup)"
        return 0
    fi

    # The setup wizard reads from /dev/tty, so it works even when the
    # install script itself is piped (curl | bash). Only skip if no
    # terminal is available at all (e.g. Docker build, CI).
    #
    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO, so the wizard would proceed and
    # then crash on `< /dev/tty` below.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Setup wizard skipped (no terminal available). Run 'marlow setup' after install."
        return 0
    fi

    echo ""
    log_info "Starting setup wizard..."
    echo ""

    cd "$INSTALL_DIR"

    # Run marlow setup using the venv Python directly (no activation needed).
    # Redirect stdin from /dev/tty so interactive prompts work when piped from curl.
    if [ "$USE_VENV" = true ]; then
        "$INSTALL_DIR/venv/bin/python" -m marlow_cli.main setup < /dev/tty
    else
        python -m marlow_cli.main setup < /dev/tty
    fi
}

maybe_start_gateway() {
    # Check if any messaging platform tokens were configured
    ENV_FILE="$MARLOW_HOME/.env"
    if [ ! -f "$ENV_FILE" ]; then
        return 0
    fi

    HAS_MESSAGING=false
    for VAR in TELEGRAM_BOT_TOKEN DISCORD_BOT_TOKEN SLACK_BOT_TOKEN SLACK_APP_TOKEN FEISHU_APP_ID EMAIL_ADDRESS; do
        VAL=$(grep "^${VAR}=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        if [ -n "$VAL" ] && [ "$VAL" != "your-token-here" ]; then
            HAS_MESSAGING=true
            break
        fi
    done

    if [ "$HAS_MESSAGING" = false ]; then
        return 0
    fi

    echo ""
    log_info "Messaging platform token detected!"
    log_info "The gateway needs to be running for Marlow to send/receive messages."

    # Probe by actually opening /dev/tty: a bare existence test passes
    # in Docker builds where the device node is in the mount namespace
    # but opening fails with ENXIO. See #16746.
    if ! (: </dev/tty) 2>/dev/null; then
        log_info "Gateway setup skipped (no terminal available). Run 'marlow gateway install' later."
        return 0
    fi

    echo ""
    local should_install_gateway=false
    if prompt_yes_no "Would you like to install the gateway as a background service?" "yes"; then
        should_install_gateway=true
    fi

    if [ "$should_install_gateway" = true ]; then
        MARLOW_CMD="$(get_marlow_command_path)"

        if command -v systemctl &> /dev/null; then
            log_info "Installing systemd service..."
            if $MARLOW_CMD gateway install 2>/dev/null; then
                log_success "Gateway service installed"
                if $MARLOW_CMD gateway start 2>/dev/null; then
                    log_success "Gateway started! Your bot is now online."
                else
                    log_warn "Service installed but failed to start. Try: marlow gateway start"
                fi
            else
                log_warn "Systemd install failed. You can start manually: marlow gateway"
            fi
        else
            log_info "systemd not available — starting gateway in background..."
            nohup $MARLOW_CMD gateway > "$MARLOW_HOME/logs/gateway.log" 2>&1 &
            GATEWAY_PID=$!
            log_success "Gateway started (PID $GATEWAY_PID). Logs: ~/.marlow/logs/gateway.log"
            log_info "To stop: kill $GATEWAY_PID"
            log_info "To restart later: marlow gateway"
        fi
    else
        log_info "Skipped. Start the gateway later with: marlow gateway"
    fi
}

print_success() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    echo "┌─────────────────────────────────────────────────────────┐"
    echo "│              ✓ Installation Complete!                   │"
    echo "└─────────────────────────────────────────────────────────┘"
    echo -e "${NC}"
    echo ""

    # Show file locations
    echo -e "${CYAN}${BOLD}📁 Your files:${NC}"
    echo ""
    echo -e "   ${YELLOW}Config:${NC}    $MARLOW_HOME/config.yaml"
    echo -e "   ${YELLOW}API Keys:${NC}  $MARLOW_HOME/.env"
    echo -e "   ${YELLOW}Data:${NC}      $MARLOW_HOME/cron/, sessions/, logs/"
    echo -e "   ${YELLOW}Code:${NC}      $INSTALL_DIR"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    echo -e "${CYAN}${BOLD}🚀 Commands:${NC}"
    echo ""
    echo -e "   ${GREEN}marlow${NC}              Start chatting"
    echo -e "   ${GREEN}marlow setup${NC}        Configure API keys & settings"
    echo -e "   ${GREEN}marlow config${NC}       View/edit configuration"
    echo -e "   ${GREEN}marlow config edit${NC}  Open config in editor"
    echo -e "   ${GREEN}marlow gateway install${NC} Install gateway service (messaging + cron)"
    echo -e "   ${GREEN}marlow update${NC}       Update to latest version"
    echo ""

    echo -e "${CYAN}─────────────────────────────────────────────────────────${NC}"
    echo ""
    if [ "$ROOT_FHS_LAYOUT" = true ]; then
        echo -e "${YELLOW}⚡ 'marlow' was linked into /usr/local/bin and is ready to use — no shell reload needed.${NC}"
        echo ""
    else
        echo -e "${YELLOW}⚡ Reload your shell to use 'marlow' command:${NC}"
        echo ""
        LOGIN_SHELL="$(basename "${SHELL:-/bin/bash}")"
        if [ "$LOGIN_SHELL" = "zsh" ]; then
            echo "   source ~/.zshrc"
        elif [ "$LOGIN_SHELL" = "bash" ]; then
            echo "   source ~/.bashrc"
        elif [ "$LOGIN_SHELL" = "fish" ]; then
            echo "   source ~/.config/fish/config.fish"
        else
            echo "   source ~/.bashrc   # or ~/.zshrc"
        fi
        echo ""
    fi

    # Show Node.js warning if auto-install failed
    if [ "$HAS_NODE" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: Node.js could not be installed automatically."
        echo "Browser tools need Node.js. Install manually:"
        echo "  https://nodejs.org/en/download/"
        echo -e "${NC}"
    fi

    # Show ripgrep note if not installed
    if [ "$HAS_RIPGREP" = false ]; then
        echo -e "${YELLOW}"
        echo "Note: ripgrep (rg) was not found. File search will use"
        echo "grep as a fallback. For faster search in large codebases,"
        echo "install ripgrep: sudo apt install ripgrep (or brew install ripgrep)"
        echo -e "${NC}"
    fi
}

ensure_browser() {
    if ! command -v node >/dev/null 2>&1; then
        local node_bin="$MARLOW_HOME/node/bin/node"
        if [ -x "$node_bin" ]; then
            export PATH="$MARLOW_HOME/node/bin:$PATH"
        else
            log_error "Node.js not found. Run with --ensure node first."
            return 1
        fi
    fi

    local npm_bin
    npm_bin="$(command -v npm 2>/dev/null || echo "$MARLOW_HOME/node/bin/npm")"
    if [ ! -x "$npm_bin" ]; then
        log_error "npm not found"
        return 1
    fi

    log_info "Installing agent-browser..."
    local log_file
    log_file="$(mktemp)"
    if ! "$npm_bin" install -g --prefix "$MARLOW_HOME/node" --silent --ignore-scripts \
        "agent-browser@^0.26.0" \
        >"$log_file" 2>&1; then
        log_error "npm install failed:"
        cat "$log_file" >&2
        rm -f "$log_file"
        return 1
    fi
    rm -f "$log_file"
    export PATH="$MARLOW_HOME/node/bin:$PATH"

    local sys_browser
    sys_browser="$(find_system_browser 2>/dev/null || true)"
    if [ -n "$sys_browser" ]; then
        configure_browser_env_from_system_browser "$sys_browser"
        log_info "System browser detected -- skipping Chromium download"
        return 0
    fi

    log_info "Installing Chromium via agent-browser install..."
    local ab_bin="$MARLOW_HOME/node/bin/agent-browser"
    if [ -x "$ab_bin" ]; then
        "$ab_bin" install 2>/dev/null || {
            log_warn "Chromium install failed. Browser tools may not work without a system browser."

            # OS-specific hints (detect_os sets $DISTRO)
            case "${DISTRO:-unknown}" in
                ubuntu|debian)
                    log_info "Try: sudo apt-get install -y chromium-browser"
                    ;;
                arch)
                    log_info "Try: sudo pacman -S chromium"
                    ;;
                fedora|rhel|centos)
                    log_info "Try: sudo dnf install -y chromium"
                    ;;
            esac
        }
    else
        log_warn "agent-browser not found at $ab_bin"
    fi

    return 0
}

ensure_mode() {
    detect_os

    IFS=',' read -ra DEPS <<< "$ENSURE_DEPS"
    for dep in "${DEPS[@]}"; do
        dep="$(echo "$dep" | tr -d '[:space:]')"
        case "$dep" in
            node)
                check_node
                ;;
            browser)
                check_node
                if [ "$HAS_NODE" = true ]; then
                    ensure_browser
                fi
                ;;
            ripgrep)
                if ! command -v rg &>/dev/null; then
                    HAS_RIPGREP=false
                    install_system_packages
                fi
                ;;
            *)
                log_warn "Unknown dependency: $dep"
                ;;
        esac
    done
}

postinstall_mode() {
    print_banner
    detect_os

    log_info "Post-install mode: setting up Marlow for pip install"

    check_node
    check_network_prerequisites
    install_system_packages

    if [ "$HAS_NODE" = true ] && [ "$SKIP_BROWSER" = false ]; then
        ensure_browser
    fi

    MARLOW_CMD="$(command -v marlow 2>/dev/null || echo "")"
    if [ -n "$MARLOW_CMD" ]; then
        log_info "Running marlow setup..."
        "$MARLOW_CMD" setup
    else
        log_warn "marlow command not found on PATH"
        log_info "Try: python -m marlow_cli.main setup"
    fi
}

# Each --stage runs in its own process, so (unlike the monolithic main() where
# clone_repo cd's once and later steps inherit it) a stage that operates on the
# checkout must cd into it explicitly. Without this, install_deps/setup_path run
# from the desktop app's cwd and resolve `.` / the venv against the wrong tree.
require_install_dir() {
    if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
        log_error "Install directory not found: ${INSTALL_DIR:-<unset>}"
        log_info "The 'repository' stage must run before this one."
        return 1
    fi
    cd "$INSTALL_DIR"
}

# Desktop bootstrap stage protocol for the Electron bootstrap runner.
run_stage_body() {
    local stage="$1"

    case "$stage" in
        prerequisites)
            print_banner
            detect_os
            resolve_install_layout
            install_uv
            check_python
            check_git
            check_node
            check_network_prerequisites
            install_system_packages
            ;;
        repository)
            detect_os
            resolve_install_layout
            check_git
            clone_repo
            ;;
        venv)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            setup_venv
            ;;
        python-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            install_uv
            check_python
            install_deps
            ;;
        node-deps)
            detect_os
            resolve_install_layout
            require_install_dir
            check_node
            install_node_deps
            ;;
        path)
            detect_os
            resolve_install_layout
            require_install_dir
            setup_path
            ;;
        config)
            detect_os
            resolve_install_layout
            require_install_dir
            copy_config_templates
            ;;
        setup)
            detect_os
            resolve_install_layout
            require_install_dir
            run_setup_wizard
            ;;
        gateway)
            detect_os
            resolve_install_layout
            require_install_dir
            maybe_start_gateway
            ;;
        complete)
            detect_os
            resolve_install_layout
            print_success
            echo "git" > "$MARLOW_HOME/.install_method"
            ;;
        *)
            log_error "Unknown stage: $stage"
            return 2
            ;;
    esac
}

run_stage_protocol() {
    local stage="$1"
    if [ -z "$stage" ]; then
        log_error "--stage requires a stage name"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "" false false "missing stage name"
        fi
        return 2
    fi

    if [ "$NON_INTERACTIVE" = true ] && stage_needs_user_input "$stage"; then
        log_info "Skipping $stage (non-interactive bootstrap)"
        if [ "$JSON_OUTPUT" = true ]; then
            emit_stage_json "$stage" true true
        fi
        return 0
    fi

    # Run the stage body in a subshell so a stage helper that calls `exit 1`
    # on failure (clone_repo, install_deps, etc. were written for the monolithic
    # flow) only exits the subshell — the parent still reaches the JSON result
    # frame below. Without this, a failed --stage would terminate the process
    # before emitting the frame and the Rust/Electron parser would see "no
    # result frame" instead of a clean {ok:false} contract response.
    set +e
    ( run_stage_body "$stage" )
    local code=$?
    set -e

    if [ "$JSON_OUTPUT" = true ]; then
        if [ "$code" -eq 0 ]; then
            emit_stage_json "$stage" true false
        else
            emit_stage_json "$stage" false false "exit code $code"
        fi
    fi
    return "$code"
}

# ============================================================================
# Main
# ============================================================================

main() {
    print_banner

    detect_os
    resolve_install_layout
    install_uv
    check_python
    check_git
    check_node
    check_network_prerequisites
    install_system_packages

    clone_repo
    setup_venv
    install_deps
    install_node_deps
    setup_path
    copy_config_templates
    run_setup_wizard
    maybe_start_gateway

    print_success

    echo "git" > "$MARLOW_HOME/.install_method"
}

if [ "$MANIFEST_MODE" = true ]; then
    emit_manifest
elif [ -n "$STAGE_NAME" ]; then
    run_stage_protocol "$STAGE_NAME"
elif [ -n "$ENSURE_DEPS" ]; then
    ensure_mode
elif [ "$POSTINSTALL_MODE" = true ]; then
    postinstall_mode
else
    main
fi
