#!/usr/bin/env bash
set -euo pipefail
root="${REPO_ROOT:-$(git rev-parse --show-toplevel)}"
test -f "$root/argoproj/langfuse/files/mcp-readonly-proxy.mjs" || { echo 'FAIL: proxy is absent'; exit 1; }
TEST_PROXY="$root/argoproj/langfuse/files/mcp-readonly-proxy.mjs" node "$root/tests/chainsaw/langfuse/mcp-proxy-unit.mjs"
