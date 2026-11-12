# Web UI design

The web UI follows the design in this folder, made on Claude Design and approved before the code was written.

| File | What it shows |
|---|---|
| `Main.dc.html` | A request thread: your messages, Pullwright's replies, the activity rails, the plan card, the approval card, a GitHub comment, the composer and the lifecycle panel |
| `NewRequest.dc.html` | The empty state: what to ask, the composer and example prompts |
| `Components.dc.html` | Tokens (colours, radii, type) and the shared pieces |
| `canvas.json` | The canvas layout the files above sit on |

## Principles

- **Dark, flat, rounded.** Near-black background (`#0a0a0a`), a slightly lighter sidebar, cards at `#141414`. No glass, no gradients. Radii of 8, 10, 14 and 18 px; pills are fully round.
- **Every message stands alone.** Each turn has its own header (who, when) and spacing of 28 px, so two replies never read as one.
- **Show the work, folded.** Agent tool calls are a rail (`ThinkingState`) summarised in one line ("read 6 files · ran 3 commands"); open it to see each call.
- **Motion says what's happening.** A `thinking-orbs` orb marks the live state: searching while it plans, composing while it answers, solving while it verifies, breathing while it waits for your approval, connecting while it waits for a runner, working while the runner codes, and weaving while the checks run. Status text shimmers, new messages fade in, the newest reply streams in, and reduced-motion settings stop all of it.

## Where it lives in code

| Piece | File |
|---|---|
| Tokens, keyframes | `web/src/index.css` |
| Thread, plan, approval, lifecycle | `web/src/Thread.tsx` |
| Sidebar, new request, queue, login | `web/src/Requests.tsx` |
| Orb, toasts, input, link shortening | `web/src/shared.tsx` |
| Activity rail, markdown, streaming text | `web/src/components/ui/ai-thinking-and-tool-calls.tsx` |
| Approval card, button | `web/src/components/ui/approval-card.tsx`, `button.tsx` |
