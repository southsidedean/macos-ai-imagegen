// mia's page. It uses only the public API, signed in with a cookie the page can't read. Prompts and other text from the API
// only ever go in as text: nothing here builds HTML from a string, and a test checks that.
"use strict";

const PAGE_SIZE = 24;
const POLL_BUSY = 5000, POLL_IDLE = 30000; // milliseconds. A running job's progress comes from its event stream
const THUMB_RETRY = 30000, THUMB_RETRIES = 2; // a page of pictures can reach the limit on requests, which lasts a minute
const FAILED_SHOWN = 3, FAILED_FOR = 3600000; // a job that failed stays listed for an hour, in case nobody was looking

const $ = (id) => document.getElementById(id);

let me = null; // the signed-in device
let models = [];
let shownImages = 0;
let knownDone = null; // jobs already finished, to spot new pictures, including another device's
let stream = null; // one at a time: a browser has about six connections to a host, and mia speaks HTTP/1.1
let pollTimer = null;
let searchTimer = null;
let opened = null; // the image in the dialog
let lastSubmit = null; // so a retry after a lost answer can't make the picture twice

class Problem extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function element(tag, properties = {}, ...children) {
  const made = document.createElement(tag);
  Object.assign(made, properties);
  made.append(...children);
  return made;
}

function show(id, message) {
  $(id).textContent = message;
  $(id).hidden = !message;
}

async function reason(answer) {
  try {
    const { detail } = await answer.json();
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail[0]) return `${detail[0].loc.at(-1)}: ${detail[0].msg}`;
  } catch {
    // Not JSON: the status below will have to do.
  }
  return `mia answered with an error (${answer.status}).`;
}

async function api(method, path, { body, headers = {} } = {}) {
  const options = { method, headers: { ...headers } };
  if (body !== undefined) {
    options.body = JSON.stringify(body);
    options.headers["Content-Type"] = "application/json";
  }
  let answer;
  try {
    answer = await fetch(path, options);
  } catch {
    throw new Problem("mia can't be reached. Is the Mac awake, and on this network?", 0);
  }
  if (answer.status === 401 && me) {
    signedOut("Your sign-in has ended. Sign in again with a token.");
  }
  if (!answer.ok) throw new Problem(await reason(answer), answer.status);
  return answer.status === 204 ? null : answer.json();
}

// Signing in and out

function signedOut(message = "") {
  me = null;
  stopWatching();
  $("main-view").hidden = true;
  $("account").hidden = true;
  $("sign-in-view").hidden = false;
  $("detail").close();
  // What was on the page was private, and the next person to sign in may not be the same.
  $("images").replaceChildren();
  $("queue").replaceChildren();
  knownDone = null;
  shownImages = 0;
  show("sign-in-problem", message);
}

async function signIn(event) {
  event.preventDefault();
  const token = $("token").value.trim();
  show("sign-in-problem", "");
  try {
    me = await api("POST", "/v1/session", { headers: { Authorization: `Bearer ${token}` } });
  } catch (problem) {
    show("sign-in-problem", problem.status === 401 ? "That token wasn't accepted. Check that all of it was pasted, and that it hasn't been revoked." : problem.message);
    return;
  } finally {
    $("token").value = "";
  }
  await enter();
}

async function signOut() {
  try {
    await api("DELETE", "/v1/session");
  } catch (problem) {
    show("notice", problem.message);
    return;
  }
  signedOut();
}

const allows = (scope) => ["read", "generate", "admin"].indexOf(me.scope) >= ["read", "generate", "admin"].indexOf(scope);

async function enter() {
  $("sign-in-view").hidden = true;
  $("main-view").hidden = false;
  $("account").hidden = false;
  $("device").textContent = me.device;
  $("generate").hidden = !allows("generate");
  $("read-only").hidden = allows("generate");
  $("detail-reuse").hidden = !allows("generate");
  $("detail-delete").hidden = !allows("admin");
  await Promise.all([loadModels(), loadImages(true)]);
  await poll();
}

// The form

async function loadModels() {
  try {
    const listed = await api("GET", "/v1/models");
    models = listed.models;
    const chosen = listed.default;
    $("model").replaceChildren(...models.map((model) => element("option", {
      value: model.id,
      textContent: model.installed ? model.title : `${model.title} (not downloaded)`,
      selected: model.id === chosen,
    })));
    describeModel();
  } catch (problem) {
    show("notice", problem.message);
  }
}

function describeModel() {
  const model = models.find((known) => known.id === $("model").value);
  if (!model) return;
  $("license").textContent = `License: ${model.license}.`;
  $("steps").placeholder = String(model.steps);
}

function chosenSize() {
  const value = $("size").value;
  if (value === "custom") return [Number($("width").value), Number($("height").value)];
  return value.split("x").map(Number);
}

