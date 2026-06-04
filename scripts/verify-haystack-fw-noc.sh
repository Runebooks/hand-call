#!/usr/bin/env bash
# Verify FWSS token works for Haystack Prom (+ optional MCP) from prometheus-agent pod.
set -euo pipefail

NAMESPACE="${NAMESPACE:-a2a-ops}"
DEPLOY="${DEPLOY:-prometheus-agent}"

kubectl get deploy "${DEPLOY}" -n "${NAMESPACE}" >/dev/null

kubectl exec -n "${NAMESPACE}" "deploy/${DEPLOY}" -- python3 -c "
import os, httpx

tok = os.environ.get('PROMETHEUS_TOKEN', '')
org = os.environ.get('PROMETHEUS_ORG_ID', 'fw-noc')
prom = os.environ.get('PROMETHEUS_URL', '').rstrip('/')
mcp = os.environ.get('HAYSTACK_MCP_URL', '').rstrip('/')
backend = os.environ.get('PROMETHEUS_QUERY_BACKEND', 'direct')

print('backend:', backend)
print('org:', org)
print('token_len:', len(tok))
print('prom_url:', prom)
print('mcp_url:', mcp or '(unset)')

h = {'Authorization': f'Bearer {tok}', 'X-Scope-OrgID': org}
r = httpx.get(f'{prom}/api/v1/query', params={'query': 'up'}, headers=h, timeout=20)
n = len(r.json().get('data', {}).get('result', [])) if r.status_code == 200 else 0
print('PROM:', r.status_code, 'series', n)

if mcp:
    body = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'verify', 'version': '1'},
        },
    }
    r2 = httpx.post(mcp, json=body, headers={**h, 'Content-Type': 'application/json'}, timeout=20)
    print('MCP:', r2.status_code, (r2.text or '')[:120])
"

echo ""
echo "Expect: PROM 200 with series > 0. MCP 200 when observability enables FWSS on o11y.int."
