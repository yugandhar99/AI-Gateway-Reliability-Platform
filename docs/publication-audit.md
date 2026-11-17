# Publication audit — 2026-11-17

**Disposition: historical cleanup required before publishing the existing history.** Current publication files contain no detected unrelated private context. Historical copies remain. This audit describes the pre-commit baseline. A new portfolio documentation commit is authorized for finalization; no history rewrites, remote mutations or pushes are authorized or performed.

## Preceding review: scope and method

Baseline: `e56ed19` on `master`. Reviewed all **eight reachable commits**, historical paths across all local refs, **133 local Git objects** enumerated by `git cat-file --batch-all-objects`, and **63 tracked/non-ignored publication files**, including the existing draft. Object review includes locally available unreachable objects; it cannot cover objects absent from this clone, remote caches, forks, hidden remote refs or deleted remote content. `git fsck --full` passed. The configured origin points to the repository linked in the README; no new remote-ref enumeration was performed in this review.

Read-only checks searched historical blob, commit and tag content for absolute home paths, unrelated private context, AWS access-key IDs, GitHub/provider-style tokens and private-key headers. Current files were checked for private-context patterns, excluding the audit's redacted descriptions and intentional ignore safeguards. Local credential file values were compared in memory against historical objects without printing values. Historical file paths were checked for credential files and databases. Commit identity metadata was reviewed without reproducing personal values here.

No matches to the tested credential patterns or current local credential values were detected. No historical credential/database path candidates were found. These bounded checks are not an entropy-based secret scan, dependency vulnerability scan or proof that every secret is absent.

## Historical findings requiring review

| Finding | Location | Required disposition |
|---|---|---|
| Absolute private workspace paths | README versions across all eight commits, `c4f9e4a` through `e56ed19`; seven distinct blobs | Remove historical private paths before public distribution |
| Unrelated private working context | README in `6db4ad0` and `e56ed19` | Remove historical context before public distribution |
| Unrelated validation scenario label | `scripts/validate_infra.py`, introduced in `3001dc9` and retained through baseline `e56ed19` | Current draft uses `custom-config`; older versions require cleanup |
| Personal name/email in author and committer metadata | All eight commits; one distinct identity | Owner should decide whether this is intended public attribution |

Sensitive text is deliberately omitted. Inspect `git log --all --format=fuller`, historical README versions, and the historical validator privately. A new documentation commit cannot hide earlier versions. History cleanup needs separate explicit authorization; none was performed. Review any already-public copies independently because this local audit cannot establish their removal.

## Current publication files

The portfolio README replaces lengthy working notes with focused engineering documentation in `docs/`. Current context-pattern checks found no candidates outside redacted audit descriptions and intentional ignore rules. The pre-existing one-line validator change renames a scenario without changing application behavior. Application source and the CI workflow remain unchanged.

Existing ignore rules exclude environment files, credentials, local databases and sidecars, simulation inputs, monitoring runtime data, tools and private configuration. The Docker context uses an allowlist for application Python and the Dockerfile. Ignored local credentials, databases and runtime artifacts were preserved; they must remain outside publication bundles. The SVG banner is an authored graphic. No runtime screenshots were included in that preceding review; the finalization below adds genuine captures.

## Review before publication

1. Resolve historical content findings through an explicitly authorized cleanup approach, and decide whether commit identity metadata is appropriate for publication.
2. Review the complete working-tree diff, including untracked documentation. Choose a reuse license if desired; none has been inferred.
3. Review fresh [validation evidence](validation.md). The CI workflow is unchanged; the badge reports its hosted status dynamically.
4. The finalization request authorizes a local commit of reviewed documentation and the neutral validation-label cleanup using the existing Git identity. Push only after separate instruction.

## Preceding documentation commit confirmation

Before the authorized commit, all 63 tracked/non-ignored publication files were rechecked for private-context and credential patterns, historical personal name/email text, and current local credential values. No matches were found. Public repository/profile links intentionally identify the portfolio owner. Local documentation links and banner XML passed validation. All 70 Python tests passed again; application source and the CI workflow match the baseline. The historical findings above remain separate and unresolved. Ignored private runtime files are outside the publication set and were preserved.

## Screenshot finalization review — 2026-11-17

Baseline: `ab4ada0` on `master`, **nine reachable commits** and **155 local Git objects** before the new commit. All **73 tracked and non-ignored publication files** were scanned, including the new capture automation and six images; historical blob, commit and tag contents were checked for credential patterns and exact current local credential values without printing them. Current files were also checked against commit name/email text and absolute home-path patterns. A private workspace path in the draft screenshot instructions was removed. No tested credential patterns or known local secret values matched current files or historical objects. No historical credential/database file paths were detected. One dangling blob exists; it was included in the object scan and preserved.

The seven historical blobs with private home paths remain. The preceding audit's unrelated working context and validator scenario label also remain in older commits. Author and committer name/email remain in all nine baseline commits and will also be present in the requested new commit using the existing Git identity. These are unresolved publication concerns; documentation edits cannot remove them. The scan covers local objects, not remote caches, forks, hidden refs or unavailable objects, and is not proof that every possible secret is absent.

All local documentation file links and heading anchors resolve. Public linked pages and all seven badge URLs returned HTTP 200; the live Actions badge reported passing for `master`. All five Mermaid diagrams rendered in Chromium. Visual inspection prompted shorter recovery-state labels. The application and CI workflow are unchanged. Fresh Python, container, infrastructure, alert fixture and live integration results are recorded in [validation evidence](validation.md).

The screenshot gallery is generated from actual running local services with synthetic traffic. The Docker image is explicitly a rendered report of selected real container fields, rather than a Docker Desktop screenshot. The capture manifest records source revision, dirty-tree status, scenario, container state and image digests. The browser uses ephemeral authentication and a fresh unauthenticated context for public GitHub. Credentials, browser state, raw Docker configuration and runtime databases are excluded. Local-only service names, loopback/internal container addresses, the demo admin username and the intentionally public GitHub owner are expected operational context.

No history rewrite or push is authorized by this finalization request or performed. Historical cleanup and any review of already-public copies require separate work.

Final image review covered all **six PNGs** at native resolution. Labels and statuses are readable, all twelve dashboard panels contain data, and no credentials, private home paths, private identity text, cookies or authentication dialogs are visible. PNGs have no text or EXIF metadata; every SHA-256 digest matches the manifest, and every gallery image link resolves. The final capture reuses the earlier synthetic workload and records that fact. The public successful run belongs to `e56ed19`; it has a Node.js 20 deprecation annotation for the existing pinned actions. This is a remaining maintenance item, with no demonstrated credential exposure.