async function generate(event) {
  event.preventDefault();
  const [width, height] = chosenSize();
  const body = { prompt: $("prompt").value, model: $("model").value, width, height };
  if ($("seed").value !== "") body.seed = Number($("seed").value);
  if ($("steps").value !== "") body.steps = Number($("steps").value);

  const sent = JSON.stringify(body);
  if (!lastSubmit || lastSubmit.sent !== sent) lastSubmit = { sent, key: crypto.randomUUID() };
  show("generate-problem", "");
  $("generate-button").disabled = true;
  try {
    await api("POST", "/v1/jobs", { body, headers: { "Idempotency-Key": lastSubmit.key } });
    lastSubmit = null;
    await poll();
  } catch (problem) {
    show("generate-problem", problem.message);
  } finally {
    $("generate-button").disabled = false;
  }
}

function reuse(image) {
  $("prompt").value = image.prompt;
  if ([...$("model").options].some((option) => option.value === image.model)) $("model").value = image.model;
  describeModel();
  const size = `${image.width}x${image.height}`;
  const preset = [...$("size").options].some((option) => option.value === size);
  $("size").value = preset ? size : "custom";
  $("width").value = image.width;
  $("height").value = image.height;
  $("custom-size").hidden = preset;
  $("seed").value = image.seed;
  $("steps").value = image.steps;
  $("advanced").open = true;
  $("detail").close();
  $("compose").scrollIntoView();
  $("prompt").focus();
}

// The queue

function tiles(job) {
  const row = element("div", { className: "tiles" });
  row.setAttribute("role", "progressbar");
  row.setAttribute("aria-valuemin", "0");
  row.setAttribute("aria-valuemax", String(job.steps));
  row.setAttribute("aria-valuenow", String(job.step));
  for (let step = 1; step <= job.steps; step += 1) {
    const state = step < job.step ? " lit" : step === job.step ? " lit now" : "";
    row.append(element("span", { className: `tile${state}` }));
  }
  return row;
}

function jobState(job) {
  if (job.cancel_requested && job.state === "running") return "Stopping";
  if (job.state === "queued") return "Waiting";
  if (job.state === "failed") return "Failed";
  if (job.step === 0) return "Loading the model";
  // A step is reported when it starts, and the last wait is the longest: it covers the last step and making the picture.
  return job.step >= job.steps ? "Finishing up" : `Step ${job.step} of ${job.steps}`;
}

function jobItem(job) {
  const item = element("li", { className: `job job-${job.state}` });
  item.dataset.job = job.id;
  item.append(element("div", { className: "job-prompt", textContent: job.prompt, title: job.prompt }));
  if (job.state === "running") item.append(tiles(job));
  const line = element("div", { className: "job-line" }, element("span", { className: "job-state", textContent: jobState(job) }));
  if (job.state !== "failed" && allows("generate") && !job.cancel_requested) {
    const stop = element("button", { type: "button", className: "quiet", textContent: job.state === "queued" ? "Remove" : "Stop" });
    stop.addEventListener("click", () => cancel(job.id, stop));
    line.append(stop);
  }
  item.append(line);
  if (job.error) item.append(element("div", { className: "job-error", textContent: job.error }));
  return item;
}

async function cancel(id, button) {
  button.disabled = true;
  try {
    await api("DELETE", `/v1/jobs/${id}`);
  } catch (problem) {
    if (problem.status !== 409) show("notice", problem.message); // 409: it had already finished
  }
  await poll();
}

function showJobs(jobs) {
  const active = jobs.filter((job) => job.state === "running" || job.state === "queued");
  const failed = jobs.filter((job) => job.state === "failed" && Date.now() - Date.parse(job.finished) < FAILED_FOR).slice(0, FAILED_SHOWN);
  $("queue").replaceChildren(...[...active, ...failed].map(jobItem));
  $("queue-empty").hidden = active.length + failed.length > 0;
  return active;
}

function stopWatching() {
  clearTimeout(pollTimer);
  closeStream();
}

function closeStream() {
  if (stream) stream.source.close();
  stream = null;
}

function watch(job) {
  if (stream && stream.id === job.id) return;
  closeStream();
  const source = new EventSource(job.links.events);
  stream = { id: job.id, source };
  source.addEventListener("progress", (event) => {
    const fresh = JSON.parse(event.data);
    const item = document.querySelector(`[data-job="${fresh.id}"]`);
    if (item) item.replaceWith(jobItem(fresh));
  });
  source.addEventListener("done", () => {
    closeStream();
    poll();
  });
  // Left alone, it would retry for ever. Polling carries on, and starts a new one if the job is still running.
  source.addEventListener("error", closeStream);
}

