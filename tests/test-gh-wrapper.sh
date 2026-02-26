#!/bin/bash
# Test suite for gh-wrapper policy
# Extracts the wrapper script from the ConfigMap and tests it
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PASS=0
FAIL=0
TOTAL=0

TMPDIR="${TMPDIR:-/tmp}"
WRAPPER="$TMPDIR/gh-wrapper-test.sh"
MOCK_DIR="$TMPDIR/mock-bin"

# Extract wrapper script from ConfigMap (strip leading 4 spaces from YAML literal block)
sed -n '/^  gh: |$/,/^  [a-z]/{ /^  gh: |$/d; /^  [a-z]/d; s/^    //; p; }' \
  "$REPO_DIR/argoproj/openclaw/configmap-gh-wrapper.yaml" > "$WRAPPER"
chmod +x "$WRAPPER"

# Create a mock "real" gh that just prints what it was called with
mkdir -p "$MOCK_DIR"
cat > "$MOCK_DIR/gh" << 'MOCK'
#!/bin/sh
echo "REAL_GH_CALLED: $*"
MOCK
chmod +x "$MOCK_DIR/gh"

# Patch wrapper to use mock
sed -i "s|REAL_GH=\"/usr/bin/gh\"|REAL_GH=\"$MOCK_DIR/gh\"|" "$WRAPPER"

run_test() {
  local desc="$1"
  local expect="$2"  # "allow" or "deny"
  shift 2
  TOTAL=$((TOTAL + 1))

  if output=$("$WRAPPER" "$@" 2>&1); then
    if [ "$expect" = "allow" ]; then
      echo "  PASS: $desc"
      PASS=$((PASS + 1))
    else
      echo "  FAIL: $desc (expected deny, got allow: $output)"
      FAIL=$((FAIL + 1))
    fi
  else
    if [ "$expect" = "deny" ]; then
      echo "  PASS: $desc (denied)"
      PASS=$((PASS + 1))
    else
      echo "  FAIL: $desc (expected allow, got deny: $output)"
      FAIL=$((FAIL + 1))
    fi
  fi
}

echo "=== gh pr tests (merge以外すべて許可) ==="
run_test "gh pr list" allow pr list
run_test "gh pr view 123" allow pr view 123
run_test "gh pr create" allow pr create
run_test "gh pr diff" allow pr diff
run_test "gh pr checks" allow pr checks
run_test "gh pr status" allow pr status
run_test "gh pr checkout 123" allow pr checkout 123
run_test "gh pr close 123" allow pr close 123
run_test "gh pr comment 123" allow pr comment 123
run_test "gh pr edit 123" allow pr edit 123
run_test "gh pr review 123" allow pr review 123
run_test "gh pr reopen 123" allow pr reopen 123
run_test "gh pr ready 123" allow pr ready 123
run_test "gh pr lock 123" allow pr lock 123
run_test "gh pr unlock 123" allow pr unlock 123
run_test "gh pr update-branch" allow pr update-branch
run_test "gh pr revert 123" allow pr revert 123

echo ""
echo "=== gh pr merge (明示的に拒否) ==="
run_test "gh pr merge" deny pr merge
run_test "gh pr merge 123" deny pr merge 123
run_test "gh pr merge --squash" deny pr merge --squash
run_test "gh pr merge --rebase 123" deny pr merge --rebase 123
run_test "gh -R owner/repo pr merge 123" deny -R owner/repo pr merge 123

echo ""
echo "=== gh issue tests (すべて許可) ==="
run_test "gh issue list" allow issue list
run_test "gh issue view 123" allow issue view 123
run_test "gh issue create" allow issue create
run_test "gh issue close 123" allow issue close 123
run_test "gh issue comment 123" allow issue comment 123
run_test "gh issue edit 123" allow issue edit 123
run_test "gh issue delete 123" allow issue delete 123
run_test "gh issue reopen 123" allow issue reopen 123
run_test "gh issue lock 123" allow issue lock 123
run_test "gh issue unlock 123" allow issue unlock 123
run_test "gh issue pin 123" allow issue pin 123
run_test "gh issue unpin 123" allow issue unpin 123
run_test "gh issue transfer 123 owner/repo" allow issue transfer 123 owner/repo
run_test "gh issue develop 123" allow issue develop 123
run_test "gh issue status" allow issue status

