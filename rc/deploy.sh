#!/bin/bash
# RapidCanvas deploy: module + artifacts + recipe code → run
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

HASH_FILE="$SCRIPT_DIR/.deploy-hashes"
FORCE=false
SKIP_RUN=false
for arg in "$@"; do
  [ "$arg" = "--force" ] && FORCE=true
  [ "$arg" = "--no-run" ] && SKIP_RUN=true
done

# ── Parse deploy.yaml ────────────────────────────────────

parse_config() {
  python3 -c "
import yaml, shlex, os

c = yaml.safe_load(open('$SCRIPT_DIR/deploy.yaml'))

def emit(var, val):
    if isinstance(val, list):
        print(f'{var}=({\" \".join(shlex.quote(str(v)) for v in val)})')
    else:
        print(f'{var}={shlex.quote(str(val))}')

emit('RC_PROJECT_ID',       os.environ.get('RC_PROJECT_ID') or c['project_id'])
emit('MODULE_NAME',         c['module']['name'])
emit('MODULE_DESC',         c['module']['description'])
emit('MODULE_REQUIREMENTS', c['module']['requirements'])
emit('MODULE_FILES',        c['module']['files'])
emit('RECIPE_NAME',         c['recipe']['name'])
emit('RECIPE_DISPLAY_NAME', c['recipe']['display_name'])
emit('RUNNER_FILE',         c['recipe']['runner'])
emit('ARTIFACT_NAME',       c['artifacts']['name'])
emit('ARTIFACT_FILES',      c['artifacts']['files'])

reqs = c['recipe']['requirements']
if isinstance(reqs, list):
    reqs = r'\n'.join(reqs)
emit('RECIPE_REQUIREMENTS', reqs)
"
}

eval "$(parse_config)"

FAILED=()

# ── Helpers ───────────────────────────────────────────────