async function noteNewImages(jobs) {
  const done = jobs.filter((job) => job.state === "done" && job.image);
  const fresh = knownDone === null ? [] : done.filter((job) => !knownDone.has(job.id));
  knownDone = new Set([...(knownDone || []), ...done.map((job) => job.id)]);
  if (!fresh.length || $("search").value) return;
  const images = await Promise.all(fresh.reverse().map((job) => api("GET", job.links.image)));
  for (const image of images) {
    $("images").prepend(thumbnail(image));
    shownImages += 1;
  }
  $("images-empty").hidden = true;
}

async function poll() {
  clearTimeout(pollTimer);
  if (!me || document.hidden) return;
  let wait = POLL_IDLE;
  try {
    const { jobs } = await api("GET", "/v1/jobs?limit=20");
    if (!me) return;
    const active = showJobs(jobs);
    const running = active.find((job) => job.state === "running");
    if (running) watch(running);
    else closeStream();
    if (active.length) wait = POLL_BUSY;
    await noteNewImages(jobs);
    $("notice").hidden = true;
  } catch (problem) {
    if (!me) return;
    show("notice", problem.message);
    wait = POLL_BUSY;
  }
  pollTimer = setTimeout(poll, wait);
}

// The gallery

function thumbnail(image) {
  const source = image.links.preview || image.links.file; // images from before 0.4 have no preview
  const picture = element("img", { src: source, alt: image.prompt, loading: "lazy" });
  let retries = 0;
  picture.addEventListener("error", () => {
    if (retries < THUMB_RETRIES) {
      retries += 1;
      setTimeout(() => { picture.src = source; }, THUMB_RETRY);
    }
  });
  const button = element("button", { type: "button", className: "thumb", title: image.prompt }, picture);
  button.addEventListener("click", () => openImage(image));
  const item = element("li", {}, button);
  item.dataset.image = image.id;
  return item;
}

async function loadImages(fresh) {
  const search = $("search").value;
  const offset = fresh ? 0 : shownImages;
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset), search });
  try {
    const { images } = await api("GET", `/v1/images?${query}`);
    if (search !== $("search").value) return; // something newer was typed while this was on its way
    if (fresh) {
      $("images").replaceChildren();
      shownImages = 0;
    }
    $("images").append(...images.map(thumbnail));
    shownImages += images.length;
    $("more").hidden = images.length < PAGE_SIZE;
    show("images-empty", shownImages ? "" : search ? "No pictures match that search." : "No pictures yet. The ones you make will appear here.");
  } catch (problem) {
    if (me) show("notice", problem.message);
  }
}

function openImage(image) {
  opened = image;
  $("detail-image").src = image.links.file;
  $("detail-image").alt = image.prompt;
  $("detail-prompt").textContent = image.prompt;
  const settings = [
    ["Model", image.model],
    ["Size", `${image.width} × ${image.height}`],
    ["Seed", image.seed],
    ["Steps", image.steps],
    ["Made", new Date(image.created).toLocaleString()],
    ["Engine", `${image.engine} ${image.engine_version}`],
  ];
  $("detail-settings").replaceChildren(...settings.flatMap(([name, value]) => [
    element("dt", { textContent: name }),
    element("dd", { textContent: String(value) }),
  ]));
  $("detail-file").href = image.links.file;
  $("detail-delete").textContent = "Delete";
  $("detail-delete").dataset.sure = "";
  show("detail-problem", "");
  $("detail").showModal();
}

async function remove() {
  // Deleting can't be undone, so it takes two presses.
  if (!$("detail-delete").dataset.sure) {
    $("detail-delete").dataset.sure = "yes";
    $("detail-delete").textContent = "Delete for good?";
    return;
  }
  try {
    await api("DELETE", opened.links.self);
  } catch (problem) {
    show("detail-problem", problem.message);
    return;
  }
  const item = document.querySelector(`[data-image="${opened.id}"]`);
  if (item) item.remove();
  shownImages -= 1;
  $("detail").close();
}

// Starting up

function listen() {
  $("sign-in-form").addEventListener("submit", signIn);
  $("sign-out").addEventListener("click", signOut);
  $("generate").addEventListener("submit", generate);
  $("model").addEventListener("change", describeModel);
  $("size").addEventListener("change", () => {
    const custom = $("size").value === "custom";
    $("custom-size").hidden = !custom;
    if (custom) $("advanced").open = true;
  });
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadImages(true), 250);
  });
  $("more").addEventListener("click", () => loadImages(false));
  $("detail-close").addEventListener("click", () => $("detail").close());
  $("detail-reuse").addEventListener("click", () => reuse(opened));
  $("detail-delete").addEventListener("click", remove);
  $("detail").addEventListener("close", () => { $("detail-image").removeAttribute("src"); });
  // A phone stops a page in the background, and its stream with it.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopWatching();
    else if (me) poll();
  });
}

async function start() {
  listen();
  try {
    me = await api("GET", "/v1/session");
  } catch (problem) {
    if (problem.status === 401) signedOut();
    else {
      signedOut();
      show("notice", problem.message);
    }
    return;
  }
  await enter();
}

start();
