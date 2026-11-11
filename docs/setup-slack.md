# Set up Slack

Pullwright connects to Slack over **Socket Mode**: it opens an outbound WebSocket, so the control plane needs no public URL and no inbound firewall rule.

## 1. Create the app from the manifest

1. Go to https://api.slack.com/apps → **Create New App** → **From a manifest**, and pick your workspace.
2. Paste [`deploy/slack-manifest.yml`](../deploy/slack-manifest.yml). It requests these scopes:
   - `app_mentions:read`, `chat:write`, `users:read`, `im:history` and `im:read`;
   - the `app_mention` and `message.im` events;
   - interactivity (for the buttons and the revise modal);
   - Socket Mode.
3. Create the app, then **Install to Workspace**.

## 2. Tokens

- **Bot token:** OAuth & Permissions → *Bot User OAuth Token* (`xoxb-...`) → `SLACK_BOT_TOKEN`.
- **App token:** Basic Information → *App-Level Tokens* → **Generate Token and Scopes** with the `connections:write` scope (`xapp-...`) → `SLACK_APP_TOKEN`.

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
PULLWRIGHT_SLACK_REPOS={"C0123ABCD": "arcane-tech/api"}   # optional: default repo per channel
```

Restart `pullwright-serve`. The log line `pullwright up: channels=['slack', 'web']` confirms it connected.

## 3. Use it

Invite the bot to a channel (`/invite @pullwright`), then:

```
@pullwright arcane-tech/api: return 404 instead of 500 when an order id does not exist
```

The first time, the bot replies with a link to connect your GitHub account (it expires in an hour). After that:

- the bot drafts a plan in the thread with **Approve**, **Revise** and **Cancel** buttons;
- **Revise** opens a box for your feedback, and a new plan follows in the thread;
- **Approve** opens the GitHub issue and posts its link;
- the PR link is posted in the same thread when the runner finishes.

You can send several tasks; each mention is its own request. In a channel mapped with `PULLWRIGHT_SLACK_REPOS`, you can leave out the repository. Direct messages to the bot work too.

`@pullwright help` and `@pullwright link` are answered directly.

## Troubleshooting

- **No reaction to a mention:** check that the bot is in the channel and that the server log shows Slack connected. Socket Mode needs outbound HTTPS to `wss-primary.slack.com`.
- **Buttons do nothing:** interactivity must be enabled (the manifest does this).
- **"Only people with write access can approve":** the clicking user's linked GitHub account needs write access to the repository.
