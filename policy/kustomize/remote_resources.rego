# kustomization.yaml の remote resource に関する policy (BOXP-266)
#
# github.com を参照する resource (argocd base, kube-prometheus, image-updater など) は
# Renovate が ?ref= を更新する。branch 名や HEAD ではなく固定 tag (vX.Y.Z / X.Y.Z) を要求する。
package main

deny contains msg if {
	input.kind == "Kustomization"
	some r in input.resources
	regex.match(`^(https?://)?github\.com/`, r)
	not regex.match(`\?ref=v?[0-9]+\.[0-9]+\.[0-9]+$`, r)
	msg := sprintf("remote resource %q は ?ref=<固定 tag> にしてください", [r])
}
