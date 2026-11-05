# Connecting an AI assistant

mia has an MCP server, so an assistant that speaks MCP can make images, follow them, and look through what you've
made. The assistant writes the prompt, and your Mac does the work. mia holds no AI keys and calls no AI service.

For now, the assistant has to run on the same Mac. Reaching mia from another machine comes later in 0.4, with HTTPS.

## Start here

You need mia installed and a model downloaded: the [README](../README.md) has both steps. Make a token for the
assistant, and start the server:

```sh
uv run mia token create --for my-assistant
uv run mia serve
```

The first command prints the token once. In another window, add mia to the assistant. With Claude Code:

```sh
claude mcp add --transport http mia http://127.0.0.1:8765/mcp --header "Authorization: Bearer paste-your-token-here"
```

Then ask for what you want, such as "make me an image of a red fox in a snowy forest". Other assistants need the same
two things in their settings: the address `http://127.0.0.1:8765/mcp`, and the `Authorization` header.

## What the assistant can do

| Tool | Scope | What it does |
|---|---|---|
| `mia_generate` | generate | Starts a job, and can wait up to 55 seconds for it |
| `mia_job` | read | A job's state and progress, with its picture once it's done. It can wait too |
| `mia_cancel` | generate | Stops a job |
| `mia_images` | read | Searches the images already made |
| `mia_image` | read | One image's prompt and settings, with its picture |
| `mia_models` | read | The models mia knows, their licenses, and which are downloaded |
| `mia_status` | read | The worker, the queue, and free disk |

A finished picture comes back as two things: a preview, a JPEG up to 512 pixels on its longest side, which the assistant
can see, and a `mia://images/...` link to the full PNG, which it can read if it needs to. Images made before 0.4 have
only the link.

The assistant can't delete images, and it can't download or remove models, whatever its token's scope. A download can
take many gigabytes, so that choice stays with you. Give it a `read` token if it should only look.

## If your assistant only runs local commands

Some assistants start a program and talk to it directly. `mia mcp` is that program, and passes everything on to `mia
serve`, which still has to be running:

```sh
claude mcp add mia -e MIA_TOKEN=paste-your-token-here -- uv run --directory /path/to/macos-ai-imagegen mia mcp
```

It reads the token only from `MIA_TOKEN`. `--url` or `MIA_URL` says where the server is, by default
`http://127.0.0.1:8765`. It refuses plain `http` to any other machine, because the token and your prompts would cross the
network unprotected.

## Good to know

- **Privacy.** What a tool returns goes to your assistant, and from there to whichever AI service it uses. That includes
  previews, and prompts other devices wrote. An assistant may also keep its own copy: Claude Code saves a full image it
  reads in its own folder.
- **Settings files hold the token.** Both commands above store it in the assistant's settings. Give each assistant its
  own token, and `uv run mia token revoke my-assistant` stops it.
- **Limits.** A tool call counts against the same [limits](api.md#limits) as the API, and counts more than once: the
  call itself, plus one or more requests behind it. A wait checks the job every 2 seconds.
- **Waiting.** If an assistant gives up on a tool call before its wait is over, the job carries on, and `mia_job` can
  pick it up. Lower `wait_seconds` if yours is impatient.
- **Prompts are data.** mia tells assistants to treat everything it returns as data, not instructions, because a prompt
  may have come from another device. That's advice to the assistant, which mia can't enforce.