json_escape() { python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" < "$1"; }

files_hash() { cat "$@" | shasum -a 256 | cut -d' ' -f1; }

get_cached_hash() {
  [ -f "$HASH_FILE" ] && grep "^$1 " "$HASH_FILE" | cut -d' ' -f2 || echo ""
}

set_cached_hash() {
  if [ -f "$HASH_FILE" ]; then
    grep -v "^$1 " "$HASH_FILE" > "$HASH_FILE.tmp" || true
    mv "$HASH_FILE.tmp" "$HASH_FILE"
  fi
  echo "$1 $2" >> "$HASH_FILE"
}

api() {
  local response http_code
  response=$(curl -s -w "\n%{http_code}" "$@")
  http_code=$(echo "$response" | tail -1)
  local body
  body=$(echo "$response" | sed '$d')
  if [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
    echo "$body"
    return 0
  else
    echo "[HTTP $http_code] $body" >&2
    return 1
  fi
}

fail() { echo "[FAIL] $1"; FAILED+=("$1"); }

parse_json() {
  python3 -c "
import json,sys
data = json.loads(sys.stdin.read(), strict=False)
if isinstance(data, list): data = data[0] if data else {}
print(data.get('$1') or '')
" 2>/dev/null
}

# ── Load env & config ─────────────────────────────────────

[ -z "${RAPIDCANVAS_API_KEY:-}" ] && { echo "[ERROR] RAPIDCANVAS_API_KEY not set"; exit 1; }
API_HOST="${RC_API_HOST:-https://app.rapidcanvas.ai}"

echo "=========================================="
echo "${RECIPE_DISPLAY_NAME} — RC Deploy"
echo "=========================================="
echo "  Project: $RC_PROJECT_ID"
echo "  Host:    $API_HOST"
echo ""

# ── Step 1: Bearer token ─────────────────────────────────

echo "Step 1: Authenticate"
TOKEN_BODY=$(api "$API_HOST/api/access_key/token" \
  -H "X-API-KEY: $RAPIDCANVAS_API_KEY" -H "Accept: application/json") || {
  echo "[ERROR] Failed to get token"; exit 1;
}
BEARER_TOKEN=$(echo "$TOKEN_BODY" | jq -r '.token // .access_token // .' 2>/dev/null)
[ -z "$BEARER_TOKEN" ] || [ "$BEARER_TOKEN" = "null" ] && BEARER_TOKEN=$(echo "$TOKEN_BODY" | tr -d '"')
[ -z "$BEARER_TOKEN" ] && { echo "[ERROR] Empty token"; exit 1; }

AUTH=(-H "Authorization: Bearer $BEARER_TOKEN")
JSON=(-H "Content-Type: application/json")
echo "[OK] Authenticated"

# ── Step 2: Code module ──────────────────────────────────

echo ""
echo "Step 2: Code module ($MODULE_NAME)"
MODULE_HASH=$(files_hash "${MODULE_FILES[@]}")
MODULE_CHANGED=false

if [ "$FORCE" = false ] && [ "$(get_cached_hash module)" = "$MODULE_HASH" ]; then
  echo "[SKIP] Unchanged"
  MODULE_ID=$(api "${AUTH[@]}" "$API_HOST/api/v2/projects/$RC_PROJECT_ID/custom-modules/by-name/$MODULE_NAME" 2>/dev/null | jq -r '.id // empty' 2>/dev/null) || true
else
  FILES_PAYLOAD="["
  for f in "${MODULE_FILES[@]}"; do
    [ "$FILES_PAYLOAD" != "[" ] && FILES_PAYLOAD+=","
    FILES_PAYLOAD+="{\"filePath\":\"$(basename "$f")\",\"content\":$(json_escape "$f")}"
  done
  FILES_PAYLOAD+="]"

  MODULE_BODY=$(api "${AUTH[@]}" "$API_HOST/api/v2/projects/$RC_PROJECT_ID/custom-modules/by-name/$MODULE_NAME" 2>/dev/null) || true
  MODULE_ID=$(echo "${MODULE_BODY:-}" | jq -r '.id // empty' 2>/dev/null)

  if [ -n "$MODULE_ID" ]; then
    api -X PUT "${AUTH[@]}" "${JSON[@]}" \
      -d "{\"description\":\"$MODULE_DESC\",\"requirements\":\"$MODULE_REQUIREMENTS\",\"files\":$FILES_PAYLOAD}" \
      "$API_HOST/api/v2/projects/$RC_PROJECT_ID/custom-modules/$MODULE_ID" > /dev/null || { fail "Module update"; MODULE_ID=""; }
    [ -n "$MODULE_ID" ] && echo "[OK] Updated"
  else
    RESP=$(api -X POST "${AUTH[@]}" "${JSON[@]}" \
      -d "{\"name\":\"$MODULE_NAME\",\"description\":\"$MODULE_DESC\",\"requirements\":\"$MODULE_REQUIREMENTS\",\"mode\":\"editor\",\"files\":$FILES_PAYLOAD}" \
      "$API_HOST/api/v2/projects/$RC_PROJECT_ID/custom-modules") || { fail "Module create"; }
    MODULE_ID=$(echo "${RESP:-}" | jq -r '.id // empty' 2>/dev/null)
    [ -n "$MODULE_ID" ] && echo "[OK] Created ($MODULE_ID)"
  fi
  [ -n "$MODULE_ID" ] && { set_cached_hash module "$MODULE_HASH"; MODULE_CHANGED=true; }
fi

# ── Step 3: Artifacts ────────────────────────────────────

echo ""
echo "Step 3: Artifacts ($ARTIFACT_NAME)"
ARTIFACT_FILES_EXIST=()
for f in "${ARTIFACT_FILES[@]}"; do
  [ -f "$f" ] && ARTIFACT_FILES_EXIST+=("$f") || echo "[WARN] Artifact file missing (gitignored?): $f"
done

if [ ${#ARTIFACT_FILES_EXIST[@]} -eq 0 ]; then
  ARTIFACT_HASH=""
else
  ARTIFACT_HASH=$(files_hash "${ARTIFACT_FILES_EXIST[@]}")
fi

upload_artifact_file() {
  local filepath="$1" artifact_path="$2" filename
  filename=$(basename "$filepath")
  local encoded_path
  encoded_path=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe='/'))" "$artifact_path")
  SIGNED=$(api "${AUTH[@]}" "$API_HOST/api/v2/artifacts/signed-url?path=$encoded_path") || { fail "Signed URL for $filename"; return 1; }
  SIGNED_URL=$(echo "$SIGNED" | python3 -c "import json,sys; print(json.loads(sys.stdin.read(),strict=False).get('signedUrl',''))" 2>/dev/null)
  CT=$(echo "$SIGNED" | python3 -c "import json,sys; print(json.loads(sys.stdin.read(),strict=False).get('headers',{}).get('Content-Type','application/octet-stream'))" 2>/dev/null)
  api -X PUT "$SIGNED_URL" -H "Content-Type: $CT" --data-binary "@$filepath" > /dev/null || { fail "Upload $filename"; return 1; }
  echo "[OK] $filename"
}

if [ ${#ARTIFACT_FILES_EXIST[@]} -eq 0 ]; then
  echo "[SKIP] No artifact files present (gitignored?)"
elif [ "$FORCE" = false ] && [ -n "$ARTIFACT_HASH" ] && [ "$(get_cached_hash artifacts)" = "$ARTIFACT_HASH" ]; then
  echo "[SKIP] Unchanged"
else
  # Ensure artifact folder exists
  api -X POST "${AUTH[@]}" "$API_HOST/api/v2/artifacts/empty-folder/$ARTIFACT_NAME" > /dev/null 2>&1 || true

  for f in "${ARTIFACT_FILES_EXIST[@]}"; do
    upload_artifact_file "$f" "$ARTIFACT_NAME/$(basename "$f")"
  done
  [ -n "$ARTIFACT_HASH" ] && set_cached_hash artifacts "$ARTIFACT_HASH"
fi

# ── Step 4: Recipe + template ────────────────────────────

echo ""
echo "Step 4: Recipe ($RECIPE_NAME)"
RECIPES_BODY=$(api "${AUTH[@]}" "$API_HOST/api/v2/dfs-run-config-groups?projectId=$RC_PROJECT_ID&name=$RECIPE_NAME" 2>/dev/null) || true
RECIPE_ID=$(echo "${RECIPES_BODY:-}" | parse_json id)

if [ -n "$RECIPE_ID" ]; then
  echo "[OK] Exists ($RECIPE_ID)"
  TEMPLATE_ID=$(python3 -c "
import json,sys
data = json.loads(sys.stdin.read(), strict=False)
r = data[0] if isinstance(data, list) else data
rcs = r.get('runConfigs', [])
print(rcs[0].get('templateId','') if rcs else '')
" <<< "$RECIPES_BODY" 2>/dev/null)
else
  echo "[INFO] Creating..."
  RESP=$(api -X POST "${AUTH[@]}" "${JSON[@]}" \
    -d '{"name":"'"$RECIPE_NAME"'","displayName":"'"$RECIPE_DISPLAY_NAME"'","recipeType":"API_CONNECTOR","timeout":1}' \
    "$API_HOST/api/v2/dfs-run-config-groups/$RC_PROJECT_ID") || { fail "Recipe create"; }
  RECIPE_ID=$(echo "${RESP:-}" | parse_json id)
  [ -n "$RECIPE_ID" ] && echo "[OK] Created ($RECIPE_ID)"
  TEMPLATE_ID=""
fi

if [ -n "${RECIPE_ID:-}" ] && [ -z "${TEMPLATE_ID:-}" ]; then
  echo "[INFO] Creating template + run config..."
  TPL=$(api -X POST "${AUTH[@]}" "${JSON[@]}" \
    -d "{\"name\":\"${RECIPE_NAME}-runner\",\"type\":\"CODE\",\"source\":\"CUSTOM\",\"projectId\":\"$RC_PROJECT_ID\",\"code\":\"# placeholder\",\"requirements\":\"$RECIPE_REQUIREMENTS\",\"tags\":[\"code-template\"]}" \
    "$API_HOST/api/v2/dfs-templates") || { fail "Template create"; }
  TEMPLATE_ID=$(echo "${TPL:-}" | jq -r '.id // empty' 2>/dev/null)

  if [ -n "$TEMPLATE_ID" ]; then
    api -X POST "${AUTH[@]}" "${JSON[@]}" \
      -d "{\"name\":\"${RECIPE_NAME}-runner\",\"groupId\":\"$RECIPE_ID\",\"projectId\":\"$RC_PROJECT_ID\",\"templateId\":\"$TEMPLATE_ID\"}" \
      "$API_HOST/api/v2/dfs-run-configs" > /dev/null || { fail "Run config create"; }
    echo "[OK] Template ($TEMPLATE_ID)"
  fi
fi

# ── Step 5: Upload runner code ───────────────────────────

echo ""
echo "Step 5: Recipe code ($RUNNER_FILE)"
RUNNER_HASH=$(files_hash "$RUNNER_FILE")

if [ "$FORCE" = false ] && [ "$(get_cached_hash runner)" = "$RUNNER_HASH" ]; then
  echo "[SKIP] Unchanged"
else
  if [ -n "${TEMPLATE_ID:-}" ]; then
    PAYLOAD_FILE=$(mktemp)
    python3 -c "
import json
print(json.dumps({
    'code': open('$RUNNER_FILE').read(),
    'tags': ['code-template'],
    'requirements': '$RECIPE_REQUIREMENTS'
}))
" > "$PAYLOAD_FILE"

    RESP=$(api -X PATCH "${AUTH[@]}" "${JSON[@]}" \
      -d "@$PAYLOAD_FILE" \
      "$API_HOST/api/v2/dfs-templates/$TEMPLATE_ID") || { fail "Template PATCH"; }
    rm -f "$PAYLOAD_FILE"

    CODE_LEN=$(echo "${RESP:-}" | jq -r '.code | length' 2>/dev/null || echo 0)
    if [ "${CODE_LEN:-0}" -gt 100 ] 2>/dev/null; then
      echo "[OK] Updated ($CODE_LEN chars)"
      set_cached_hash runner "$RUNNER_HASH"
    else
      fail "Template code too short ($CODE_LEN)"
    fi
  else
    fail "No template ID"
  fi
fi

# ── Step 6: Run ──────────────────────────────────────────

echo ""
if [ ${#FAILED[@]} -gt 0 ]; then
  echo "Step 6: Run — SKIPPED (${#FAILED[@]} failures above)"
elif [ "$SKIP_RUN" = true ]; then
  echo "Step 6: Run — SKIPPED (--no-run)"
else
  echo "Step 6: Run recipe"

  # Mark unbuilt if module changed (forces reinstall)
  if [ "$MODULE_CHANGED" = true ] || [ "$FORCE" = true ]; then
    api -X POST "${AUTH[@]}" "$API_HOST/api/v2/dfs-run-config-groups/$RECIPE_ID/mark-unbuilt" > /dev/null 2>&1 || true
    echo "[INFO] Marked unbuilt (module changed)"
  fi

  # Get scenario ID
  SCENARIO_ID=$(api "${AUTH[@]}" "$API_HOST/api/v2/scenarios?projectId=$RC_PROJECT_ID" 2>/dev/null | python3 -c "
import json,sys
for s in json.loads(sys.stdin.read()):
    if s.get('name') == 'DEFAULT':
        print(s['id']); break
" 2>/dev/null) || true

  if [ -z "${SCENARIO_ID:-}" ]; then
    fail "Could not find DEFAULT scenario"
  else
    api -X POST "${AUTH[@]}" \
      "$API_HOST/api/v2/dfs-run-config-groups/run/$RECIPE_ID?scenarioId=$SCENARIO_ID" > /dev/null || { fail "Trigger run"; }
    echo "[OK] Run triggered"

    echo "[INFO] Polling status..."
    for i in $(seq 1 30); do
      sleep 10
      STATUS=$(api "${AUTH[@]}" \
        "$API_HOST/api/v2/dfs-run-config-groups/$RECIPE_ID/live-status?scenarioId=$SCENARIO_ID" 2>/dev/null | parse_json status) || true
      echo "  [$i] $STATUS"
      case "$STATUS" in
        SUCCESS) echo "[OK] Run completed successfully"; break ;;
        ERROR|FAILURE)
          fail "Run failed ($STATUS)"
          echo "[INFO] Check logs on RC canvas or:"
          echo "  curl -s -H 'Authorization: Bearer <token>' \\"
          echo "    '$API_HOST/api/v2/dfs-run-config-groups/$RECIPE_ID/live-status?scenarioId=$SCENARIO_ID'"
          break ;;
      esac
    done
  fi
fi

# ── Summary ──────────────────────────────────────────────

echo ""
echo "=========================================="
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "[DONE] Deploy + run complete"
else
  echo "[DONE] Finished with ${#FAILED[@]} issue(s):"
  for f in "${FAILED[@]}"; do echo "  - $f"; done
fi
echo "=========================================="
echo "  Module:   ${MODULE_ID:-unknown}"
echo "  Recipe:   ${RECIPE_ID:-unknown}"
echo "  Template: ${TEMPLATE_ID:-unknown}"
echo "=========================================="

exit ${#FAILED[@]}
