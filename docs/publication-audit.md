# Publication audit — 2026-11-17

**Disposition: historical cleanup required before publishing the existing history.** Current publication files contain no detected unrelated private context. Historical copies remain. This audit describes the pre-commit baseline. A new portfolio documentation commit is authorized for finalization; no history rewrites, remote mutations or pushes are authorized or performed.

## Scope and method

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

Existing ignore rules exclude environment files, credentials, local databases and sidecars, simulation inputs, monitoring runtime data, tools and private configuration. The Docker context uses an allowlist for application Python and the Dockerfile. Ignored local credentials, databases and runtime artifacts were preserved; they must remain outside publication bundles. The SVG banner is an authored graphic. No runtime screenshots were fabricated or included.

## Review before publication

1. Resolve historical content findings through an explicitly authorized cleanup approach, and decide whether commit identity metadata is appropriate for publication.
2. Review the complete working-tree diff, including untracked documentation. Choose a reuse license if desired; none has been inferred.
3. Review fresh [validation evidence](validation.md). The CI workflow is unchanged; the badge reports its hosted status dynamically.
4. The finalization request authorizes a local commit of reviewed documentation and the neutral validation-label cleanup using the existing Git identity. Push only after separate instruction.

## Finalization confirmation

Before the authorized commit, all 63 tracked/non-ignored publication files were rechecked for private-context and credential patterns, historical personal name/email text, and current local credential values. No matches were found. Public repository/profile links intentionally identify the portfolio owner. Local documentation links and banner XML passed validation. All 70 Python tests passed again; application source and the CI workflow match the baseline. The historical findings above remain separate and unresolved. Ignored private runtime files are outside the publication set and were preserved.
