---
description: How to cut a release of this service (version bump, changelog, tag)
---
# Releasing

1. Bump `version` in `internal/version/version.go`.
2. Add a section to `CHANGELOG.md` under the new version with the merged PR titles.
3. Run `make test`.
4. Tag: `git tag -a vX.Y.Z -m "vX.Y.Z"`. Do not push the tag; the user does that.
