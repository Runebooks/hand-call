#!/usr/bin/env bash
# Write .cursor/mcp.json for Haystack Grafana MCP (tenant fw-noc).
# Requires: VPN, gcloud CLI, gcloud auth login.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TENANT="${HAYSTACK_MCP_TENANT:-fw-noc}"
OUT="${CURSOR_MCP_JSON:-${ROOT}/.cursor/mcp.json}"
mkdir -p "$(dirname "${OUT}")"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "Install Google Cloud CLI (see docs/HAYSTACK_GRAFANA_MCP.md)" >&2
  exit 1
fi

JWT="$(gcloud auth print-identity-token 2>/dev/null || true)"
if [[ -z "${JWT}" ]]; then
  echo "Run: gcloud auth login" >&2
  exit 1
fi

cat > "${OUT}" <<EOF
{
  "mcpServers": {
    "grafana-${TENANT}": {
      "url": "https://mcp.haystack.es/grafana/${TENANT}",
      "headers": {
        "Authorization": "Bearer ${JWT}"
      }
    }
  }
}
EOF

chmod 600 "${OUT}" 2>/dev/null || true
echo "Wrote ${OUT} (grafana-${TENANT}, token length ${#JWT})"
echo "Reload Cursor: Cmd+Shift+P → Developer: Reload Window"
