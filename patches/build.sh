#!/usr/bin/env bash
#
# Build a patched SuperTuxKart with two-way replay sync (see PROTOCOL.md),
# from a pristine 1.5 checkout plus the four patches in this directory.
#
# Usage:
#   ./build.sh                  # clone + patch + build into the default dir
#   ./build.sh --dir PATH       # use PATH instead
#   ./build.sh --jobs N         # parallel build jobs (default: nproc)
#
# Safe to re-run: an existing checkout is reused rather than re-cloned, the
# patches are only applied once, and the build itself is incremental.
#
# stk_minimap.py's GUI looks for the result at the same default location
# this script uses (see default_patched_stk_binary() in stk_minimap.py), so
# unless you pass --dir, building here is all that's needed for the "Launch
# SuperTuxKart" button to find it on its own.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
STK_TAG="1.5"
STK_REPO="https://github.com/supertuxkart/stk-code.git"
JOBS=""
DIR=""

info() { printf '%s\n' "$*"; }
err()  { printf 'error: %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)  DIR=$2; shift 2 ;;
        --dir=*) DIR=${1#--dir=}; shift ;;
        --jobs) JOBS=$2; shift 2 ;;
        --jobs=*) JOBS=${1#--jobs=}; shift ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "unknown argument '$1' (see --help)" ;;
    esac
done

if [ -z "$DIR" ]; then
    case "$(uname -s)" in
        Darwin) base="$HOME/Library/Application Support" ;;
        *)      base="${XDG_DATA_HOME:-$HOME/.local/share}" ;;
    esac
    DIR="$base/stk-minimap/stk-code"
fi
[ -n "$JOBS" ] || JOBS=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)

info "Target checkout : $DIR"
info "Build jobs      : $JOBS"
echo

# ---------------------------------------------------------------- tools ---
need() {
    command -v "$1" >/dev/null 2>&1 || MISSING="$MISSING $1"
}
MISSING=""
need git
need cmake
CXX_OK=0
for c in g++ clang++ c++; do command -v "$c" >/dev/null 2>&1 && CXX_OK=1; done
[ "$CXX_OK" = 1 ] || MISSING="$MISSING a-c++-compiler"
GENERATOR="Unix Makefiles"
BUILD_TOOL="make"
if command -v ninja >/dev/null 2>&1; then
    GENERATOR="Ninja"
    BUILD_TOOL="ninja"
elif ! command -v make >/dev/null 2>&1; then
    MISSING="$MISSING ninja-or-make"
fi

if [ -n "$MISSING" ]; then
    err "missing build tools:$MISSING"
    cat >&2 <<'EOF'

  Arch/Manjaro : sudo pacman -S git cmake ninja gcc
  Debian/Ubuntu: sudo apt install git cmake ninja-build g++
  Fedora       : sudo dnf install git cmake ninja-build gcc-c++
  macOS        : brew install git cmake ninja
EOF
    exit 1
fi
info "Build tools OK (generator: $GENERATOR)"

# STK's own build dependencies (SDL2, OpenAL, etc.) are not checked here -
# if cmake fails below, its own error names the missing library; STK's own
# README documents this list per distro.

# ------------------------------------------------------------ checkout ---
PATCHED_MARKER="src/replay/replay_sync_server.cpp"

if [ -d "$DIR/.git" ]; then
    info "Reusing existing checkout at $DIR"
    if [ -f "$DIR/$PATCHED_MARKER" ]; then
        info "Patches already applied - skipping."
    else
        if [ -n "$(git -C "$DIR" status --porcelain)" ]; then
            die "$DIR has uncommitted changes and isn't patched - not " \
                "touching it. Move it aside or pass --dir to use a fresh one."
        fi
        info "Applying patches..."
        for p in "$SCRIPT_DIR"/000*.patch; do
            info "  $(basename "$p")"
            git -C "$DIR" apply "$p" || die "failed to apply $(basename "$p") " \
                "- the checkout may not be a clean 1.5 (try --dir with a new path)"
        done
    fi
