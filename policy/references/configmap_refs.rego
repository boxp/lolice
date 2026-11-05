# ConfigMap 参照の解決に関する policy (BOXP-364)
#
# `conftest test --combine` で Application 1 つ分の render (1 ファイル) をまとめて受け取り、
# workload の ConfigMap 参照が同じ render・同じ namespace の ConfigMap に解決するかを検査する。
# input は [{path, contents}] で、multi-document の YAML は document ごとに 1 要素になる。
#
# 再発防止の対象: kustomization.yaml に namespace が無く、configMapGenerator の生成 ConfigMap
# (namespace 無し) と namespace 付きの Deployment が name reference で結び付かず、
# Deployment が hash 無しの名前を参照したまま render された (codex-workspace / ai-ops-metrics-app)。
package main

# cluster が全 namespace に自動作成する ConfigMap。render には現れないので解決不要とする。
# この repo の source path の render で未解決の参照はこれだけ (BOXP-364 時点で全件確認済み)。
external_configmaps := {"kube-root-ca.crt"}

# volume に必須のキー (command が直接読むファイル)。items 指定が無い volume は全キーを展開するため、
# キー欠落は Pod 起動後まで見えない。
required_volume_keys := [{
	"kind": "Deployment",
	"namespace": "codex-workspace",
	"name": "codex-workspace",
	"volume": "ai-ops-metrics-app",
	"key": "server.py",
}]

workload_kinds := {"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"}

pod_spec(d) := d.spec.template.spec if d.kind in workload_kinds

pod_spec(d) := d.spec.jobTemplate.spec.template.spec if d.kind == "CronJob"

pod_spec(d) := d.spec if d.kind == "Pod"

# namespace の無い resource は区別できるよう空文字として扱う (kustomize もそのまま出力する)。
namespace_of(d) := object.get(d.metadata, "namespace", "")

containers(p) := array.concat(object.get(p, "containers", []), object.get(p, "initContainers", []))

volume_refs(p) := {v.configMap | some v in object.get(p, "volumes", []); v.configMap}

projected_refs(p) := {s.configMap |
	some v in object.get(p, "volumes", [])
	some s in object.get(object.get(v, "projected", {}), "sources", [])
	s.configMap
}

env_from_refs(p) := {e.configMapRef | some c in containers(p); some e in object.get(c, "envFrom", []); e.configMapRef}

env_refs(p) := {e.valueFrom.configMapKeyRef | some c in containers(p); some e in object.get(c, "env", []); e.valueFrom.configMapKeyRef}

configmap_refs(p) := ((volume_refs(p) | projected_refs(p)) | env_from_refs(p)) | env_refs(p)

referenced_keys(r) := {k | k := r.key} | {item.key | some item in object.get(r, "items", [])}

# 検査対象の参照: optional でも外部管理でもないもの
required_refs contains [entry, r] if {
	some entry in input
	some r in configmap_refs(pod_spec(entry.contents))
	not object.get(r, "optional", false)
	not r.name in external_configmaps
}

configmap(path, ns, name) := cm if {
	some entry in input
	entry.path == path
	cm := entry.contents
	cm.kind == "ConfigMap"
	namespace_of(cm) == ns
	cm.metadata.name == name
}

has_key(cm, key) if key in object.keys(object.get(cm, "data", {}))

has_key(cm, key) if key in object.keys(object.get(cm, "binaryData", {}))

label(entry) := sprintf("%s: %s/%s/%s", [entry.path, entry.contents.kind, namespace_of(entry.contents), entry.contents.metadata.name])

deny contains msg if {
	some [entry, r] in required_refs
	not configmap(entry.path, namespace_of(entry.contents), r.name)
	msg := sprintf("%s: ConfigMap %q が同じ render・同じ namespace にありません", [label(entry), r.name])
}

# 今回の障害パターン: "<参照名>-<hash>" の生成 ConfigMap があるのに参照が hash 無しのまま
deny contains msg if {
	some [entry, r] in required_refs
	not configmap(entry.path, namespace_of(entry.contents), r.name)
	some other in input
	other.path == entry.path
	other.contents.kind == "ConfigMap"
	regex.match(sprintf("^%s-[a-z0-9]{10}$", [regex.replace(r.name, `\.`, `\.`)]), other.contents.metadata.name)
	msg := sprintf(
		"%s: ConfigMap 参照 %q が configMapGenerator の生成名 %q (namespace %q) に置換されていません。kustomization.yaml の namespace 指定漏れを疑ってください",
		[label(entry), r.name, other.contents.metadata.name, namespace_of(other.contents)],
	)
}

deny contains msg if {
	some [entry, r] in required_refs
	cm := configmap(entry.path, namespace_of(entry.contents), r.name)
	some key in referenced_keys(r)
	not has_key(cm, key)
	msg := sprintf("%s: ConfigMap %q にキー %q がありません", [label(entry), r.name, key])
}

deny contains msg if {
	some contract in required_volume_keys
	some entry in input
	d := entry.contents
	d.kind == contract.kind
	namespace_of(d) == contract.namespace
	d.metadata.name == contract.name
	not volume_has_key(entry.path, d, contract.volume, contract.key)
	msg := sprintf("%s: volume %q の ConfigMap に必須キー %q がありません", [label(entry), contract.volume, contract.key])
}

volume_has_key(path, d, volume, key) if {
	some v in pod_spec(d).volumes
	v.name == volume
	has_key(configmap(path, namespace_of(d), v.configMap.name), key)
}
