#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROLELENS_DIR="${ROLELENS_HOME:-$HOME/.rolelens}"
SCRIPTS_DIR="${ROLELENS_SCRIPTS_HOME:-$HOME/.local}/scripts"

install -d -m 700 "$ROLELENS_DIR" "$ROLELENS_DIR/profile" "$ROLELENS_DIR/data"
install -d -m 700 "$SCRIPTS_DIR"

install -m 600 "$SOURCE_DIR/config.json" "$ROLELENS_DIR/config.json"
install -m 600 "$SOURCE_DIR/profile/career_profile.json" "$ROLELENS_DIR/profile/career_profile.json"
install -m 600 "$SOURCE_DIR/profile/matcher_profile.json" "$ROLELENS_DIR/profile/matcher_profile.json"
install -m 600 "$SOURCE_DIR/profile/search_lenses.json" "$ROLELENS_DIR/profile/search_lenses.json"
install -m 600 "$SOURCE_DIR/profile/matcher_rules_v1_1.json" "$ROLELENS_DIR/profile/matcher_rules_v1_1.json"
install -m 700 "$SOURCE_DIR/rolelens.py" "$SCRIPTS_DIR/rolelens.py"

if [[ ! -e "$ROLELENS_DIR/secrets.env" ]]; then
  install -m 600 "$SOURCE_DIR/secrets.env.example" "$ROLELENS_DIR/secrets.env"
  echo "Created $ROLELENS_DIR/secrets.env. Add the Vertex and Azure values before running doctor."
else
  chmod 600 "$ROLELENS_DIR/secrets.env"
  echo "Kept existing $ROLELENS_DIR/secrets.env"
fi

cat <<EOF
Installed RoleLens.

Next:
  1. Edit: $ROLELENS_DIR/secrets.env
  2. Test config:
       python3 $SCRIPTS_DIR/rolelens.py doctor
  3. Test discovery with ZERO LLM cost:
       python3 $SCRIPTS_DIR/rolelens.py --verbose fetch
  4. Inspect DB counters:
       python3 $SCRIPTS_DIR/rolelens.py status
  5. Manually run semantic matching:
       python3 $SCRIPTS_DIR/rolelens.py --verbose evaluate
EOF
