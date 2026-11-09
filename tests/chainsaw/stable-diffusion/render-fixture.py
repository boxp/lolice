#!/usr/bin/env python3
"""Reduce rendered gateway manifests to a GPU-free kind fixture on stdout."""
import re
import subprocess
import sys


def drop_block(text, key):
    out, skipping, indent = [], False, 0
    for line in text.split('\n'):
        width = len(line) - len(line.lstrip(' '))
        if skipping and line.strip() and (width > indent or (width == indent and line.lstrip().startswith('- '))):
            continue
        skipping = False
        if re.fullmatch(' *' + key + ':', line):
            skipping, indent = True, width
            continue
        out.append(line)
    return '\n'.join(out)


rendered = subprocess.check_output(['kustomize', 'build', sys.argv[1]], text=True)
kept = []
for document in rendered.split('\n---\n'):
    name = re.search(r'^metadata:\n(?:  .*\n)*?  name: (\S+)', document, re.M)
    kind = re.search(r'^kind: (\S+)', document, re.M)
    if not name or not kind:
        continue
    wanted = ((kind[1] == 'Deployment' and name[1] == 'sd-admission-gateway')
              or (kind[1] == 'ConfigMap' and name[1].startswith(('sd-admission-gateway-envoy', 'sd-intake-code')))
              or (kind[1] == 'Service' and name[1].startswith('stable-diffusion-webui')))
    if wanted:
        if kind[1] == 'Deployment':
            document = drop_block(drop_block(document, 'nodeSelector'), 'tolerations')
            document, count = re.subn(r'hostPath:\n\s+path: \S+\n\s+type: Directory', 'emptyDir: {}', document)
            if count != 1:
                sys.exit('expected exactly one hostPath in the gateway')
        kept.append(document)
if len(kept) != 5:
    sys.exit(f'expected 5 fixture objects, found {len(kept)}')
print('\n---\n'.join(kept))
