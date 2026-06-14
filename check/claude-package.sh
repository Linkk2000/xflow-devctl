#!/usr/bin/env bash
set -euo pipefail

OPS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVCTL_SKIP_PROVIDER_LOAD=1
source "$OPS_ROOT/lib/common.sh"
source "$OPS_ROOT/check/academic-common.sh"

academic_parse_issue_file_args "$@"
file="${academic_file:-$(academic_default_file claude-task.md)}"

academic_check_template "$file" \
  "# Claude Task Package" \
  "Issue:" \
  "Claude Skill:" \
  "Skill Source:" \
  "Invocation:" \
  "Input Files:" \
  "Output File:" \
  "## Objective" \
  "## Constraints" \
  "## Required Output Format" \
  "## Human Review Requirement"

if grep -Eq '^AcademicForge Skill:' "$file"; then
  devctl_die "obsolete Claude skill field in $file: use Claude Skill, Skill Source, and Invocation"
fi

source_value="$(grep -E '^Skill Source:' "$file" | head -n 1 | sed 's/^Skill Source:[[:space:]]*//')"
skill_value="$(grep -E '^Claude Skill:' "$file" | head -n 1 | sed 's/^Claude Skill:[[:space:]]*//')"
invocation_value="$(grep -E '^Invocation:' "$file" | head -n 1 | sed 's/^Invocation:[[:space:]]*//')"
invocation_skill="$(printf '%s\n' "$invocation_value" | sed -n 's#^/\([A-Za-z0-9][A-Za-z0-9_.-]*\)\([[:space:]].*\)\{0,1\}$#\1#p')"

if [[ -z "$invocation_skill" ]]; then
  devctl_die "Invocation must start with an explicit Claude skill command such as /peer-review"
fi

if [[ "$skill_value" != "$invocation_skill" ]]; then
  devctl_die "Claude Skill '$skill_value' does not match Invocation '/$invocation_skill'"
fi

catalog="$OPS_ROOT/xflow/catalogs/academicforge-skills.txt"
if printf '%s\n' "$source_value" | grep -Eiq 'academicforge'; then
  [[ -f "$catalog" ]] || devctl_die "missing AcademicForge skill catalog: $catalog"
  if ! tr -d '\r' <"$catalog" | grep -Fxq "$skill_value"; then
    devctl_die "unknown AcademicForge skill '$skill_value': update the catalog or choose a verified skill"
  fi
fi

devctl_info "claude-package check passed: $file"
