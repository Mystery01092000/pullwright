# Set up the GitHub App

For an organization, Pullwright authenticates as a GitHub App rather than as a person:
- every job gets an installation token that can touch **only the one repository** it is working on and **expires within an hour**;
- issues, comments and PRs are authored by the app (`pullwright[bot]`), not by an employee's account;
- access is granted per repository by installing the app.

## 1. Create the app

Go to GitHub → your organization → Settings → Developer settings → GitHub Apps → **New GitHub App**.

- **Name:** e.g. `Pullwright (Arcane Tech)`
- **Homepage URL:** any URL. GitHub only shows it on the app's page and never calls it, so the repository URL is fine, e.g. `https://github.com/Mystery01092000/pullwright`.
- **Callback URL** (under *Identifying and authorizing users*): leave it empty. The app acts on repositories; nobody logs in through it.
- **Webhook:** uncheck *Active*. Pullwright polls the queue, so it needs no webhook and no public URL.
- **Repository permissions:**

  | Permission | Access | Why |
  |---|---|---|
  | Contents | Read and write | clone, push the work branch |
  | Issues | Read and write | open queued issues, labels, comments |
  | Pull requests | Read and write | open PRs, request reviews |
  | Metadata | Read-only | required |

- **Where can this GitHub App be installed?** Only on this account.

Create it, note the **App ID**, and under *Private keys* click **Generate a private key**. Keep the downloaded `.pem` file as a secret.

## 2. Install it

On the app's page, click **Install App**, choose the organization, and select the repositories Pullwright may work on. You can add more later.

## 3. Configure Pullwright

```bash
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_FILE=/secrets/pullwright.pem   # or GITHUB_APP_PRIVATE_KEY="-----BEGIN ...-----\n..."
PULLWRIGHT_REPOS=arcane-tech/api,arcane-tech/web
```

Remove `GITHUB_TOKEN` if it was set. The app takes precedence when both are present.

## 4. The web login is a separate OAuth app

The GitHub App acts on repositories. A **GitHub OAuth App** (or the GitHub App's own user authorization) identifies the developer who logs in to the web UI. Create an OAuth App with callback URL `$PULLWRIGHT_PUBLIC_URL/auth/github/callback` and set `GITHUB_OAUTH_CLIENT_ID` and `GITHUB_OAUTH_CLIENT_SECRET`. It asks only for `read:user`.

## Who may approve

Approval is checked live: the approver must be linked to a GitHub account with **write**, **maintain** or **admin** permission on the repository. Repository permissions in GitHub are the access control. There is no separate user list to maintain.
