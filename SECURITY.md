# Security

mia will hold your prompts and pictures, and take requests over your network. This page says how to
report a problem, and how mia is meant to limit the damage if something goes wrong.

## Reporting a problem

Please report security problems privately, through GitHub: on the repository's **Security** tab,
choose **Report a vulnerability**, or use
[this link](https://github.com/southsidedean/macos-ai-imagegen/security/advisories/new). Please don't
open a public issue.

## Supported versions

Until 1.0, only the newest release gets fixes.

## How it's meant to work

Working today, and tested:

- Model files can't run code: mia downloads safetensors weights and text files only, never code, and
  refuses a repository with no safetensors weights, with no override.
- Only `mia models pull` downloads. Making an image runs offline.
- Images are saved with no metadata, because the image engine would otherwise write your prompt and home
  folder into every one. Prompts are kept in mia's own history file, and stay out of its output.
- mia's folder and history file are readable only by you.
- The API listens only on this Mac, and refuses a request whose `Host` or `Origin` names anywhere else,
  which stops a web page from reaching it through DNS rebinding.
- Everything but a bare health check and the API's public description needs a token, and only in the
  `Authorization` header. It's checked before a request's body is read, and a test walks the app's routes to
  prove each needs one. Each device has its own, with its own permissions, stored only as a hash and shown once.
  You can revoke one without touching the others, and give one an end date.
- Requests and event streams are limited per device, as are the queue's length and a request's size. Refused
  requests are limited per address.
- A connection gets 15 seconds to deliver a whole request, and connections are capped per address and overall, so
  holding many open with no token doesn't use up the server.
- An answer that refuses a request doesn't repeat what was sent, and prompts stay out of the server's output.
- Deleting an image also deletes the job that made it. Finished jobs, which hold prompts, are forgotten after 30
  days.
- The API can read or delete image files only in mia's own images folder.
- The launchd service runs as you, listens only on this Mac, keeps its log private, and its file holds no token.
- The API process doesn't load the image engine, so a crash there can't take it down.
- MCP tools are calls to the API with the assistant's own token, so its checks apply to them too. No tool can delete an
  image or change the models, and `mia mcp` won't send a token over plain HTTP to another machine.

Known limits:

- All tokens share one queue and history, so give tokens only to devices you trust. mia is built for one person's
  or household's devices.
- Every process on your Mac shares one address, so one holding 64 connections or sending 30 bad tokens can make
  other programs on the Mac wait. It can't do that to other machines.
- A device with a token can hold a connection open by reading an answer very slowly, up to the connection caps.

Still to come, following the design in [PLAN.md](PLAN.md#security): remote access over HTTPS, in 0.4.
Until then, there's no way to turn on access from another machine.
