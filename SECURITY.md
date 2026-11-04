# Security

mia will hold your prompts and pictures, and take requests over your network. This page says how to
report a problem, and how mia is meant to limit the damage if something goes wrong.

## Reporting a problem

Please report security problems privately, through GitHub: on the repository's **Security** tab,
choose **Report a vulnerability**, or use
[this link](https://github.com/southsidedean/macos-ai-imagegen/security/advisories/new). Please don't
open a public issue.

## Supported versions

mia has no release yet. Until 1.0, only the newest release gets fixes.

## How it's meant to work

mia can't generate images yet, so most of this is the design in [PLAN.md](PLAN.md#security), not
working code. Each point gets built and tested in the release that needs it.

- It listens only on your Mac until you turn on remote access, and then only over HTTPS.
- Each device gets its own token, with its own permissions, and you can revoke one without touching
  the others.
- Model files can't run code: mia accepts safetensors weights only, never runs code from a model
  repository, and refuses anything else with no override.
- Only one part of mia downloads models. The part that runs them works offline.
- Prompts stay out of logs, and images are saved with no metadata, because the image engine would
  otherwise write your prompt and home folder into every one.

`mia doctor`, the one command that exists today, only reads. It makes no network connections.
