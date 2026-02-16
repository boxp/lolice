# Re-enable vr-match (hitohub) stage workloads

## Overview
Re-enable the vr-match (hitohub) stage environment workloads by changing `replicas: 0` to `replicas: 1`.

## Changes
- `argoproj/hitohub/overlays/stage/deployment-hitohub-backend.yaml`: replicas 0 -> 1
- `argoproj/hitohub/overlays/stage/deployment-hitohub-frontend.yaml`: replicas 0 -> 1
- `argoproj/hitohub/overlays/stage/deployment-cloudflared.yaml`: replicas 0 -> 1

## Impact
- Stage environment will have hitohub backend, frontend, and cloudflared tunnel running with 1 replica each
- No impact on production environment