else
    [ -e "$DIR" ] && die "$DIR exists and isn't a git checkout - remove it " \
        "or pass --dir"
    info "Cloning stk-code $STK_TAG..."
    mkdir -p "$(dirname "$DIR")"
    git clone --depth 1 --branch "$STK_TAG" "$STK_REPO" "$DIR"
    info "Applying patches..."
    for p in "$SCRIPT_DIR"/000*.patch; do
        info "  $(basename "$p")"
        git -C "$DIR" apply "$p" || die "failed to apply $(basename "$p") " \
            "against a fresh clone - please report this, stk-code's 1.5 " \
            "tag may have moved"
    done
fi
echo

# --------------------------------------------------------------- assets --
# The patched build needs the same tracks/karts/textures/etc. as a normal
# install. Reusing an existing SuperTuxKart install avoids a second ~1GB
# download (the separate stk-assets checkout) - symlink its data
# subdirectories in, the same way this project's own docs describe doing
# by hand.
ASSET_SUBDIRS="library models music sfx textures tracks karts"
find_existing_data_dir() {
    for c in \
        /usr/share/supertuxkart/data \
        /usr/local/share/supertuxkart/data \
        /usr/local/share/games/supertuxkart/data \
        /var/lib/flatpak/app/net.supertuxkart.SuperTuxKart/current/active/files/share/supertuxkart/data \
        "$HOME/.local/share/flatpak/app/net.supertuxkart.SuperTuxKart/current/active/files/share/supertuxkart/data" \
        "$HOME/snap/supertuxkart/current/usr/share/supertuxkart/data" \
        "/Applications/SuperTuxKart.app/Contents/Resources/data"
    do
        [ -d "$c" ] && { printf '%s\n' "$c"; return 0; }
    done
    return 1
}

if [ -e "$DIR/data/tracks" ]; then
    info "Assets already linked in $DIR/data"
elif SRC_DATA=$(find_existing_data_dir); then
    info "Linking assets from $SRC_DATA"
    mkdir -p "$DIR/data"
    for d in $ASSET_SUBDIRS; do
        [ -e "$DIR/data/$d" ] && continue
        [ -e "$SRC_DATA/$d" ] && ln -s "$SRC_DATA/$d" "$DIR/data/$d"
    done
else
    info "No existing SuperTuxKart install found to reuse assets from."
    info "Install SuperTuxKart normally first (recommended - a speedrunning"
    info "setup needs it anyway), then re-run this script; or clone"
    info "https://github.com/supertuxkart/stk-assets into $DIR/data yourself"
    info "(a much larger download). Continuing to build the binary - it"
    info "just won't be able to run without one of the above."
fi
echo

# ---------------------------------------------------------------- build --
info "Configuring..."
# -DBUILD_RECORDER=0: the in-game video recorder needs libopenglrecorder,
# which most distros don't package and which this project has no use for -
# a plain tagged checkout (unlike a git-describe "git" build) treats a
# missing one as a hard error rather than just disabling the feature.
cmake -S "$DIR" -B "$DIR/build" -G "$GENERATOR" -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_RECORDER=0 \
    >/dev/null || die "cmake configure failed (see output above)"

info "Building with $JOBS job(s) - this takes a few minutes..."
cmake --build "$DIR/build" -j "$JOBS" || die "build failed (see output above)"

BIN="$DIR/build/bin/supertuxkart"
[ -x "$BIN" ] || die "build finished but $BIN wasn't produced - something's wrong"

echo
info "Done: $BIN"
info ""
info "Run it with:"
info "  cd '$DIR' && ./build/bin/supertuxkart --sync-port=27982"
info ""
info "stk_minimap.py's GUI will find this binary on its own (same default"
info "location) - open the Replay tab and use the Launch SuperTuxKart button."
