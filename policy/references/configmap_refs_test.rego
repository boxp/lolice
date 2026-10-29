package main

cm(name, ns, entries) := {"kind": "ConfigMap", "metadata": {"name": name, "namespace": ns}, "data": entries}

deploy(ns, ref) := {
	"kind": "Deployment",
	"metadata": {"name": "codex-workspace", "namespace": ns},
	"spec": {"template": {"spec": {"volumes": [{"name": "ai-ops-metrics-app", "configMap": ref}]}}},
}

render(docs) := [{"path": "render.yaml", "contents": d} | some d in docs]

has_message(messages, fragment) if {
	some m in messages
	contains(m, fragment)
}

# 修正後 (7a0d44d): 生成 ConfigMap と Deployment が同じ namespace で、参照が hash 付きに置換されている
test_fixed_render_passes if {
	count(deny) == 0 with input as render([
		deploy("codex-workspace", {"name": "codex-workspace-ai-ops-metrics-9mt6ckmbbt"}),
		cm("codex-workspace-ai-ops-metrics-9mt6ckmbbt", "codex-workspace", {"server.py": "x"}),
	])
}

# 修正前 (6332922): 生成 ConfigMap に namespace が無く、参照が hash 無しのまま
test_namespace_missing_render_fails if {
	messages := deny with input as render([
		deploy("codex-workspace", {"name": "codex-workspace-ai-ops-metrics"}),
		{"kind": "ConfigMap", "metadata": {"name": "codex-workspace-ai-ops-metrics-abcde12345"}, "data": {"server.py": "x"}},
	])
	has_message(messages, "configMapGenerator の生成名")
	has_message(messages, "同じ render・同じ namespace にありません")
}

test_missing_configmap_fails if {
	messages := deny with input as render([deploy("ns", {"name": "missing"}), cm("x", "ns", {})])
	has_message(messages, `"missing" が同じ render・同じ namespace にありません`)
}

test_other_namespace_fails if {
	messages := deny with input as render([deploy("ns", {"name": "x"}), cm("x", "other", {})])
	has_message(messages, `"x" が同じ render・同じ namespace にありません`)
}

test_required_volume_key_missing_fails if {
	messages := deny with input as render([
		deploy("codex-workspace", {"name": "x"}),
		cm("x", "codex-workspace", {"other.py": "x"}),
	])
	has_message(messages, `必須キー "server.py"`)
}

test_optional_reference_is_ignored if {
	count(deny) == 0 with input as render([deploy("ns", {"name": "missing", "optional": true})])
}

test_external_configmap_is_ignored if {
	count(deny) == 0 with input as render([deploy("ns", {"name": "kube-root-ca.crt"})])
}

test_unrelated_hash_like_name_does_not_claim_generator if {
	messages := deny with input as render([deploy("ns", {"name": "app"}), cm("app-v2-abcde12345", "ns", {})])
	not has_message(messages, "configMapGenerator の生成名")
}

test_configmap_key_ref_missing_key_fails if {
	pod := {"kind": "Pod", "metadata": {"name": "p", "namespace": "ns"}, "spec": {"containers": [{"env": [{"valueFrom": {"configMapKeyRef": {"name": "x", "key": "missing"}}}]}]}}
	messages := deny with input as render([pod, cm("x", "ns", {"present": "1"})])
	has_message(messages, `キー "missing" がありません`)
}

test_projected_items_missing_key_fails if {
	job := {"kind": "Job", "metadata": {"name": "j", "namespace": "ns"}, "spec": {"template": {"spec": {"volumes": [{"name": "v", "projected": {"sources": [{"configMap": {"name": "x", "items": [{"key": "missing", "path": "m"}]}}]}}]}}}}
	messages := deny with input as render([job, cm("x", "ns", {})])
	has_message(messages, `キー "missing" がありません`)
}

test_init_container_env_from_missing_fails if {
	cron := {"kind": "CronJob", "metadata": {"name": "c", "namespace": "ns"}, "spec": {"jobTemplate": {"spec": {"template": {"spec": {"initContainers": [{"envFrom": [{"configMapRef": {"name": "missing"}}]}]}}}}}}
	messages := deny with input as render([cron])
	has_message(messages, `"missing" が同じ render・同じ namespace にありません`)
}

test_binary_data_key_resolves if {
	ref := {"name": "x", "items": [{"key": "bin", "path": "bin"}]}
	binary := {"kind": "ConfigMap", "metadata": {"name": "x", "namespace": "ns"}, "binaryData": {"bin": "eA=="}}
	count(deny) == 0 with input as render([deploy("ns", ref), binary])
}
