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
[API](api.md), [assistant](mcp.md), and [other devices](remote.md) guides work the same way. It uses the same images, history, and tokens as before.

To stop it and start it again:

```sh
uv run mia service stop
uv run mia service start
```

A stopped service stays stopped until you start it or log in again. To remove it for good:

```sh
uv run mia service uninstall
```

Your images, history, and tokens stay.

## After upgrading

The service keeps running the code it started with, so restart it when you've upgraded mia. `mia doctor` and `mia service
status` both say if it's running an older version.

```sh
uv run mia service restart
```

Stopping or restarting fails a job that's running. Jobs that are waiting stay in the queue, and run when it's back.

## Options

`uv run mia service install --port 9000` uses another port. Installing again replaces the service, which is also what to
do if you move mia's folder, or want a change of `MIA_HOME` or `MIA_MODELS` to take effect. Replacing it stops any job
it's running, and the jobs waiting run when it's back.

`status` exits with an error if the service isn't installed or isn't answering, so a script can check it.

## Good to know

- **Memory.** After a job, the model stays loaded for 10 minutes, so that the next job starts quickly. Then mia gives the
  memory back, and the next job takes longer while the model loads. To change the wait, set `MIA_IDLE_SECONDS` to a number
  of seconds and run `mia service install` again. A worker with no model loaded holds very little.
- **Someone has to be logged in.** The service is a user agent, which runs only while you're logged in at the Mac, because
  that's where it can use the GPU. A locked screen is fine. Logging out stops it.
- **On a Mac used as a server,** turn on automatic login in System Settings, under Users & Groups, so that a restart brings
  mia back by itself. macOS doesn't offer this while FileVault is on, so you'd have to choose between them.
- **Sleep.** mia keeps the Mac awake while a job runs, and only then. It stops idle sleep, not the display from turning off,
  and not a sleep you ask for: closing a laptop's lid will still usually stop a job. Time spent asleep doesn't count against
  a job, but a sleeping Mac can't take new requests, and mia can't wake it. If requests have to arrive at any time, stop the
  Mac sleeping in System Settings.
- **`mia generate`** refuses to run while the service holds the GPU, even with no model loaded. Send the job through the API,
  or run `mia service stop` first.
- **The log** is `logs/serve.log` in mia's folder, and `status` shows where. It holds problems and progress bars, never
  prompts or tokens. Above 10 MB it moves to `serve.log.1`, replacing the one before, so the two never take more than
  about 20 MB.
- **Under the hood:** the service is one launchd agent, `local.mia.serve`, in `~/Library/LaunchAgents`. It starts `mia serve`,
  which looks after its worker. The file holds no token.
