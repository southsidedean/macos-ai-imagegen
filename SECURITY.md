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
- Every route but a bare health check and the API's public description needs a token, and only in the
  `Authorization` header. A test walks the app's routes to prove it. Each device
  has its own, with its own permissions, stored only as a hash and shown once. You can revoke one without
  touching the others.
- Requests and event streams are limited per device, as are the queue's length and a request's size.
- An answer that refuses a request doesn't repeat what was sent, and prompts stay out of the server's output.
- Deleting an image also deletes the job that made it. Finished jobs, which hold prompts, are forgotten after 30
  days.
- The API can read or delete image files only in mia's own images folder.
- The API process doesn't load the image engine, so a crash there can't take it down.

Known limits, on this Mac for now because nothing else can reach the API:

- All tokens share one queue and history, and tokens don't expire. mia is built for one person's or household's
  devices.
- A process on your Mac can hold many idle connections open with no token, and could use up the server's
  connections. This needs fixing before remote access arrives.

Still to come, following the design in [PLAN.md](PLAN.md#security): remote access over HTTPS, in 0.4.
Until then, there's no way to turn on access from another machine.
