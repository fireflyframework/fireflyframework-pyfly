#!/usr/bin/env bash
# Copyright 2026 Firefly Software Foundation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# PyFly Framework Installer
# Usage:
#   Interactive:     bash install.sh
#   Via curl:        curl -fsSL https://raw.githubusercontent.com/fireflyframework/fireflyframework-pyfly/main/install.sh | bash
#   Via get.pyfly:   curl -fsSL https://get.pyfly.io/ | bash
#   Uninstall:       bash install.sh --uninstall
#   Custom dir:      PYFLY_HOME=/opt/pyfly bash install.sh
#   Custom extras:   PYFLY_EXTRAS=web,data bash install.sh

set -euo pipefail

# ── Constants ──────────────────────────────────────────────────────────────────

PYFLY_VERSION="26.05.03"
PYFLY_REPO="https://github.com/fireflyframework/fireflyframework-pyfly.git"
DEFAULT_INSTALL_DIR="$HOME/.pyfly"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=12

# ── Color support ──────────────────────────────────────────────────────────────

# Enable colors when output is a terminal
if [ -t 1 ]; then
    BOLD="\033[1m"
    DIM="\033[2m"
    RED="\033[31m"
    GREEN="\033[32m"
    YELLOW="\033[33m"
    CYAN="\033[36m"
    MAGENTA="\033[35m"
    RESET="\033[0m"
else
    BOLD="" DIM="" RED="" GREEN="" YELLOW="" CYAN="" MAGENTA="" RESET=""
fi

# Interactive detection: works both for direct execution AND curl-piped mode.
# When piped via curl, stdin is the script — but /dev/tty is the user's terminal.
if [ -t 0 ]; then
    IS_INTERACTIVE=true
    TTY_IN="/dev/stdin"
elif [ -t 1 ] && [ -e /dev/tty ]; then
    # curl | bash mode — stdout is a terminal, stdin is the pipe, but /dev/tty works
    IS_INTERACTIVE=true
    TTY_IN="/dev/tty"
else
    IS_INTERACTIVE=false
    TTY_IN="/dev/null"
fi

# ── Helper functions ───────────────────────────────────────────────────────────

info()    { printf "${CYAN}[INFO]${RESET}  %s\n" "$1"; }
success() { printf "${GREEN}[OK]${RESET}    %s\n" "$1"; }
warn()    { printf "${YELLOW}[WARN]${RESET}  %s\n" "$1"; }
error()   { printf "${RED}[ERROR]${RESET} %s\n" "$1" >&2; }
fatal()   { error "$1"; exit 1; }

# Read a line from the user, even when piped via curl.
# Returns empty string on EOF (Ctrl+D) instead of failing.
# Discards any buffered input (e.g. arrow keys pressed before the prompt).
prompt_read() {
    # Flush ALL stale input from the terminal (arrow keys, escape sequences)
    if [ "$TTY_IN" = "/dev/tty" ]; then
        while read -r -t 0.1 _discard < /dev/tty 2>/dev/null; do :; done
    fi
    read -r "$@" < "$TTY_IN" || true
}

banner() {
    printf "${MAGENTA}"
    cat << 'BANNER'
                _____.__
______ ___.__._/ ____\  | ___.__.
\____ <   |  |\   __\|  |<   |  |
|  |_> >___  | |  |  |  |_\___  |
|   __// ____| |__|  |____/ ____|
|__|   \/                 \/
BANNER
    printf "${RESET}\n"
    printf "  ${DIM}:: PyFly Framework Installer :: (v%s)${RESET}\n" "$PYFLY_VERSION"
    printf "  ${DIM}Copyright 2026 Firefly Software Foundation. | Apache 2.0 License${RESET}\n\n"
}

# Ensure the cli extra is always included in the extras list.
# The pyfly command requires click, rich, and jinja2 from the cli extra.
ensure_cli_extra() {
    local extras="$1"
    if [ "$extras" = "full" ]; then
        echo "full"
        return
    fi
    # Check if cli is already in the extras list
    if echo ",$extras," | grep -q ",cli,"; then
        echo "$extras"
    else
        echo "${extras},cli"
    fi
}

# ── Uninstall ─────────────────────────────────────────────────────────────────

