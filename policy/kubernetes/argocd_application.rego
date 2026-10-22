# Argo CD Application の Helm chart source に関する policy (BOXP-266)
#
# Renovate の argocd manager が更新する targetRevision が、範囲指定や branch 名ではなく
# 固定版 (X.Y.Z / vX.Y.Z) であることを要求する。chart の自動 merge は固定版の更新に限る。
package main

application_sources contains s if {
	input.kind == "Application"
	s := input.spec.source
}

application_sources contains s if {
	input.kind == "Application"
	some s in input.spec.sources
}

deny contains msg if {
	some s in application_sources
	s.chart
	rev := sprintf("%v", [object.get(s, "targetRevision", "")])
	not regex.match(`^v?[0-9]+\.[0-9]+\.[0-9]+$`, rev)
	msg := sprintf("Application/%s: chart %q の targetRevision %q は固定版 (X.Y.Z) にしてください", [input.metadata.name, s.chart, rev])
}
