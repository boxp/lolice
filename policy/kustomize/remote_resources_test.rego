package main

kustomization(resources) := {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": resources}

test_pinned_tag_allowed if {
	count(deny) == 0 with input as kustomization(["github.com/argoproj/argo-cd/manifests/base?ref=v3.5.6", "application.yaml"])
}

test_unversioned_tag_allowed if {
	count(deny) == 0 with input as kustomization(["github.com/example/repo/config?ref=1.2.3"])
}

test_missing_ref_denied if {
	count(deny) == 1 with input as kustomization(["github.com/argoproj/argo-cd/manifests/base"])
}

test_branch_ref_denied if {
	count(deny) == 1 with input as kustomization(["github.com/prometheus-operator/kube-prometheus?ref=main"])
}

test_https_form_checked if {
	count(deny) == 1 with input as kustomization(["https://github.com/example/repo?ref=HEAD"])
}

test_local_resources_ignored if {
	count(deny) == 0 with input as kustomization(["deployment.yaml", "../base"])
}
