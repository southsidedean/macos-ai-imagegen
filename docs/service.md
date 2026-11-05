# Keeping mia running

`mia serve` stops when you close its window. To keep it running, install it as a service: it starts now and at every
login, and comes back by itself if it dies.

## Start here

Make a token if you don't have one, then install the service:

```sh
uv run mia token create --for my-script
uv run mia service install
uv run mia service status
```

`status` should say `running` and `answering`. mia now listens on port 8765 of this Mac, as `mia serve` does, and the
[API](api.md) and [assistant](mcp.md) guides work the same way. It uses the same images, history, and tokens as before.

To stop it and remove it:

```sh
uv run mia service uninstall
```

Your images, history, and tokens stay. There's no separate stop command: uninstall it, and install it again when you
want it back.

## Options

`uv run mia service install --port 9000` uses another port. Installing again replaces the service, which is also what to
do if you move mia's folder, or want a change of `MIA_HOME` or `MIA_MODELS` to take effect. Replacing it stops any job
it's running, and the jobs waiting run when it's back.

`status` exits with an error if the service isn't installed or isn't answering, so a script can check it.

## Good to know

- **Someone has to be logged in.** The service is a user agent, which runs only while you're logged in at the Mac, because
  that's where it can use the GPU. A locked screen is fine. Logging out stops it.
- **On a Mac used as a server,** turn on automatic login in System Settings, under Users & Groups, so that a restart brings
  mia back by itself. macOS doesn't offer this while FileVault is on, so you'd have to choose between them.
- **Sleep.** mia keeps the Mac awake while a job runs, and only then. It stops idle sleep, not the display from turning off,
  and not a sleep you ask for: closing a laptop's lid will still usually stop a job. Time spent asleep doesn't count against
  a job, but a sleeping Mac can't take new requests, and mia can't wake it. If requests have to arrive at any time, stop the
  Mac sleeping in System Settings.
- **`mia generate`** refuses to run while the service holds the GPU. Send the job through the API, or uninstall the service
  first.
- **The log** is `logs/serve.log` in mia's folder, and `status` shows where. It holds problems and progress bars, never
  prompts or tokens. It isn't rotated, though it grows by only a little per job.
- **Under the hood:** the service is one launchd agent, `local.mia.serve`, in `~/Library/LaunchAgents`. It starts `mia serve`,
  which looks after its worker. The file holds no token.
