package main

deployment(image) := {
	"kind": "Deployment",
	"metadata": {"name": "app"},
	"spec": {"template": {"spec": {"containers": [{"name": "main", "image": image}]}}},
}

test_tagged_image_allowed if {
	count(deny) == 0 with input as deployment("docker.io/library/nginx:1.27.0")
}

test_digest_only_image_allowed if {
	count(deny) == 0 with input as deployment("ghcr.io/boxp/arch/codex-workspace@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
}

test_untagged_image_denied if {
	count(deny) == 1 with input as deployment("docker.io/library/nginx")
}

test_registry_port_without_tag_denied if {
	# registry:5000/image は ":" が registry 側にしか無いので tag なし扱い
	count(deny) == 1 with input as deployment("registry.local:5000/app")
}

test_init_container_checked if {
	count(deny) == 1 with input as {
		"kind": "StatefulSet",
		"metadata": {"name": "db"},
		"spec": {"template": {"spec": {
			"initContainers": [{"name": "init", "image": "busybox"}],
			"containers": [{"name": "main", "image": "postgres:16"}],
		}}},
	}
}

test_cronjob_checked if {
	count(deny) == 1 with input as {
		"kind": "CronJob",
		"metadata": {"name": "job"},
		"spec": {"jobTemplate": {"spec": {"template": {"spec": {"containers": [{"name": "c", "image": "alpine"}]}}}}},
	}
}

test_cloudflared_with_tag_and_digest_allowed if {
	count(deny) == 0 with input as deployment("docker.io/cloudflare/cloudflared:2026.10.3@sha256:faa8e0a1475f137c910be6025f43c8e4092fdd35461b31e19fd1390657a1ff39")
}

test_cloudflared_tag_only_denied if {
	count(deny) == 1 with input as deployment("docker.io/cloudflare/cloudflared:2026.10.3")
}

test_cloudflared_short_name_tag_only_denied if {
	count(deny) == 1 with input as deployment("cloudflare/cloudflared:2026.10.3")
}

test_non_workload_kind_ignored if {
	count(deny) == 0 with input as {"kind": "ConfigMap", "metadata": {"name": "cm"}, "data": {"image": "x"}}
}
