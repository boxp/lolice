# METADATA
# title: ADR policy
# description: regomark が data.adr から deny/warn/questions を集める entrypoint。
# entrypoint: true
package adr

schema_version := 2

rule_ids := {
	"file-name",
	"title",
	"required-sections",
	"section-order",
	"status-paragraph",
	"status-value",
	"status-lines",
	"reference-placement",
	"reference-target",
	"reference-direction",
	"duplicate-number",
	"number-sequence",
	"replacement-cycle",
	"broken-link",
	"spec-detail",
	"procedure",
	"multiple-decisions",
}
