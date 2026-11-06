# The REST API

Scripts and other programs on your Mac can ask mia for images through its API, which is also what [the web page](web.md) uses.
It listens only on this Mac until you turn on [HTTPS for your other devices](remote.md).

## Start here

You need mia installed and a model downloaded: the [README](../README.md) has both steps. Then make a
token, and start the server:

```sh
uv run mia token create --for my-script
uv run mia serve
```

The first command prints the token once, so keep it somewhere safe. The server keeps running until you stop
it, with Control-C. In another window, ask for an image:

```sh
export MIA_TOKEN=paste-your-token-here
curl -s http://127.0.0.1:8765/v1/jobs \
  -H "Authorization: Bearer $MIA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a red fox in a snowy forest at sunrise, photograph"}'
```

That answers right away with a job, because an image takes a while to make. Use the job's `id` to check on
it, and when its `state` is `done`, download the picture using the link in its `links`:

```sh
curl -s http://127.0.0.1:8765/v1/jobs/JOB_ID -H "Authorization: Bearer $MIA_TOKEN"
curl -s http://127.0.0.1:8765/v1/images/IMAGE_ID/file -H "Authorization: Bearer $MIA_TOKEN" -o fox.png
```

## Making a job

`POST /v1/jobs` takes these, and only `prompt` is required:

| Field | Default | Notes |
|---|---|---|
| `prompt` | | Up to 4,000 characters |
| `model` | `flux2-klein-4b-q4` | `GET /v1/models` lists them. It has to be downloaded first |
| `seed` | random | The same seed and settings give the same picture |
| `width`, `height` | 1024 | Multiples of 16, from 256 to 2048 |
| `steps` | what the model suggests | From 1 to 50 |

If your script might send a request twice, add an `Idempotency-Key` header with any text you choose, up to 200
characters. The same key from the same token gets the first job back, and with different settings it's refused
with a 409. A retry that leaves out the seed counts as the same request, and gets the first job's seed. A key
lasts as long as its job: 30 days after it finishes, or until you delete the job's image, when the same key
makes a new job.

## Following a job

A job's `state` is `queued`, `running`, `done`, `failed`, or `cancelled`. A failed job has its reason in `error`,
and isn't tried again. One job runs at a time, and the rest wait in order.

To watch a job, poll `GET /v1/jobs/{id}`, or read `GET /v1/jobs/{id}/events`, which sends a server-sent event
whenever the job changes, then ends. A stream also ends after an hour, when its token is revoked or its browser session ends, or
when the server stops, without a `done` event, so check the job if that happens:

```sh
curl -sN http://127.0.0.1:8765/v1/jobs/JOB_ID/events -H "Authorization: Bearer $MIA_TOKEN"
```

A job's `step` is the step that has started, not one that has finished. The last wait is the longest, because
it covers the last step and turning the result into a picture, so treat the last step as "finishing up".

To stop a job, send `DELETE /v1/jobs/{id}`. A waiting job stops at once. A running one stops at its next step,
and a job in its last wait is stopped within about a minute by restarting the worker, which then has to load
the model again. A cancelled job keeps no image. A job that's already finished can't be stopped, and gets a 409.

Finished jobs are forgotten after 30 days. Their images stay until you delete them.

## Every route

| Route | Scope | What it does |
|---|---|---|
| `GET /v1/health` | none | Whether the server is up, and its version |
| `GET /v1/status` | read | The worker, the queue, and free disk |
| `POST /v1/jobs` | generate | Makes a job |
| `GET /v1/jobs` | read | What's running, what's waiting, then recent jobs. Takes `limit` |
| `GET /v1/jobs/{id}` | read | One job |
| `GET /v1/jobs/{id}/events` | read | One job's progress, as server-sent events |
| `DELETE /v1/jobs/{id}` | generate | Stops a job |
| `GET /v1/images` | read | History, newest first. Takes `search`, `limit`, and `offset` |
| `GET /v1/images/{id}` | read | An image's prompt and settings. Its `links` include `preview` only if it has one |
| `GET /v1/images/{id}/file` | read | The PNG. One saved elsewhere with `mia generate --output` gets a 404 |
| `GET /v1/images/{id}/preview` | read | A JPEG up to 512 pixels on its longest side. An image from before 0.4, or one saved elsewhere with `mia generate --output`, has none, and gets a 404 |
| `DELETE /v1/images/{id}` | admin | Permanently deletes an image's record, the job that made it, and its file. A file saved elsewhere with `mia generate --output` stays where it is |
| `GET /v1/models` | read | The models mia knows, and which are downloaded |
| `POST /v1/session` | read | Signs a browser in with a token, and sets its cookie |
| `GET /v1/session` | read | The device that's signed in, and its scope |
| `DELETE /v1/session` | read | Signs a browser out |

