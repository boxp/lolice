# Accepted の ADR が指す repo 内リンクの実在を検査する。
package adr

import data.adrdoc

link_problem := {
	"missing": "repo 内に存在しません",
	"outside": "repo の外を指しています",
}

deny contains {
	"rule": "broken-link",
	"path": document.path,
	"line": link.line,
	"message": sprintf("リンク %q は%s", [link.dest, link_problem[link.kind]]),
} if {
	some document in input.documents
	adrdoc.is_accepted(document)
	some link in document.links
	link_problem[link.kind]
}