uninstall_pyfly() {
    banner

    local install_dir="${PYFLY_HOME:-$DEFAULT_INSTALL_DIR}"
    install_dir="${install_dir/#\~/$HOME}"

    if [ ! -d "$install_dir" ]; then
        info "PyFly is not installed at $install_dir"
        exit 0
    fi

    info "Found PyFly installation at: $install_dir"

    if [ "$IS_INTERACTIVE" = true ]; then
        printf "\n${BOLD}Remove PyFly installation at %s?${RESET} ${DIM}[y/N]${RESET}: " "$install_dir"
        prompt_read confirm
        case "${confirm:-N}" in
            [Yy]*) ;;
            *) info "Uninstall cancelled."; exit 0 ;;
        esac
    fi

    info "Removing $install_dir ..."
    rm -rf "$install_dir"
    success "PyFly installation removed"

    # Clean up PATH entries from shell profiles
    local cleaned=false
    for profile in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile" "$HOME/.config/fish/config.fish"; do
        if [ -f "$profile" ] && grep -q "pyfly" "$profile" 2>/dev/null; then
            local tmp
            tmp=$(mktemp) || { warn "Failed to create temp file; skipping $profile"; continue; }
            grep -v "# PyFly Framework" "$profile" | grep -Fv "$install_dir/bin" > "$tmp" || true
            mv "$tmp" "$profile"
            cleaned=true
            success "Cleaned PATH entry from $profile"
        fi
    done

    if [ "$cleaned" = false ]; then
        info "No PATH entries to clean up"
    fi

    printf "\n${GREEN}${BOLD}PyFly has been uninstalled.${RESET}\n\n"
    exit 0
}

# ── Prerequisite checks ───────────────────────────────────────────────────────

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local version
            version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
            local major minor
            major=$(echo "$version" | cut -d. -f1)
            minor=$(echo "$version" | cut -d. -f2)
            # Proper version comparison: major > MIN or (major == MIN and minor >= MIN_MINOR)
            if [ "$major" -gt "$MIN_PYTHON_MAJOR" ] 2>/dev/null; then
                PYTHON_CMD="$cmd"
                PYTHON_VERSION="$version"
                return 0
            elif [ "$major" -eq "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ] 2>/dev/null; then
                PYTHON_CMD="$cmd"
                PYTHON_VERSION="$version"
                return 0
            fi
        fi
    done
    return 1
}

check_prerequisites() {
    info "Checking prerequisites..."

    if find_python; then
        success "Python $PYTHON_VERSION found ($PYTHON_CMD)"
    else
        fatal "Python >= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} is required. Please install it first."
    fi

    if "$PYTHON_CMD" -c "import venv" &>/dev/null; then
        success "venv module available"
    else
        fatal "Python venv module is required. Install it with: sudo apt install python3-venv (Debian/Ubuntu)"
    fi

    if "$PYTHON_CMD" -c "import ensurepip" &>/dev/null; then
        success "ensurepip available (pip will be installed in venv)"
    else
        fatal "ensurepip is required. Install it with: sudo apt install python3-venv (Debian/Ubuntu) or brew install python (macOS)"
    fi

    if command -v uv &>/dev/null; then
        HAS_UV=true
        success "uv available (will use for faster installs)"
    else
        HAS_UV=false
        info "uv not found — falling back to pip (install uv for faster installs: https://docs.astral.sh/uv/)"
    fi

    if command -v git &>/dev/null; then
        success "git available"
    else
        warn "git not found. Some features may be limited."
    fi
}

# ── Interactive prompts ────────────────────────────────────────────────────────