`GET /openapi.json` describes all of this in a form tools can read. `/mcp` is for AI assistants: see
[Connecting an AI assistant](mcp.md).

## Tokens

Each device or script gets its own token, so you can stop one without touching the others:

```sh
uv run mia token create --for laptop --scope read
uv run mia token create --for visitor --days 7
uv run mia token list
uv run mia token revoke laptop
```

A token has one of three scopes, and each includes the ones before it. `read` can look, `generate` can also
make and stop jobs (the default), and `admin` can also delete images. mia stores only a hash of each token,
so a lost one can't be shown again: revoke it, and make another. A token lasts until you revoke it, unless you
give it `--days`, from 1 to 3,650.

All tokens work on the same queue and history: a `read` token can see every device's prompts, and a `generate`
token can stop any job. mia is built for one person's or household's devices, not for people who shouldn't see
each other's work.

Tokens go only in the `Authorization` header, never in a URL, where they would end up in logs and histories.

## Signing in from a browser

A script should use its token on every request. A browser can swap one for a cookie instead, so it doesn't have to keep
the token, and images and event streams can load without a header. Send `POST /v1/session` with the token in the
`Authorization` header. The cookie it sets has the token's scope.

A session lasts 90 days, or until its token's end date if that's sooner. `DELETE /v1/session` ends it, and revoking the token
ends all of its sessions. Each token can have 20 at once, and a new one replaces the oldest.

The cookie only works on `/v1/` routes, and only on requests a browser marks as coming from mia's own page. That means
`Sec-Fetch-Site: same-origin`, or `none` for reading, and anything that writes also needs an `Origin` matching the address it
was sent to. Anything else gets a 403. If a request has an `Authorization` header, mia uses that and ignores the cookie.

## Limits

| Limit | Value | What you get |
|---|---|---|
| Requests per device, per minute | 240 | 429, with `Retry-After` |
| Refused requests from one address, per minute | 30 | 429, with `Retry-After`, even with a good token, until the minute has passed |
| Jobs submitted per device, per minute | 30 | 429, with `Retry-After` |
| Jobs waiting in the queue | 20 | 429, with `Retry-After` |
| Open event streams | 8 per device, 32 in all | 429, with `Retry-After` |
| A request's size | 64 KB | 413, or 411 with no `Content-Length` |
| Time for a whole request to arrive | 15 seconds | The connection is closed |
| An answer left unread, with the buffers behind it full | 30 seconds | The connection is closed. A client still reading only gets this below about 10 KB a second |
| Open connections | 64 from one address, 256 in all | A new connection is closed |
| Free disk needed to accept a job | 2 GB | 507 |

A wrong token or cookie counts as a refused request. A request with neither doesn't, and still gets a 401.

A model that isn't downloaded yet gets a 409, with the command that downloads it. A request that can't be
understood gets a 422 saying which field and why, without repeating your prompt. Anything but `GET /v1/health`,
`GET` or `HEAD /openapi.json`, and the web page's own files gets a 401 without a token, even a path that doesn't exist.

A job has no limit on how long it takes, as long as it keeps moving. If its `step` doesn't change for 30 minutes,
mia restarts the worker and marks the job failed, and the next job runs. Time the Mac spends asleep doesn't count.

## Good to know

To keep the server running without a window open, see [Keeping mia running](service.md). While it's running, its worker
holds the GPU, so `mia generate` refuses to run. Send the job through the API instead, or stop the server first.

Stopping the server fails the job it's running and keeps the ones waiting, which run when it starts again.

Changing model makes the worker restart, to hand back the first model's memory, so the first job on a new model
takes longer. If the worker crashes or the Mac runs out of memory, the job it was running is marked failed, the
server stays up, and a new worker takes the next job.