echo ""
echo "=== gh api GraphQL tests (query許可, mutation allowlist) ==="
# Queries — all allowed
run_test "graphql query" allow api graphql -f 'query=query { repository(owner:"o",name:"r") { id } }'
run_test "graphql shorthand query" allow api graphql -f 'query={ repository(owner:"o",name:"r") { id } }'
run_test "graphql query with variables" allow api graphql -f 'query=query($o:String!){repository(owner:$o,name:"r"){id}}' -f 'variables={"o":"owner"}'
run_test "graphql query with operationName" allow api graphql -f 'query=query GetRepo{repository{id}}' -f 'operationName=GetRepo'

# Allowlisted mutations — allowed
run_test "graphql mutation addComment" allow api graphql -f 'query=mutation { addComment(input:{subjectId:"x",body:"hi"}) { commentEdge { node { id } } } }'
run_test "graphql mutation named addComment" allow api graphql -f 'query=mutation AddC($i:AddCommentInput!) { addComment(input:$i) { commentEdge { node { id } } } }'
run_test "graphql mutation addProjectV2ItemById" allow api graphql -f 'query=mutation { addProjectV2ItemById(input:{projectId:"p",contentId:"c"}) { item { id } } }'
run_test "graphql mutation closeIssue" allow api graphql -f 'query=mutation { closeIssue(input:{issueId:"i"}) { issue { id } } }'
run_test "graphql mutation updateProjectV2ItemFieldValue" allow api graphql -f 'query=mutation { updateProjectV2ItemFieldValue(input:{projectId:"p",itemId:"i",fieldId:"f",value:{text:"v"}}) { projectV2Item { id } } }'
run_test "graphql mutation createIssue" allow api graphql -f 'query=mutation { createIssue(input:{repositoryId:"r",title:"t"}) { issue { id } } }'

# Non-allowlisted mutations — denied
run_test "graphql mutation createRepository (deny)" deny api graphql -f 'query=mutation { createRepository(input:{name:"evil"}) { repository { id } } }'
run_test "graphql mutation mergePullRequest (deny)" deny api graphql -f 'query=mutation { mergePullRequest(input:{pullRequestId:"x"}) { pullRequest { id } } }'
run_test "graphql mutation deleteUser (deny)" deny api graphql -f 'query=mutation { deleteUser(input:{userId:"x"}) { clientMutationId } }'

# Alias bypass — denied (actual mutation name is checked, not alias)
run_test "graphql alias bypass (deny)" deny api graphql -f 'query=mutation { safe:createRepository(input:{name:"evil"}) { repository { id } } }'
run_test "graphql alias allowed mutation" allow api graphql -f 'query=mutation { myComment:addComment(input:{subjectId:"x",body:"hi"}) { commentEdge { node { id } } } }'

# Multiple mutations — mixed allowed+denied → denied
run_test "graphql multi-mutation mixed (deny)" deny api graphql -f 'query=mutation { addComment(input:{subjectId:"x",body:"hi"}) { commentEdge { node { id } } } deleteRepository(input:{repositoryId:"r"}) { clientMutationId } }'

# Multiple mutations — all allowed → allowed
run_test "graphql multi-mutation all allowed" allow api graphql -f 'query=mutation { addComment(input:{subjectId:"x",body:"a"}) { commentEdge { node { id } } } closeIssue(input:{issueId:"i"}) { issue { id } } }'

# Unparseable mutation — denied (fail-closed)
run_test "graphql unparseable mutation (deny)" deny api graphql -f 'query=mutation { }'

