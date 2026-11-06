# Security

mia holds your prompts and pictures, and can take requests over your network. This page says how to
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
- The API listens only on this Mac unless you turn on remote access, and refuses a request whose `Host` or `Origin` names
  anywhere else, which stops a web page from reaching it through DNS rebinding.
- Everything but a bare health check and the API's public description needs a token, and only in the
  `Authorization` header. It's checked before a request's body is read, and a test walks the app's routes to
  prove each needs one. Each device has its own, with its own permissions, stored only as a hash and shown once.
  You can revoke one without touching the others, and give one an end date.
- A browser swaps its token for a session cookie that scripts on a page can't read, and keeps no token. The session is stored only
  as a hash, has the token's permissions, lasts at most 90 days, and ends with its token. A cookie is refused unless the
  browser marks the request as coming from mia's own page, with `Sec-Fetch-Site`, and `Origin` too for anything that writes. So
  another site can't use it, and neither can another program's page on this Mac. It doesn't work on MCP.
- Requests and event streams are limited per device. The queue's length and a request's size have limits too, and refused
  requests are limited per address.
- A connection gets 15 seconds to deliver a whole request, and is closed if an answer sits unread for 30 seconds with the
  buffers behind it full, which a client still reading only reaches below about 10 KB a second. Connections are capped per address and overall, so holding many open with no token is limited.
- A refusal from the REST API doesn't repeat your prompt, and prompts stay out of the server's output.
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
- Browsers send a cookie to every port on a name, so another web server on the same name gets mia's session cookie if your browser
  visits it. On this Mac, that's another program serving pages on `127.0.0.1` or `localhost`. Over HTTPS, it's another HTTPS
  server on the name you use for mia. Don't sign in to mia on a name it shares with something you don't trust.
- A client can hold a connection open by reading answers just fast enough, with or without a token, up to the connection
  caps. Five addresses could fill the 256 connections, which would keep everyone out, including this Mac's own tools, for
  as long as they kept it up.
- Your own network means any private network the Mac joins, including a hotel's Wi-Fi or a VPN, so turn remote access off
  if you take the Mac somewhere you don't trust. Strangers there still need a token to do anything but the limits above.
- `mia remote on --name` accepts any name, so an authority made for a public domain could vouch for that whole domain. Use
  names on your own network.
- An MCP tool's error can repeat what it was sent, to the assistant that sent it. It doesn't reach the log.
- If the worker hits an error that quotes the prompt, and writing to the database fails at the same moment, the prompt can
  reach the log. No real error from the engine is known to quote it.
- `Upgrade` requests write warnings to the log with no token needed. Rotation keeps its size limited, but older entries can
  be pushed out quickly.
- The authority can't be limited by address, because clients on BoringSSL, including Claude Code, then refuse its
  certificates. So a stolen key could pose as a bare address to your devices.
- With remote access on, anyone on your network can read `/v1/health`, which gives mia's version, and `/openapi.json`.
- Someone on your network can make mia do handshakes, up to the limits above, and repeat them as fast as they finish, which
  could slow mia down. How much hasn't been measured. With every handshake slot full, new HTTPS connections are dropped until
  one comes free, at most 5 seconds later. They can't get further without a token.
- The authority's key is a file only your account can read, not an item in the Keychain, so anything running as you could
  read it. The same goes for your images and history.
