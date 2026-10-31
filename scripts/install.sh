#!/bin/sh
# Installs a downloaded disk-steward-<version>.pyz for the current user, or removes it again. Never uses sudo.
#
#   sh install.sh ~/Downloads/disk-steward-0.4.0.pyz    install, or replace an earlier install
#   sh install.sh --uninstall                           remove the wrapper and the .pyz; your data stays
#
# It writes two files and nothing else:
#   ~/.local/share/disk-steward/disk-steward.pyz
#   ~/.local/bin/disk-steward    a wrapper that runs the .pyz with python3
set -eu

[ -n "${HOME:-}" ] || { echo "install.sh: HOME is not set" >&2; exit 1; }

share_dir="$HOME/.local/share/disk-steward"
pyz="$share_dir/disk-steward.pyz"
bin_dir="$HOME/.local/bin"
wrapper="$bin_dir/disk-steward"
marker='# disk-steward wrapper written by install.sh'
# the weekly scan (`disk-steward schedule install`); the label is schedule.LABEL in the package
schedule_plist="$HOME/Library/LaunchAgents/io.github.disk-steward.scan.plist"
data_dir="$HOME/Library/Application Support/Disk Steward"

die() {
  echo "install.sh: $*" >&2
  exit 1
}

usage() {
  echo "Usage: sh install.sh PATH/TO/disk-steward-VERSION.pyz"
  echo "       sh install.sh --uninstall"
}

# A symlink or a file of someone else's is never ours.
is_our_wrapper() {
  [ -f "$wrapper" ] && [ ! -L "$wrapper" ] && grep -qF "$marker" "$wrapper"
}

install_pyz() {
  src=$1
  [ -f "$src" ] || die "$src is not a file"
  if ! command -v python3 >/dev/null 2>&1 ||
    ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
    die "Python 3.9 or newer is required as python3. Run 'xcode-select --install' or install Python from python.org, then run this again."
  fi
  version=$(python3 "$src" --version 2>&1) || version=
  case $version in
    "disk-steward "*) ;;
    *) die "$src does not run as Disk Steward. A failed download can save an error page under that name: download it again from the release page." ;;
  esac

  if [ -e "$wrapper" ] || [ -L "$wrapper" ]; then
    is_our_wrapper || die "$wrapper exists and this installer did not write it (pip and pipx put a file there too). Move or remove it, then run this again."
  fi
  [ ! -d "$pyz" ] || die "$pyz is a folder"

  if [ -f "$pyz" ]; then verb=Updated; else verb=Installed; fi
  mkdir -p "$share_dir" "$bin_dir"
  new_pyz="$pyz.new.$$"
  new_wrapper="$wrapper.new.$$"
  trap 'rm -f "$new_pyz" "$new_wrapper"' EXIT
  cp "$src" "$new_pyz"
  chmod 755 "$new_pyz"
  {
    printf '%s\n' '#!/bin/sh' "$marker"
    printf '%s\n' 'exec /usr/bin/env python3 "$HOME/.local/share/disk-steward/disk-steward.pyz" "$@"'
  } >"$new_wrapper"
  chmod 755 "$new_wrapper"
  mv -f "$new_pyz" "$pyz"
  mv -f "$new_wrapper" "$wrapper"

  installed=$("$wrapper" --version)
  echo "$verb $installed"
  echo "  $pyz"
  echo "  $wrapper"
  case ":${PATH:-}:" in
    *":$bin_dir:"*) echo "Run: disk-steward portal --open" ;;
    *)
      echo
      echo "$bin_dir is not on your PATH, so Terminal cannot find disk-steward yet."
      echo "For zsh, the macOS default shell, add it with:"
      echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc"
      echo "then open a new Terminal window. Until then, run:"
      echo "  $wrapper portal --open"
      ;;
  esac
  echo "Remove it again with: sh install.sh --uninstall"
}

uninstall() {
  if [ -f "$schedule_plist" ] && grep -qF '/disk-steward/disk-steward.pyz' "$schedule_plist"; then
    die "The weekly scan is installed and runs the .pyz this would remove. Run 'disk-steward schedule remove' first, then run this again."
  fi
  removed=0
  if [ -e "$wrapper" ] || [ -L "$wrapper" ]; then
    if is_our_wrapper; then
      rm -f "$wrapper"
      echo "Removed $wrapper"
      removed=1
    else
      echo "Left $wrapper alone: this installer did not write it."
    fi
  fi
  if [ -f "$pyz" ] || [ -L "$pyz" ]; then
    rm -f "$pyz"
    echo "Removed $pyz"
    removed=1
  fi
  rmdir "$share_dir" 2>/dev/null || true
  [ "$removed" = 1 ] || echo "Nothing to remove: no Disk Steward install found under ~/.local."
  if [ -d "$data_dir" ]; then
    echo "Your data stays in \"$data_dir\" (scan history, audit log, archives)."
    echo "To delete it as well, restore any archives you want first, then: rm -r \"$data_dir\""
  fi
}

case ${1:-} in
  --uninstall)
    [ $# -eq 1 ] || { usage >&2; exit 2; }
    uninstall
    ;;
  -h | --help)
    usage
    ;;
  -* | "")
    usage >&2
    exit 2
    ;;
  *)
    [ $# -eq 1 ] || { usage >&2; exit 2; }
    install_pyz "$1"
    ;;
esac
