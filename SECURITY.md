# Security policy

Pullwright runs AI agents that read your code, execute commands on runners and open pull requests, so we treat security reports as a priority.

## Reporting a vulnerability

Report privately through [GitHub security advisories](https://github.com/Mystery01092000/pullwright/security/advisories/new). Do not open a public issue or pull request for a vulnerability.

Include what an attacker can do, the steps to reproduce, and the version or commit. You can expect an acknowledgement within three working days.

## In scope

- Escaping the runner's isolation, or reaching another job's data or tokens.
- Getting the control plane to run code from a repository it plans for. Planning is meant to be read-only.
- Bypassing approval: getting an issue opened or implemented without approval from a linked user with write access.
- Forging Slack, Teams or web requests, session cookies or account-link tokens.
- Leaking secrets (GitHub tokens, the Anthropic key, the app's private key) into logs, issues, PRs or prompts.

## The security model in short

- **Runners** are throwaway containers: non-root, with CPU, memory and PID limits, no-new-privileges, no host mounts, and removed on exit. They receive a GitHub installation token scoped to one repository that expires within an hour. Agents there cannot `git push` or call `gh`; the pipeline pushes and opens the PR itself.
- **Planning** runs on a read-only clone with read-only tools. Repository code never executes in the control plane.
- **Approval** requires a chat user linked to a GitHub account that has write access to the repository.
- **Teams** requests are verified against the Bot Framework signing keys, the app id and the service URL. **Slack** uses Socket Mode, so it exposes no inbound endpoint.
- **Prompt injection**: repository content and issue text can contain instructions aimed at the agents. The runner's sandbox, the scoped token and the human review of every PR are the controls. Treat a Pullwright PR like any external contribution and review it before merging.
