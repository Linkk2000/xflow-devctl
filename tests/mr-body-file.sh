#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

git -C "$tmpdir" init -q
git -C "$tmpdir" config user.email "test@example.com"
git -C "$tmpdir" config user.name "Test User"
git -C "$tmpdir" commit --allow-empty -m "init" -q
git -C "$tmpdir" checkout -b feature/1-demo -q
git -C "$tmpdir" remote add origin git@github.com:example/paper-demo.git
git -C "$tmpdir" config devctl.base main
git -C "$tmpdir" config devctl.issue 1

cat >"$tmpdir/mr-body.md" <<'EOF'
## Summary

- Uses markdown
- Keeps code fenced

```bash
echo "safe through file"
```
EOF

cat >"$tmpdir/provider-stub.sh" <<'EOF'
provider_init() { :; }
provider_pr_create() {
  printf '%s' "$2" >"$DEVCTL_REPO_ROOT/provider-body.out"
  printf '{"html_url":"https://example.test/pr/1","number":1}'
}
EOF

PATH="$tmpdir/bin:$PATH"
mkdir -p "$tmpdir/bin"
cat >"$tmpdir/bin/curl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$tmpdir/bin/jq" <<'EOF'
#!/usr/bin/env bash
python -c 'import sys,json; data=json.load(sys.stdin); expr=sys.argv[1]; print(data.get("html_url" if "html_url" in expr else "number", ""))' "$1"
EOF
chmod +x "$tmpdir/bin/curl" "$tmpdir/bin/jq"

DEVCTL_REPO_ROOT="$tmpdir" \
DEVCTL_PROVIDER_STUB="$tmpdir/provider-stub.sh" \
DEVCTL_SKIP_PUSH=1 \
DEVCTL_SKIP_PROVIDER_LOAD=1 \
  bash "$OPS_ROOT/git/mr.sh" --title "Demo" --body-file "$tmpdir/mr-body.md" --issue 1 >/dev/null

grep -Fq '```bash' "$tmpdir/provider-body.out"

if DEVCTL_REPO_ROOT="$tmpdir" DEVCTL_SKIP_PROVIDER_LOAD=1 \
  bash "$OPS_ROOT/git/mr.sh" --title "Bad" --body $'line 1\nline 2' --issue 1 >/dev/null 2>&1
then
  echo "expected inline multiline MR body to fail" >&2
  exit 1
fi

echo "mr body-file ok"
