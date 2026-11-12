---
name: commit-messages
description: How we write commit messages and PR titles (conventional commits)
---
Use conventional commits: `type(scope): summary`, where type is one of feat, fix, docs, refactor, test, chore.

- The summary is imperative, lower case, at most 72 characters, no trailing period.
- The body says why the change was needed, not what the diff shows.
- Reference the issue as `Refs #123` in the body.
