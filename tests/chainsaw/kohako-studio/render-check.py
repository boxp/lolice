import json
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
def objects(name):
    output = subprocess.check_output(['yq', '-o=json', '-I=0', '.', str(root / name)], text=True)
    return [json.loads(line) for line in output.splitlines() if line.strip() and line != '---']
def get(items, kind, name):
    return next(x for x in items if x.get('kind') == kind and x['metadata']['name'] == name)

studio, workspace, hermes = (objects(x) for x in ('kohako-studio', 'codex-workspace', 'hermes-agent'))
role = get(studio, 'ClusterRole', 'kohako-studio-tokenreview')
assert role['rules'] == [{'apiGroups': ['authentication.k8s.io'], 'resources': ['tokenreviews'], 'verbs': ['create']}]
binding = get(studio, 'ClusterRoleBinding', 'kohako-studio-tokenreview')
assert binding['roleRef']['name'] == 'kohako-studio-tokenreview'
assert binding['subjects'] == [{'kind': 'ServiceAccount', 'name': 'kohako-studio', 'namespace': 'kohako-studio'}]
policy = get(studio, 'NetworkPolicy', 'kohako-studio')['spec']
assert set(policy['types']) == {'Ingress', 'Egress'}
allowed = {(r['source']['namespaceSelector'], r['source']['selector'], tuple(r['destination']['ports'])) for r in policy['ingress'] if r['action'] == 'Allow' and r['protocol'] == 'TCP'}
assert allowed == {("kubernetes.io/metadata.name == 'even-g2-lab'", "app == 'cloudflared'", (8080,)), ("kubernetes.io/metadata.name == 'codex-workspace'", "app == 'codex-workspace'", (8080,))}
assert len(policy['ingress']) == len(allowed)  # no other ingress, including other namespaces or ports
assert any(r['destination'].get('selector') == "app == 'hermes-agent'" and r['destination']['ports'] == [8642] for r in policy['egress'])
deploy = get(studio, 'Deployment', 'kohako-studio')['spec']['template']['spec']
assert deploy['serviceAccountName'] == 'kohako-studio'
bff = next(c for c in deploy['containers'] if c['name'] == 'bff')
env = {x['name']: x.get('value') for x in bff['env']}
assert env['KOHAKO_ACCESS_AUD'] == 'bd491ea940b2fe8ced4ff62699c58a02bce6919da7986a513189fc44e9caec85'
assert env['KOHAKO_VERIFIER_ENABLED'] == 'true' and 'KOHAKO_STT_URL' not in env
ws = get(workspace, 'Deployment', 'codex-workspace')['spec']['template']['spec']
volume = next(x for x in ws['volumes'] if x['name'] == 'kohako-bff-verifier')
assert volume['projected']['sources'] == [{'serviceAccountToken': {'audience': 'kohako-bff-verifier', 'expirationSeconds': 3600, 'path': 'token'}}]
mounts = [(c['name'], m) for c in ws['containers'] for m in c.get('volumeMounts', []) if m['name'] == 'kohako-bff-verifier']
assert mounts == [('workspace', {'name': 'kohako-bff-verifier', 'mountPath': '/var/run/secrets/kohako-bff-verifier', 'readOnly': True})]
# BOXP-501: notification ingest token is audience-bound and mounted only into the event dispatcher.
volume = next(x for x in ws['volumes'] if x['name'] == 'kohako-bff-notifier')
assert volume['projected']['sources'] == [{'serviceAccountToken': {'audience': 'kohako-bff-notifier', 'expirationSeconds': 3600, 'path': 'token'}}]
mounts = [(c['name'], m) for c in ws['containers'] for m in c.get('volumeMounts', []) if m['name'] == 'kohako-bff-notifier']
assert mounts == [('task-board-event-dispatcher', {'name': 'kohako-bff-notifier', 'mountPath': '/var/run/secrets/kohako-bff-notifier', 'readOnly': True})]
assert env['KOHAKO_DATA_DIR'] == '/data' and env['KOHAKO_NOTIFIER_ENABLED'] == 'true' and env['KOHAKO_NOTIFIER_USERNAME'] == 'system:serviceaccount:codex-workspace:codex-workspace'
assert {'name': 'data', 'mountPath': '/data'} in bff['volumeMounts']
assert get(studio, 'Deployment', 'kohako-studio')['spec']['strategy'] == {'type': 'Recreate', 'rollingUpdate': None}
assert get(studio, 'Deployment', 'kohako-studio')['metadata']['annotations']['argocd.argoproj.io/sync-options'] == 'ServerSideApply=false'
assert next(x for x in deploy['volumes'] if x['name'] == 'data')['persistentVolumeClaim'] == {'claimName': 'kohako-studio-data'}
assert get(studio, 'PersistentVolumeClaim', 'kohako-studio-data')['spec']['accessModes'] == ['ReadWriteOnce']
def permits(policy, side, namespace, selector, port):
    for rule in policy[side]:
        peer = rule['source' if side == 'ingress' else 'destination']
        dest = rule['destination']
        if rule.get('action') == 'Allow' and rule.get('protocol') == 'TCP' and peer.get('namespaceSelector') == f"kubernetes.io/metadata.name == '{namespace}'" and peer.get('selector') == f"app == '{selector}'" and port in dest.get('ports', []):
            return True
    return False
assert permits(get(workspace, 'NetworkPolicy', 'codex-workspace-network-policy')['spec'], 'egress', 'kohako-studio', 'kohako-studio', 8080)
assert permits(get(hermes, 'NetworkPolicy', 'hermes-agent-api-ingress')['spec'], 'ingress', 'kohako-studio', 'kohako-studio', 8642)
# BOXP-504: kohako-home status strip reads only the read-only Task Board projection (8090) and AI metrics (9464).
for port in (8090, 9464):
    assert permits(policy, 'egress', 'codex-workspace', 'codex-workspace', port)
    assert permits(get(workspace, 'NetworkPolicy', 'codex-workspace-network-policy')['spec'], 'ingress', 'kohako-studio', 'kohako-studio', port)
assert not permits(get(workspace, 'NetworkPolicy', 'codex-workspace-network-policy')['spec'], 'ingress', 'kohako-studio', 'kohako-studio', 8080)
secret_role = get(hermes, 'Role', 'kohako-api-server-key-reader')
assert secret_role['rules'] == [{'apiGroups': [''], 'resources': ['secrets'], 'resourceNames': ['hermes-agent-api-server-key'], 'verbs': ['get']}]
assert {'kind': 'ServiceAccount', 'name': 'kohako-api-server-key-reader', 'namespace': 'kohako-studio'} in get(hermes, 'RoleBinding', 'kohako-api-server-key-reader')['subjects']
print('kohako-studio render checks passed')