# Multi-operation document bypass — mutation not at start → denied
run_test "graphql multi-op bypass (deny)" deny api graphql -f 'query=query Q { repository { id } } mutation M { deleteRepository(input:{repositoryId:"r"}) { clientMutationId } }' -f 'operationName=M'
run_test "graphql multi-op bypass allowed mutation (deny)" deny api graphql -f 'query=query Q { repository { id } } mutation M { addComment(input:{subjectId:"x",body:"hi"}) { commentEdge { node { id } } } }' -f 'operationName=M'

# Comment bypass — GraphQL comment before mutation → denied (mutation detected in full text)
run_test "graphql comment bypass (deny)" deny api graphql -f "$(printf 'query=#comment\nmutation { deleteRepository(input:{repositoryId:"r"}) { clientMutationId } }')"

# String-with-hash bypass — # inside string should not hide mutation keyword
run_test "graphql string hash bypass (deny)" deny api graphql -f 'query=query Q { field(arg: "#") } mutation M { deleteRepository(input:{repositoryId:"r"}) { clientMutationId } }'

# clientMutationId in query should not trigger false positive
run_test "graphql query with clientMutationId field" allow api graphql -f 'query=query { node(id:"x") { ... on Issue { id clientMutationId } } }'

# P2: "mutation" inside string literals should not trigger mutation detection
run_test "graphql query with mutation in string literal" allow api graphql -f 'query=query { search(query:"foo mutation bar", type:ISSUE, first:10) { nodes { id } } }'
run_test "graphql query with mutation in repo name string" allow api graphql -f 'query={ repository(owner:"o",name:"mutation-tracker") { id } }'
run_test "graphql query with escaped quote and mutation in string" allow api graphql -f 'query=query { search(query:"test \"mutation\" value", type:ISSUE) { nodes { id } } }'
run_test "graphql query with mutation in block string" allow api graphql -f 'query=query { search(query:"""foo " mutation " bar""", type:ISSUE) { nodes { id } } }'
run_test "graphql query with escaped triple quote in block string" allow api graphql -f 'query=query { f(arg:"""prefix \""" mutation suffix""") }'

# -F/--field — denied (supports @file bypass)
run_test "graphql -F (deny)" deny api graphql -F 'query=query { repository { id } }'
run_test "graphql --field (deny)" deny api graphql --field 'query=query { repository { id } }'

# --input — denied
run_test "graphql --input (deny)" deny api graphql --input body.json

# File reference @file — denied
run_test "graphql @file query (deny)" deny api graphql -f 'query=@evil.graphql'

# Disallowed extra fields — denied
run_test "graphql extra field (deny)" deny api graphql -f 'query=query{repository{id}}' -f 'title=evil'

echo ""
echo "=== P1: Non-GraphQL API with 'graphql' in option values ==="
# P1: "graphql" as an option value should NOT trigger GraphQL path
run_test "api --jq graphql (non-graphql GET, allow)" allow api repos/o/r --jq graphql
run_test "api --jq graphql -XPUT (non-graphql PUT, deny)" deny api repos/o/r --jq graphql -X PUT
run_test "api -H graphql repos/o/r (non-graphql GET, allow)" allow api -H graphql repos/o/r

echo ""
echo "=== その他のコマンド ==="
run_test "gh auth status" allow auth status
run_test "gh auth login (deny)" deny auth login
run_test "gh run list" allow run list
run_test "gh run view 123" allow run view 123
run_test "gh run cancel (deny)" deny run cancel 123
run_test "gh repo list (deny)" deny repo list
run_test "gh api repos/o/r" allow api repos/o/r
run_test "gh api -XPOST (deny)" deny api -XPOST repos/o/r
run_test "gh (no args, deny)" deny

echo ""
echo "=============================="
echo "Total: $TOTAL  Pass: $PASS  Fail: $FAIL"
echo "=============================="

# Cleanup
rm -f "$WRAPPER"
rm -rf "$MOCK_DIR"

[ "$FAIL" -eq 0 ]
