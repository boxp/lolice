# boxp-langfuse-observer: threat model and accepted residual (BOXP-428 TD-07)

This file records the observation contract the Owner approved on 2026-11-06 (Owner: "その方針にしましょう").
It is the review boundary for this plugin: a finding is judged against it. The source of truth for the
contract is the vault note `Projects/langfuse-observability/request.md` (top section).

## Deployment boundary

- Langfuse runs inside the lolice cluster (`argoproj/langfuse`). Bodies are sent only to
  `langfuse-web.langfuse.svc.cluster.local`, never to an external SaaS or an external LLM.
- The UI is reachable only through Cloudflare Access (Owner-only policy) and Langfuse OIDC; sign-up and
  password login are disabled. Ingestion uses a project API key read from an optional Secret volume.
- Retention: traces older than 14 days are deleted by the `langfuse-retention` CronJob, raw events in
  MinIO expire after 1 day. Nothing is written to disk by this plugin (no spool).

## Goal: readable bodies (positive visibility)

The Owner must be able to read, in the UI, the prompts, replies, tool input/output and delegation bodies
of the selected paths (Kohako G2 sessions, webhook/Task Board sessions, delegation and ticket-referring
turns). The following must stay readable and are covered by positive tests (unit and Chainsaw):

- an ordinary tool call (read_file, terminal, MCP, ...) and its output, and every later turn of the
  session, its parent and its children;
- a request history this process did not observe (after a restart or compaction). It is scanned by
  author: user/assistant/system/developer text is scanned like a live reply (an ordinary sentence such
  as "Your password is too short." pins words but does not withhold), while tool results, tool call
  arguments and unknown shapes keep the tool-output scan;
- text that mentions a secret word without a value ("Reset your password", "パスワードを設定してください",
  "if token is None:", "max_output_tokens: 4000").

Withholding a whole session only because a tool ran, a secret word was mentioned or history was
unobserved is a regression, not a protection.

## Protected (must not be stored or sent)

1. Known secret sources, not collected at all: credential files (`auth.json`, `.credentials*`, `.env*`,
   `.ssh/`, `.aws/`, kubeconfig and `~/.kube/config`, `.git-credentials`, `.netrc`, private key files, `secrets/`,
   token files, and the well-known CLI credential files shared with the Task Board exporter: `.npmrc`, `.pypirc`,
   `.pgpass`, `.my.cnf`, `.docker/config.json`, `.boto`, `.s3cfg`, `.vault-token`, `.terraformrc`, gh `hosts.yml`,
   `.config/gcloud`, `.azure`, `.cargo`/`.gem` credentials, `.htpasswd`, ...),
   environment dumps (`env`, `printenv`, `export -p`, `declare -x`, `process.env`, output with `PATH=` and
   `HOME=`), cluster secrets (`kubectl ... secret`, `kubectl create token`), cloud/CLI credential commands
   (`gcloud auth print-access-token`/`print-identity-token`/`config config-helper`/`secrets versions access`,
   `az account get-access-token`/`keyvault secret show`/`ad sp create-for-rbac`, `aws sts`/`ecr get-login-password`/
   `ssm get-parameter`/`secretsmanager get-*`, `gh auth token`, `vault kv get`/`read`, `op read`, `bw get`,
   `--show-token`, `docker-credential-*`). A credential command not in this list is a known gap (its output is
   still masked by known formats and secret words). The call is sent
   without its body and the later bodies of the session lineage are withheld (the model has seen it).
   The check runs on the model reply that asks for the call too (its `tool_calls` arguments, e.g. writing
   `.env`), because that reply is kept before the tool hook fires. Withholding and pinned values go to the
   session and every recorded ancestor (parent, grandparent, ...); a session reads them from its whole
   lineage, so a running child and a sibling see them as well. Known over-withholding: a long-lived G2
   session is withheld once any descendant touches a known source.
2. Known formats, masked in every field before the batch is queued: private keys, Authorization /
   Cookie headers, URL credentials, JWT, provider keys (sk-, ghp_, xox*, AKIA, AIza, ...), bearer tokens,
   secret-named assignments / flags / env entries / XML elements / YAML blocks, data URIs, high-entropy
   strings, values of this process's environment.
