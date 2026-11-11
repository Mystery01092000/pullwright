# Set up Microsoft Teams

Teams reaches Pullwright through the **Bot Framework**. Teams POSTs each message to your control plane's messaging endpoint, and Pullwright replies through the Bot Connector API. That means the control plane needs a **public HTTPS URL** (in development, a tunnel). Pullwright speaks the Bot Framework REST protocol directly, so no Microsoft SDK is involved. Every inbound request's token is verified against the Bot Framework's signing keys, your app id and the conversation's service URL.

## 1. Register the bot in Azure

1. In the Azure portal, create an **Azure Bot** resource.
   - *Type of App:* **Single Tenant** (recommended for one organization) or Multi Tenant.
   - *Creation type:* create a new Microsoft App ID.
2. In the bot's **Configuration**:
   - Note the **Microsoft App ID** (and the tenant id for single-tenant bots).
   - Set the **Messaging endpoint** to `https://<your control plane>/api/teams/messages`.
   - Click *Manage Password* and create a **client secret**.
3. Under **Channels**, add **Microsoft Teams**.

```bash
PULLWRIGHT_TEAMS_APP_ID=00000000-0000-0000-0000-000000000000
PULLWRIGHT_TEAMS_APP_PASSWORD=<client secret>
PULLWRIGHT_TEAMS_TENANT_ID=<tenant id>                 # single-tenant bots only
PULLWRIGHT_TEAMS_REPOS={"19:abc...@thread.tacv2": "arcane-tech/api"}   # optional: default repo per channel
```

## 2. Package the Teams app

1. Copy [`deploy/teams/manifest.json`](../deploy/teams/manifest.json) and replace:
   - `${{PULLWRIGHT_TEAMS_APP_ID}}` with your App ID (in both `id` and `botId`);
   - `${{PULLWRIGHT_PUBLIC_HOST}}` with your control plane's host name.
2. Add two icons next to it: `color.png` (192×192) and `outline.png` (32×32, white on transparent).
3. Zip the three files (with no folder inside the zip). In Teams, go to **Apps → Manage your apps → Upload an app**, or have an admin publish it to the organization's app catalog.

## 3. Use it

Add the app to a team or chat, then mention it:

```
@Pullwright arcane-tech/web: add a dark-mode toggle to the settings page, persisted per user
```

- The first time, it replies with a link to connect your GitHub account.
- The plan arrives as an **Adaptive Card** with a feedback box and **Approve**, **Revise** and **Cancel** buttons. For Revise, type your feedback in the box first.
- The issue link and, later, the PR link are posted in the same conversation.

## Local development with a tunnel

```bash
devtunnel host -p 8765 --allow-anonymous    # or: ngrok http 8765
```

Set the bot's messaging endpoint to `https://<tunnel>/api/teams/messages` and `PULLWRIGHT_PUBLIC_URL` to the tunnel URL.

## Troubleshooting

- **401 in the server log:** the App ID doesn't match the bot, or the request didn't come from the Bot Framework. Check `PULLWRIGHT_TEAMS_APP_ID`.
- **Replies fail with 401 from the Bot Connector:** wrong client secret, or a single-tenant bot without `PULLWRIGHT_TEAMS_TENANT_ID`.
- **Mentions in channels don't arrive:** the app must be installed in the team, and the manifest's bot `scopes` must include `team`.