prompt_install_dir() {
    if [ "$IS_INTERACTIVE" = true ] && [ -z "${PYFLY_HOME:-}" ]; then
        printf "\n${BOLD}Installation directory${RESET} ${DIM}[%s]${RESET}: " "$DEFAULT_INSTALL_DIR"
        prompt_read user_dir
        # Strip control characters (ESC, etc.) AND leftover ANSI sequences ([A, [B, [1;5C, ...)
        user_dir=$(printf '%s' "${user_dir:-}" | tr -d '[:cntrl:]' | sed 's/\[[0-9;]*[A-Za-z]//g; s/^[[:space:]]*//; s/[[:space:]]*$//')
        # Validate: must be empty (→ default) or a real path (starts with / or ~)
        if [ -n "$user_dir" ]; then
            case "$user_dir" in
                /*|~*)  ;;  # Valid absolute or home-relative path
                *)
                    warn "Invalid input detected — using default directory"
                    user_dir=""
                    ;;
            esac
        fi
        INSTALL_DIR="${user_dir:-$DEFAULT_INSTALL_DIR}"
    else
        INSTALL_DIR="${PYFLY_HOME:-$DEFAULT_INSTALL_DIR}"
    fi
    INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

    # Final validation: must be a non-empty absolute-ish path
    if [ -z "$INSTALL_DIR" ] || [ "$INSTALL_DIR" = "/" ]; then
        INSTALL_DIR="$DEFAULT_INSTALL_DIR"
        warn "Invalid directory — falling back to $INSTALL_DIR"
    fi
    info "Install directory: $INSTALL_DIR"
}

prompt_extras() {
    if [ "$IS_INTERACTIVE" = true ] && [ -z "${PYFLY_EXTRAS:-}" ]; then
        printf "\n${BOLD}Available extras:${RESET}\n"
        printf "  ${CYAN} 1${RESET}) full             — All modules incl. Granian + FastAPI (recommended)\n"
        printf "  ${CYAN} 2${RESET}) web              — Web framework (Starlette + Uvicorn; see web-fast for Granian)\n"
        printf "  ${CYAN} 3${RESET}) web-fast         — High-performance web (Starlette + Granian + uvloop)\n"
        printf "  ${CYAN} 4${RESET}) web-fastapi      — FastAPI web framework (FastAPI + Granian + uvloop)\n"
        printf "  ${CYAN} 5${RESET}) fastapi          — FastAPI adapter only\n"
        printf "  ${CYAN} 6${RESET}) data-relational  — SQL databases (SQLAlchemy, Alembic, SQLite default)\n"
        printf "  ${CYAN} 7${RESET}) data-document    — Document databases (MongoDB, Beanie ODM)\n"
        printf "  ${CYAN} 8${RESET}) eda              — Event-Driven Architecture (Kafka, RabbitMQ, in-memory)\n"
        printf "  ${CYAN} 9${RESET}) cache            — Caching (Redis, in-memory)\n"
        printf "  ${CYAN}10${RESET}) client           — HTTP client (HTTPX, circuit breaker, retry)\n"
        printf "  ${CYAN}11${RESET}) security         — Auth & JWT (PyJWT, bcrypt)\n"
        printf "  ${CYAN}12${RESET}) scheduling       — Cron jobs and scheduled tasks\n"
        printf "  ${CYAN}13${RESET}) observability    — Prometheus metrics, OpenTelemetry tracing\n"
        printf "  ${CYAN}14${RESET}) shell            — CLI commands (Click, interactive REPL)\n"
        printf "  ${CYAN}15${RESET}) granian          — Granian ASGI server (Rust/tokio, ~3x faster than Uvicorn)\n"
        printf "  ${CYAN}16${RESET}) hypercorn        — Hypercorn ASGI server (HTTP/2 and HTTP/3 support)\n"
        printf "  ${CYAN}17${RESET}) custom           — Enter comma-separated extras\n"
        printf "\n${BOLD}Select extras${RESET} ${DIM}[1]${RESET}: "
        prompt_read choice
        case "${choice:-1}" in
            1)  EXTRAS="full" ;;
            2)  EXTRAS="web" ;;
            3)  EXTRAS="web-fast" ;;
            4)  EXTRAS="web-fastapi" ;;
            5)  EXTRAS="fastapi" ;;
            6)  EXTRAS="data-relational" ;;
            7)  EXTRAS="data-document" ;;
            8)  EXTRAS="eda" ;;
            9)  EXTRAS="cache" ;;
            10) EXTRAS="client" ;;
            11) EXTRAS="security" ;;
            12) EXTRAS="scheduling" ;;
            13) EXTRAS="observability" ;;
            14) EXTRAS="shell" ;;
            15) EXTRAS="granian" ;;
            16) EXTRAS="hypercorn" ;;
            17)
                printf "${BOLD}Enter extras${RESET} ${DIM}(comma-separated, e.g. web,data-relational,cache,security)${RESET}: "
                prompt_read custom_extras
                EXTRAS="${custom_extras:-full}"
                ;;
            *) EXTRAS="full" ;;
        esac
    else
        EXTRAS="${PYFLY_EXTRAS:-full}"
    fi

    # Always include the cli extra — required for the pyfly command to work
    EXTRAS=$(ensure_cli_extra "$EXTRAS")
    info "Selected extras: $EXTRAS"
}

prompt_add_to_path() {
    ADD_TO_PATH=false
    if [ "$IS_INTERACTIVE" = true ]; then
        printf "\n${BOLD}Add pyfly to PATH?${RESET} ${DIM}[Y/n]${RESET}: "
        prompt_read add_path
        case "${add_path:-Y}" in
            [Yy]*) ADD_TO_PATH=true ;;
            *) ADD_TO_PATH=false ;;
        esac
    else
        ADD_TO_PATH=true
    fi
}

# ── Installation ───────────────────────────────────────────────────────────────

detect_source_dir() {
    # If this script is in a pyfly source directory, use it
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
    if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && grep -q 'name = "pyfly"' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
        SOURCE_DIR="$SCRIPT_DIR"
        info "Source detected at: $SOURCE_DIR"
        return 0
    fi

    # If PYFLY_SOURCE is set, use it
    if [ -n "${PYFLY_SOURCE:-}" ] && [ -d "$PYFLY_SOURCE" ]; then
        SOURCE_DIR="$PYFLY_SOURCE"
        info "Source from PYFLY_SOURCE: $SOURCE_DIR"
        return 0
    fi

    # No local source — clone from GitHub (curl | bash mode)
    if ! command -v git &>/dev/null; then
        fatal "git is required to install PyFly via curl. Please install git first."
    fi
    info "Cloning PyFly from $PYFLY_REPO ..."
    SOURCE_DIR="$(mktemp -d)/pyfly"
    if ! git clone --depth 1 "$PYFLY_REPO" "$SOURCE_DIR" --quiet; then
        fatal "Failed to clone PyFly repository. Check your network connection and try again."
    fi
    success "Cloned PyFly source"
}

install_pyfly() {
    info "Creating installation directory: $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"

    # Copy source
    info "Copying PyFly source..."
    if [ "$SOURCE_DIR" != "$INSTALL_DIR/source" ]; then
        rm -rf "$INSTALL_DIR/source"
        cp -r "$SOURCE_DIR" "$INSTALL_DIR/source"
        rm -rf "$INSTALL_DIR/source/.worktrees" \
               "$INSTALL_DIR/source/.venv" \
               "$INSTALL_DIR/source/.pytest_cache" \
               "$INSTALL_DIR/source/.mypy_cache" \
               "$INSTALL_DIR/source/.ruff_cache" \
               "$INSTALL_DIR/source/htmlcov" \
               "$INSTALL_DIR/source/.coverage"
    fi
    success "Source copied"

    # Create virtual environment
    info "Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$INSTALL_DIR/venv"
    success "Virtual environment created"

    # Install PyFly — prefer uv if available
    info "Installing PyFly with extras: $EXTRAS ..."
    if [ "$HAS_UV" = true ]; then
        if ! uv pip install --python "$INSTALL_DIR/venv/bin/python" -e "$INSTALL_DIR/source[$EXTRAS]"; then
            fatal "uv pip install failed. Check the output above for details."
        fi
    else
        local pip_cmd="$INSTALL_DIR/venv/bin/pip"
        info "Upgrading pip..."
        "$pip_cmd" install --upgrade pip --quiet 2>&1 || warn "pip upgrade failed, continuing with existing version"
        if ! "$pip_cmd" install -e "$INSTALL_DIR/source[$EXTRAS]"; then
            fatal "pip install failed. Check the output above for details."
        fi
    fi
    success "PyFly installed successfully"

    # Verify that the pyfly entry point was created
    if [ ! -f "$INSTALL_DIR/venv/bin/pyfly" ]; then
        fatal "pyfly entry point not found after install. The cli extra may not have been installed correctly."
    fi

    # Create wrapper script
    info "Creating pyfly wrapper..."
    mkdir -p "$INSTALL_DIR/bin"
    cat > "$INSTALL_DIR/bin/pyfly" << WRAPPER
#!/usr/bin/env bash
# PyFly CLI wrapper — activates the venv and runs pyfly
exec "$INSTALL_DIR/venv/bin/pyfly" "\$@"
WRAPPER
    chmod +x "$INSTALL_DIR/bin/pyfly"
    success "Wrapper created at: $INSTALL_DIR/bin/pyfly"
}

configure_path() {
    if [ "$ADD_TO_PATH" = false ]; then
        return
    fi

    # Guard: INSTALL_DIR must be a non-empty, absolute path
    if [ -z "$INSTALL_DIR" ] || [ "$INSTALL_DIR" = "/" ]; then
        warn "Invalid INSTALL_DIR ('$INSTALL_DIR') — skipping PATH configuration"
        return
    fi

    local bin_dir="$INSTALL_DIR/bin"

    # Sanity check: bin_dir must point inside the install directory, not system /bin
    if [ "$bin_dir" = "/bin" ] || [ "$bin_dir" = "/usr/bin" ]; then
        warn "Refusing to add system path '$bin_dir' to shell profile"
        return
    fi

    local shell_profile=""
    local path_line=""
    case "${SHELL:-/bin/bash}" in
        */zsh)  shell_profile="$HOME/.zshrc" ;;
        */bash)
            if [ -f "$HOME/.bash_profile" ]; then
                shell_profile="$HOME/.bash_profile"
            else
                shell_profile="$HOME/.bashrc"
            fi
            ;;
        */fish)
            shell_profile="$HOME/.config/fish/config.fish"
            ;;
        *)  shell_profile="$HOME/.profile" ;;
    esac

    if echo "$PATH" | tr ':' '\n' | grep -Fq "$bin_dir"; then
        info "pyfly is already in PATH"
        return
    fi

    if [ -n "$shell_profile" ]; then
        if ! grep -Fq "$bin_dir" "$shell_profile" 2>/dev/null; then
            case "${SHELL:-/bin/bash}" in
                */fish) path_line="set -gx PATH $bin_dir \$PATH" ;;
                *)      path_line="export PATH=\"$bin_dir:\$PATH\"" ;;
            esac
            {
                echo ""
                echo "# PyFly Framework"
                echo "$path_line"
            } >> "$shell_profile"
            success "Added pyfly to PATH in $shell_profile"
            info "Run 'source $shell_profile' or open a new terminal to use pyfly"
        else
            info "pyfly PATH entry already exists in $shell_profile"
        fi
    fi
}