3. Values pinned from 2.: a value identified next to a secret name (assignment, element, prose with a
   separator or a copula, e.g. "the password is X", "X is my password") is masked in every later field of
   the session lineage, including its JSON/HTML/percent escapes, Base64/base64url/hex and reversed forms
   (reversed at every length: a 2-3 character value pinned from a reply is matched reversed as a word of its own;
   a short environment value, reversed or not, matches anywhere).
   Every Base64/base64url/hex-looking run is also decoded (4 alignments for Base64, 2 for hex); when the
   decoded text reads as text and contains a pinned or environment value anywhere ("xqzvy" -> `eHF6dnk=`
   for a pinned `qzv`), the whole run is masked. Known over-masking: such an encoded run is blanked even
   when the value is only part of a longer decoded word.
   Text the model or the user writes (the user request, each model reply, the final reply, a delegated
   child's summary) is scanned like tool output before it is kept, so "password: X ... X" inside one reply
   masks both occurrences. For these bodies only assignments/known formats can withhold; a secret word in
   prose pins what it can but does not withhold a reply (a mention is not evidence). A value such prose names
   with a separator is pinned even when it is short or non-ASCII ("the password is qzv" pins `qzv` as a word
   of its own; "パスワードは 秘密のもじれつ" pins the value and its parts split at の), so its bare repeat is
   masked; only a 1-character value, ASCII or not, or a 1-character part split at の ("パスワードは あ") withholds (`short_value`). Known over-masking: the words after a secret
   word in a reply ("Your password is too short." pins `too`/`short`) are masked in later fields of the
   session lineage; the session stays readable.
4. Fail-closed: when a protected field cannot be judged (too large to scan, scan error, a value too short
   or not delimitable to pin, too many values), that observation's body is withheld with a non-body reason
   (`withheld_reason`) and the agent keeps working. A final scan of the serialized batch drops a span that
   still matches. There is no fail-open path that sends an unprocessed body.
   A withheld tool call keeps no text of its input in metadata either: no path, program or argument name,
   only the argument count and size, because after an exposure the model can pass the value on as a path.
   The per-session state (withholding reason, pinned values, parent link) is kept for the 1024 most recently
   active sessions. An entry evicted while it still mattered is recorded in a fixed 1 MiB Bloom filter that is
   not cleared while the process runs, and the session and its descendants are then withheld
   (`state_evicted`) instead of being released. A false positive only withholds an unrelated session. Known
   over-withholding: a session idle while 1024 others were active, or a delegated child whose parent link was
   evicted, is withheld when it resumes.
   The hooks track this state from the first turn even while the ingest key is absent; only the export is
   skipped (`turns_skipped_no_key`). A session that read a secret source before the key appeared therefore
   stays withheld/masked after it appears.

5. Masking happens before the queue: when a turn ends, the session-end hook masks every text field (2. and 3.)
   and runs the final guard (4.), then queues only the masked turn and its guarded OTLP bodies. The raw tool
   results, replies and the pinned values stay in the hook-side turn buffer and are never copied into the queue,
   so whatever the worker holds, retries or drops is already masked. Non-text span fields are copied by an
   allow-list: the span key (which carries hook-supplied tool_call_id / api_request_id / child session ids) is
   replaced by its sha256 digest, usage keeps only the known numeric counters, type/level/timestamps are
   normalised, and ticket ids must match BOXP-N and be left unchanged by the masking (a pinned or environment
   value shaped like BOXP-N is dropped from the tickets/tags instead of copied). If masking raises, nothing is queued
   (`turns_dropped_mask_error`) and the agent continues. A full queue is checked first and costs no masking
   work. Cost: the masking work that used to run in the worker now runs in the session-end hook (measured
   about 0.1 s for an ordinary turn, 0.8 s for ten 30 KB tool outputs, a few seconds for a turn near the
   4 MiB turn limit); the tool hooks already scan the same text synchronously.
   The value matchers are cached in one shared slot that hooks of concurrent sessions replace; each call reads
   the slot once and returns the matcher built for its own values, so another session's matcher is never used.

A regression in 1-5 (a known source or format reaching the sink or the queue) is a blocking defect.

## Accepted residual (Owner-approved, not a defect of this plugin)

The Owner accepted, for this self-hosted, Owner-only, short-retention deployment, that the following can
reach Langfuse. They must not be "fixed" by withholding bodies broadly or by adding an endless list of
phrasings and encodings:

- a secret with no known format and no secret name next to it (an unmarked plain-text value), and the
  model repeating it alone;
- a value named in free prose in a way the patterns above do not delimit (a value far from the secret
  word, inside a longer sentence, in a non-separator Japanese phrasing such as "Xがパスワードです");
- transformations other than those listed in 3. (base32, rot13, case changes, splitting, paraphrase,
  translation);
- text the Owner or the system prompt writes directly, when it carries no known format or secret name.

The residual does not extend to: collecting credentials on purpose, sending fields unprocessed, disabling
masking, or sending bodies outside the cluster.
