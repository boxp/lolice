package main

application(sources) := {
	"apiVersion": "argoproj.io/v1alpha1",
	"kind": "Application",
	"metadata": {"name": "app", "namespace": "argocd"},
	"spec": {"sources": sources},
}

test_exact_chart_version_allowed if {
	count(deny) == 0 with input as application([{"repoURL": "https://charts.example", "chart": "x", "targetRevision": "1.104.1"}])
}

test_v_prefixed_chart_version_allowed if {
	count(deny) == 0 with input as application([{"repoURL": "https://charts.longhorn.io/", "chart": "longhorn", "targetRevision": "v1.9.1"}])
}

test_chart_range_denied if {
	count(deny) == 1 with input as application([{"repoURL": "https://charts.example", "chart": "x", "targetRevision": "1.x"}])
}

test_chart_without_target_revision_denied if {
	count(deny) == 1 with input as application([{"repoURL": "https://charts.example", "chart": "x"}])
}

test_chart_star_denied if {
	count(deny) == 1 with input as application([{"repoURL": "https://charts.example", "chart": "x", "targetRevision": "*"}])
}

test_git_source_branch_allowed if {
	# git source (chart なし) は branch 指定で良い
	count(deny) == 0 with input as application([{"repoURL": "https://github.com/boxp/lolice", "path": "argoproj/x", "targetRevision": "main"}])
}

test_single_source_checked if {
	count(deny) == 1 with input as {
		"kind": "Application",
		"metadata": {"name": "single"},
		"spec": {"source": {"repoURL": "https://charts.example", "chart": "x", "targetRevision": "latest"}},
	}
}
