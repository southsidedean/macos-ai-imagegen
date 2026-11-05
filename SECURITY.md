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

Working today, and tested:

- Model files can't run code: mia downloads safetensors weights and text files only, never code, and
  refuses a repository with no safetensors weights, with no override.
- Only `mia models pull` downloads. Making an image runs offline.
- Images are saved with no metadata, because the image engine would otherwise write your prompt and home
  folder into every one. Prompts are kept in mia's own history file, and stay out of its output.
- mia's folder and history file are readable only by you.

Still to come, because there's no server yet. Each gets built and tested in the release that needs it,
following the design in [PLAN.md](PLAN.md#security):

- It listens only on your Mac until you turn on remote access, and then only over HTTPS.
- Each device gets its own token, with its own permissions, and you can revoke one without touching
  the others.