print_summary() {
    printf "\n"
    printf "${GREEN}${BOLD}Installation complete!${RESET}\n\n"
    printf "  ${BOLD}Installation:${RESET}  %s\n" "$INSTALL_DIR"
    printf "  ${BOLD}PyFly binary:${RESET}  %s/bin/pyfly\n" "$INSTALL_DIR"
    printf "  ${BOLD}Extras:${RESET}        %s\n" "$EXTRAS"
    printf "  ${BOLD}Python:${RESET}        %s\n" "$PYTHON_VERSION"
    printf "\n"
    printf "  ${BOLD}Get started:${RESET}\n"
    printf "    ${CYAN}pyfly --help${RESET}         Show available commands\n"
    printf "    ${CYAN}pyfly new my-app${RESET}     Create a new project\n"
    printf "    ${CYAN}pyfly doctor${RESET}         Check your environment\n"
    printf "    ${CYAN}pyfly info${RESET}           Show framework info\n"
    printf "\n"
    printf "  ${BOLD}Uninstall:${RESET}\n"
    printf "    ${CYAN}bash install.sh --uninstall${RESET}\n"
    printf "\n"
}

# ── Cleanup on failure ─────────────────────────────────────────────────────────

cleanup_on_failure() {
    if [ -n "${INSTALL_DIR:-}" ] && [ -d "$INSTALL_DIR" ]; then
        warn "Installation failed. Cleaning up $INSTALL_DIR..."
        rm -rf "$INSTALL_DIR"
    fi
}

trap cleanup_on_failure ERR

# ── Main ───────────────────────────────────────────────────────────────────────

main() {
    # Handle --uninstall flag
    if [ "${1:-}" = "--uninstall" ] || [ "${1:-}" = "uninstall" ]; then
        uninstall_pyfly
    fi

    banner
    check_prerequisites
    detect_source_dir
    prompt_install_dir
    prompt_extras
    prompt_add_to_path
    install_pyfly
    configure_path
    print_summary
}

main "$@"
