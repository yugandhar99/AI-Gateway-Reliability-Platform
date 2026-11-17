# Publication privacy audit

The v2.0.0 publication tree removes private workspace paths and presents the platform as an independent engineering portfolio project. The existing Git history still contains private material. Review the findings before publishing the branch or distributing a clone.

## Scope

The review started from `16d19bb` on local `master`. It examined all publication files, historical paths across all local refs, and 211 local objects at baseline, including a pre-existing dangling blob. All 12 reachable commits were included. Checks covered absolute personal workspace paths, prior private-context findings, common AWS, GitHub and provider credential formats, private-key headers, selected local credential values, and credential or database filenames. Commit identity metadata was inspected without reproducing personal values. Git integrity checks passed.

No tested credential patterns or compared local credential values matched in the publication files or historical content. There were no historical credential or database path candidates and no tracked files matching ignore rules. These are bounded checks. Remote copies, hidden refs, forks, caches, and objects absent from this clone were outside the audit.

## Historical findings

| Finding | Location | Status |
|---|---|---|
| Private workspace paths | Seven historical README blobs across `c4f9e4a` through `e56ed19` | Absent from the current README, present in history |
| Additional private checkout paths | `docs/runbooks/inference.md` and `docs/validation.md` as of `e139b8d` through `16d19bb` | Removed from the publication tree, present in history |
| Unrelated private working context | README at `6db4ad0` and `e56ed19` | Absent from the current README, present in history |
| Unrelated scenario label | `scripts/validate_infra.py` from `3001dc9` through `e56ed19` | Current label is `custom-config` |
| Personal author and committer metadata | Eleven baseline commits | Existing identity remains in history. Review whether this is intended public attribution. |

The audit deliberately omits sensitive values. The publication commit leaves prior history intact. Any cleanup of historical or already-public copies needs a separate decision.
