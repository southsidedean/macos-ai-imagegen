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
- The launchd service runs as you, keeps its log private, and its file holds no token.
- mia listens only on this Mac until you run `mia remote on`. Then it adds HTTPS for your own network, using TLS 1.3 and a
  certificate authority of its own, and drops connections from anything but this Mac and the private address ranges. Plain
  HTTP stays on this Mac. A handshake has 5 seconds, with at most 16 in progress from one address and 64 overall.
- The authority's certificate limits it to `.local` names and the names you chose, so its stolen key couldn't pose as
  another site's name. Its files are readable only by you.
- The API process doesn't load the image engine, so a crash there can't take it down.
- MCP tools are calls to the API with the assistant's own token, so its checks apply to them too. No tool can delete an
  image or change the models, and `mia mcp` won't send a token over plain HTTP to another machine.

Known limits:

- All tokens share one queue and history, so give tokens only to devices you trust. mia is built for one person's
  or household's devices.
- Every process on your Mac shares one address, so one holding 64 connections or sending 30 bad tokens can make
  other programs on the Mac wait. It can't do that to other machines.
- A device with a token can hold a connection open by reading an answer very slowly, up to the connection caps.
- The authority can't be limited by address, because clients on BoringSSL, including Claude Code, then refuse its
  certificates. So a stolen key could pose as a bare address to your devices.
- With remote access on, anyone on your network can read `/v1/health`, which gives mia's version, and `/openapi.json`.
- Someone on your network can make mia do handshakes, up to the limits above, and repeat them as fast as they finish, which
  could slow mia down. How much hasn't been measured. They can't get further without a token.
- The authority's key is a file only your account can read, not an item in the Keychain, so anything running as you could
  read it. The same goes for your images and history.
