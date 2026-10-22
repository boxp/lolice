# コンテナ image の固定に関する policy (BOXP-266)
#
# Renovate が自動 merge するのは image tag / digest の更新がほとんどなので、
# render 後のマニフェストで「tag も digest も無い image」と
# 「digest 固定のはずの cloudflared が tag だけになっている」更新を止める。
package main

pod_spec := spec if {
	input.kind in {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}
	spec := input.spec.template.spec
} else := spec if {
	input.kind == "CronJob"
	spec := input.spec.jobTemplate.spec.template.spec
} else := spec if {
	input.kind == "Pod"
	spec := input.spec
}

containers contains c if some c in pod_spec.containers

containers contains c if some c in pod_spec.initContainers

# tag か digest のどちらかがあれば固定とみなす (暗黙の :latest を禁止する)
image_pinned(image) if contains(image, "@sha256:")

image_pinned(image) if {
	not contains(image, "@")
	parts := split(image, "/")
	contains(parts[count(parts) - 1], ":")
}

deny contains msg if {
	some c in containers
	not image_pinned(c.image)
	msg := sprintf("%s/%s: container %q の image %q に tag も digest がありません", [input.kind, input.metadata.name, c.name, c.image])
}

# cloudflared は renovate.json の pinDigests: true に合わせて digest 固定を必須にする
deny contains msg if {
	some c in containers
	regex.match(`^(docker\.io/)?cloudflare/cloudflared([:@]|$)`, c.image)
	not contains(c.image, "@sha256:")
	msg := sprintf("%s/%s: container %q の cloudflared image %q は digest (@sha256:) 固定が必要です", [input.kind, input.metadata.name, c.name, c.image])
}
