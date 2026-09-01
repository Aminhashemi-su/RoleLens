#!/usr/bin/env bash
set -euo pipefail

# Installs RoleLens into its home directory.
#
# Works from a checkout that carries real config/profile files and from a fresh
# clone that ships only *.example.json templates. A file that already exists in
# the home directory is never overwritten, so your edited configuration, profile
# and secrets survive every re-install. Pass --refresh-config to deliberately
# push config/profile from this checkout over what is already installed.

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROLELENS_DIR="${ROLELENS_HOME:-$HOME/.rolelens}"
SCRIPTS_DIR="${ROLELENS_SCRIPTS_HOME:-$HOME/.local}/scripts"

REFRESH=0
for arg in "$@"; do
  case "$arg" in
    --refresh-config) REFRESH=1 ;;
    -h|--help)
      cat <<'USAGE'
Usage: ./install.sh [--refresh-config]

  (no arguments)      Install. Missing config/profile files are created from
                      the shipped examples. Existing files are left untouched.
  --refresh-config    Also overwrite installed config/profile files from this
                      checkout. Never touches secrets.env.
USAGE
      exit 0 ;;
    *) echo "install.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

created=()
kept=()

# Best-effort restrictive permissions. Some filesystems (NTFS, FAT, certain
# network mounts) cannot chmod, and that must not abort an otherwise fine
# install.
harden() {
  chmod "$1" "$2" 2>/dev/null || echo "  note: could not set mode $1 on $2" >&2
}

# Secrets are the exception: if the mode cannot be set, say so loudly.
harden_secret() {
  if ! chmod 600 "$1" 2>/dev/null; then
    echo "WARNING: could not set mode 600 on $1" >&2
    echo "         Restrict it yourself before putting credentials in it." >&2
  fi
}

install_dir() {
  mkdir -p "$1"
  harden 700 "$1"
}

# install_file <destination> <primary source> [example source]
#
# Copies the primary source when present, otherwise the example. Skips a
# destination that already exists unless --refresh-config was given. Fails
# clearly when neither source exists, rather than creating empty configuration.
install_file() {
  local dest="$1" primary="$2" example="${3:-}" src=""
  local name="${dest##*/}"

  if [[ -e "$dest" && "$REFRESH" -eq 0 ]]; then
    kept+=("$name")
    return 0
  fi
  if [[ -f "$primary" ]]; then
    src="$primary"
  elif [[ -n "$example" && -f "$example" ]]; then
    src="$example"
  else
    echo "install.sh: cannot install $name" >&2
    echo "  looked for: $primary" >&2
    if [[ -n "$example" ]]; then echo "  and for:    $example" >&2; fi
    echo "  This checkout is incomplete. Re-clone or restore the missing file." >&2
    exit 1
  fi

  cp -- "$src" "$dest"
  harden 600 "$dest"
  if [[ "$src" == "$example" ]]; then
    created+=("$name  (from ${example##*/} - edit it)")
  elif [[ "$REFRESH" -eq 1 ]]; then
    created+=("$name  (refreshed from this checkout)")
  else
    created+=("$name")
  fi
}

install_dir "$ROLELENS_DIR"
install_dir "$ROLELENS_DIR/profile"
install_dir "$ROLELENS_DIR/data"
install_dir "$SCRIPTS_DIR"

install_file "$ROLELENS_DIR/config.json" \
             "$SOURCE_DIR/config.json" "$SOURCE_DIR/config.example.json"
install_file "$ROLELENS_DIR/profile/career_profile.json" \
             "$SOURCE_DIR/profile/career_profile.json" "$SOURCE_DIR/profile/career_profile.example.json"
install_file "$ROLELENS_DIR/profile/matcher_profile.json" \
             "$SOURCE_DIR/profile/matcher_profile.json" "$SOURCE_DIR/profile/matcher_profile.example.json"
install_file "$ROLELENS_DIR/profile/search_lenses.json" \
             "$SOURCE_DIR/profile/search_lenses.json" "$SOURCE_DIR/profile/search_lenses.example.json"
install_file "$ROLELENS_DIR/profile/matcher_rules_v1_1.json" \
             "$SOURCE_DIR/profile/matcher_rules_v1_1.json"

# The application itself is code, not configuration, so it is always refreshed.
if [[ ! -f "$SOURCE_DIR/rolelens.py" ]]; then
  echo "install.sh: $SOURCE_DIR/rolelens.py is missing. Re-clone the repository." >&2
  exit 1
fi
cp -- "$SOURCE_DIR/rolelens.py" "$SCRIPTS_DIR/rolelens.py"
harden 700 "$SCRIPTS_DIR/rolelens.py"

# Secrets: created from the example once, then never touched again.
secrets_created=0
if [[ ! -e "$ROLELENS_DIR/secrets.env" ]]; then
  if [[ ! -f "$SOURCE_DIR/secrets.env.example" ]]; then
    echo "install.sh: $SOURCE_DIR/secrets.env.example is missing. Re-clone the repository." >&2
    exit 1
  fi
  cp -- "$SOURCE_DIR/secrets.env.example" "$ROLELENS_DIR/secrets.env"
  secrets_created=1
fi
harden_secret "$ROLELENS_DIR/secrets.env"

echo
echo "Installed RoleLens to $ROLELENS_DIR"
echo "  script: $SCRIPTS_DIR/rolelens.py"

if ((${#created[@]})); then
  echo
  echo "Created:"
  printf '  %s\n' "${created[@]}"
fi
if ((secrets_created)); then
  echo "  secrets.env  (from secrets.env.example, mode 600)"
fi
if ((${#kept[@]})); then
  echo
  echo "Kept your existing files, not overwritten:"
  printf '  %s\n' "${kept[@]}"
fi
if ((secrets_created == 0)); then
  echo "  secrets.env"
fi

cat <<EOF

Next:
  1. Edit your configuration and profile:
       $ROLELENS_DIR/config.json
       $ROLELENS_DIR/profile/matcher_profile.json    <- sent to the model each run
       $ROLELENS_DIR/profile/career_profile.json     <- your local evidence base
       $ROLELENS_DIR/profile/search_lenses.json
  2. Add provider credentials:
       $ROLELENS_DIR/secrets.env
  3. Check the installation without spending anything:
       python3 $SCRIPTS_DIR/rolelens.py doctor
  4. Discovery only, ZERO LLM cost:
       python3 $SCRIPTS_DIR/rolelens.py --verbose fetch
  5. Inspect local counters:
       python3 $SCRIPTS_DIR/rolelens.py status
  6. First step that calls a provider:
       python3 $SCRIPTS_DIR/rolelens.py --verbose evaluate
EOF
