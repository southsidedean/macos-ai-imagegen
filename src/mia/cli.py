"""The mia command. Only `token create` and `web` print a secret, once, and only `history` prints prompts back."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from mia import __version__, catalog, doctor, gpu, history, models, paths, tokens
from mia.engine import Cancelled, MiaError, Request, random_seed
from mia.generate import again, generate, load_engine

SIZE = 1024
PORT = 8765
WEB_DEVICE = "browser"  # the token mia web makes
EXIT_STOPPED = 130  # the shell's usual code for a job stopped by Ctrl-C


def _progress(step: int, total: int) -> None:
    # Reported as started, not finished: see Progress in engine.py.
    print(f"step {step} of {total}" + (", then finishing up" if step == total else ""), file=sys.stderr, flush=True)


def _stop(_signal: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _generate(args: argparse.Namespace) -> int:
    # launchd and scripts stop things with SIGTERM. Treat it like Ctrl-C, so no half picture is left.
    signal.signal(signal.SIGTERM, _stop)
    if args.again:
        remake = again(args.again)
        model, request = remake.model, remake.request
    else:
        if not args.prompt:
            raise MiaError("say what to make: mia generate \"a red fox in the snow\"")
        model = catalog.find(args.model)
        seed = random_seed() if args.seed is None else args.seed
        request = Request(args.prompt, seed, args.width, args.height, model.steps if args.steps is None else args.steps)
    print(f"{model.title}, seed {request.seed}, {request.width} by {request.height}, {request.steps} steps", file=sys.stderr)
    with gpu.hold():
        engine = load_engine(model)
        start = time.perf_counter()
        record = generate(request, model, engine, args.output, _progress)
    print(f"{record.id}: done in {time.perf_counter() - start:.0f} seconds", file=sys.stderr)
    print(record.file)
    if args.again:
        same = record.pixels == remake.original.pixels
        print(f"identical pixels to {args.again}: {'yes' if same else 'no'}", file=sys.stderr)
        if not same:
            if record.engine_version != remake.original.engine_version:
                print(f"made with {record.engine} {record.engine_version}, not {remake.original.engine_version}", file=sys.stderr)
            return 1
    return 0


def _models_list() -> int:
    for model in catalog.MODELS:
        state = "ready" if models.is_installed(model, paths.models_folder()) else "not downloaded"
        default = ", default" if model is catalog.DEFAULT else ""
        print(f"{model.id}: {model.title} | {model.license} | {model.download_gb} GB | {state}{default}")
    return 0


def _models_pull(args: argparse.Namespace) -> int:
    model = catalog.find(args.model)
    folder = paths.models_folder()
    if models.is_installed(model, folder):
        print(f"{model.id} is already downloaded, in {models.folder_for(model, folder)}")
        return 0
    hub = models.HuggingFace()
    chosen = models.plan(model, hub)
    print(f"{model.title}\n  from {model.repo} at {model.revision[:10]}\n  license: {model.license} ({model.license_class})")
    print(f"  download: {chosen.size_gb:.1f} GB, to {models.folder_for(model, folder)}")
    if chosen.skipped:
        print(f"  left out: {', '.join(item.name for item in chosen.skipped)}")
    if not args.yes:
        if not sys.stdin.isatty():
            raise MiaError("nothing downloaded: add --yes to agree when running without a terminal")
        if input("Download it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing downloaded.")
            return 0
    print(f"ready, in {models.pull(model, folder, hub, chosen)}")
    return 0


def _printable(text: str) -> str:
    """Control characters written out: a prompt can come from another device, and mustn't drive this terminal."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape", "replace").decode() for c in text)


def _history() -> int:
    for record in history.recent(paths.history_file()):
        print(f"{record.id}  {record.created}  {record.model}  seed {record.seed}  {_printable(record.prompt)}")
    return 0


def _token(args: argparse.Namespace) -> int:
    if args.action == "create":
        issued = tokens.create(args.device, args.scope, args.days)
        lasting = f", lasting {args.days} days" if args.days else ""
        print(f"A {args.scope} token for {args.device}{lasting}. It won't be shown again:", file=sys.stderr)
        print(issued)
    elif args.action == "revoke":
        tokens.revoke(args.device)
        print(f"Revoked the token for {args.device}.")
    else:
        for device in tokens.devices():
            used = "never used" if device.last_used is None else "used " + time.strftime("%Y-%m-%d", time.localtime(device.last_used))
            state = f"revoked {device.revoked}" if device.revoked else used
            if device.expires is not None and not device.revoked:
                state += ("  expired " if device.expires <= time.time() else "  expires ") + time.strftime("%Y-%m-%d", time.localtime(device.expires))
            print(f"{device.name}  {device.scope}  {state}")
    return 0


def _copy(text: str) -> bool:
    """To the Mac's clipboard, where it can be pasted, and reach a phone through Universal Clipboard. Printing it is enough elsewhere."""
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _web(args: argparse.Namespace) -> int:
    """A token can't be shown twice, as only its hash is kept. So this makes one the first time, and after that only opens the page."""
    from mia import service

    current = next((device for device in tokens.devices() if device.name == WEB_DEVICE and device.revoked is None), None)
    if current is not None and not args.new and tokens.still_active(current.id):
        print(f"The token for {WEB_DEVICE} was shown when it was made. If a browser needs it and it's lost, run mia web --new.", file=sys.stderr)
    else:
        if current is not None:
            tokens.revoke(WEB_DEVICE)
        issued = tokens.create(WEB_DEVICE, args.scope)
        copied = " and it's on your clipboard" if _copy(issued) else ""
        print(f"A {args.scope} token for {WEB_DEVICE}. It won't be shown again{copied}. Paste it into the page to sign in:", file=sys.stderr)
        print(issued)
    port = args.port or service.installed_port() or PORT
    address = f"http://127.0.0.1:{port}"
    if service.running_version(port) is None:
        print(f"mia isn't answering at {address}. Start it with mia serve or mia service install, then run mia web again.", file=sys.stderr)
        return 1
    print(f"Opening {address}", file=sys.stderr)
    webbrowser.open(address)
    return 0


def _service(args: argparse.Namespace) -> int:
    from mia import service

    if args.action == "install":
        service.install(args.port)
        print(f"mia is running on port {args.port}, and will start whenever you log in. Its log: {service.log_file()}")
    elif args.action == "stop":
        service.stop()
        print("Stopped. It stays stopped until the next login, or mia service start. A job it was running has failed.")
    elif args.action == "start":
        service.start()
        print("Started.")
    elif args.action == "restart":
        service.restart()
        print("Restarted, running the mia that's installed now. A job it was running has failed, and waiting ones carry on.")
    elif args.action == "uninstall":
        print("Stopped and removed mia's service. Your images, history, and tokens are still there." if service.uninstall() else "mia's service wasn't installed.")
    else:
        found = service.status()
        if not found.installed:
            print("not installed: mia service install sets it up")
            return 1
        print(f"installed, port {found.port}, " + (f"running as process {found.pid}" if found.pid else "not running") + (", answering" if found.answering else ", not answering"))
        if found.version and found.version != __version__:
            print(f"it's running mia {found.version}, and {__version__} is installed: mia service restart moves it on")
        print(f"log: {service.log_file()}")
        return 0 if found.answering else 1
    return 0


def _remote(args: argparse.Namespace) -> int:
    from mia import remote, service

    if args.action == "on":
        port = args.port or remote.saved_port()
        if port == service.installed_port():
            raise MiaError(f"the service already uses port {port} for this Mac's own HTTP: choose another with --port")
        names = args.name or ([] if args.address else remote.suggested_names())
        addresses = args.address or ([] if args.name else remote.suggested_addresses())
        settings = remote.turn_on(names, addresses, port, args.new_authority)
        print("Remote access is on, for your own network, at:")
        for url in settings.urls:
            print(f"  {url}")
        print("It starts with mia serve: mia service restart does that, and fails any job that's running.")
        print(f"Each device has to trust mia's certificate authority, from mia remote authority. Its fingerprint:\n  {remote.fingerprint()}")
    elif args.action == "off":
        off = remote.turn_off()
        print("Remote access is off from the next time mia serve starts: mia service restart does that." if off else "Remote access was already off.")
    elif args.action == "authority":
        if not remote.authority_file().exists():
            raise MiaError("there's no certificate authority yet: mia remote on makes one")
        print(remote.authority_file().read_text(), end="")
    else:
        found = remote.state()
        if found is None:
            print("off: mia remote on turns it on")
            return 0
        print("on, at " + ", ".join(found.settings.urls))
        print("listening now" if found.listening else "not listening: it starts with mia serve, and if that's running, its log says why")
        print(f"certificate until {found.certificate_ends:%Y-%m-%d}, renewed by itself" if found.certificate_ends else "no certificate: run mia remote on again")
        print(f"authority's fingerprint: {remote.fingerprint()}")
    return 0


def _mcp(url: str, authority: str | None) -> int:
    """The token comes only from the environment: a command's arguments can be seen by other users of the Mac."""
    token = os.environ.get("MIA_TOKEN")
    if not token:
        raise MiaError("MIA_TOKEN has to hold a token: make one on the Mac running mia serve, with mia token create")
    from mia.mcp_server import bridge

    bridge(url, token, authority).run("stdio")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mia", description="Generate AI images on your own Mac.")
    parser.add_argument("--version", action="version", version=f"mia {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="is this machine ready? changes nothing")
    make = commands.add_parser("generate", help="make an image")
    make.add_argument("prompt", nargs="?", help="what to make")
    make.add_argument("--model", default=catalog.DEFAULT.id, help="default: %(default)s")
    make.add_argument("--seed", type=int, help="the same seed and settings give the same picture; default: random")
    make.add_argument("--width", type=int, default=SIZE, help="default: %(default)s")
    make.add_argument("--height", type=int, default=SIZE, help="default: %(default)s")
    make.add_argument("--steps", type=int, help="default: what the model suggests")
    make.add_argument("--output", type=Path, help="where to save the PNG; default: mia's images folder")
    make.add_argument("--again", metavar="ID", help="make an image from the history again, and compare its pixels")
    listing = commands.add_parser("models", help="the models mia knows")
    pulls = listing.add_subparsers(dest="action")
    pull = pulls.add_parser("pull", help="download a model")
    pull.add_argument("model", nargs="?", default=catalog.DEFAULT.id, help="default: %(default)s")
    pull.add_argument("--yes", action="store_true", help="don't ask first")
    commands.add_parser("history", help="recent images, with their prompts")
    serve = commands.add_parser("serve", help="run the REST API, MCP, and a worker")
    serve.add_argument("--port", type=int, default=PORT, help="default: %(default)s")
    page = commands.add_parser("web", help="open the web page, making a token to sign in with the first time")
    page.add_argument("--new", action="store_true", help="replace the token, if the old one is lost")
    # Whoever runs this is at the Mac, and can delete these files anyway.
    page.add_argument("--scope", choices=tokens.SCOPES, default=tokens.ADMIN, help="for a new token; default: %(default)s")
    page.add_argument("--port", type=int, help="where mia serve is, if it isn't the service's or the usual port")
    keeper = commands.add_parser("service", help="keep mia serve running, starting at login")
    steps = keeper.add_subparsers(dest="action")
    setup = steps.add_parser("install", help="start it now and at every login; installing again replaces it")
    setup.add_argument("--port", type=int, default=8765, help="default: %(default)s")
    steps.add_parser("stop", help="stop it until the next login, or until you start it")
    steps.add_parser("start", help="start it again after stopping it")
    steps.add_parser("restart", help="run the mia that's installed now, as after upgrading")
    steps.add_parser("uninstall", help="stop it and remove it, keeping your images and tokens")
    steps.add_parser("status", help="whether it's running; the same as no action")
    reach = commands.add_parser("remote", help="let your other devices reach mia, over HTTPS on your own network")
    switches = reach.add_subparsers(dest="action")
    turn_on = switches.add_parser("on", help="turn it on, or change its names; mia serve has to start again to see it")
    turn_on.add_argument("--name", action="append", metavar="NAME", help="a name devices use for this Mac, such as my-mac.local; repeatable; default: its Bonjour name")
    turn_on.add_argument("--address", action="append", metavar="ADDRESS", help="an address devices use for this Mac; repeatable; default: its current one")
    turn_on.add_argument("--port", type=int, help="default: the one it has, or 8766")
    turn_on.add_argument("--new-authority", action="store_true", help="replace the certificate authority: every device has to trust the new one")
    switches.add_parser("off", help="turn it off, keeping the certificate authority; mia serve has to start again to see it")
    switches.add_parser("authority", help="print the certificate authority, for your other devices to trust; it holds no secret")
    switches.add_parser("status", help="whether it's on; the same as no action")
    assistant = commands.add_parser("mcp", help="connect an AI assistant to mia serve, over stdin and stdout")
    assistant.add_argument("--url", default=os.environ.get("MIA_URL", "http://127.0.0.1:8765"), help="where mia serve is; default: %(default)s")
    assistant.add_argument("--ca", default=os.environ.get("MIA_CA"), metavar="FILE", help="the certificate authority to trust, from mia remote authority")
    worker = commands.add_parser("worker", help="run jobs from the queue; mia serve starts this itself")
    worker.add_argument("--parent", type=int, help=argparse.SUPPRESS)
    device = commands.add_parser("token", help="the tokens that devices use with the API")
    actions = device.add_subparsers(dest="action")
    create = actions.add_parser("create", help="make a token, shown once")
    create.add_argument("--for", dest="device", required=True, metavar="NAME", help="the device it's for")
    create.add_argument("--scope", choices=tokens.SCOPES, default=tokens.GENERATE, help="default: %(default)s")
    create.add_argument("--days", type=int, help="stop working after this many days; default: until revoked")
    actions.add_parser("list", help="the devices with tokens; the same as no action")
    revoke = actions.add_parser("revoke", help="stop a device's token working")
    revoke.add_argument("device", metavar="NAME")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor.run()
        if args.command == "generate":
            return _generate(args)
        if args.command == "history":
            return _history()
        if args.command == "token":
            return _token(args)
        if args.command == "serve":
            from mia.server import serve

            return serve(args.port)
        if args.command == "web":
            return _web(args)
        if args.command == "service":
            return _service(args)
        if args.command == "remote":
            return _remote(args)
        if args.command == "mcp":
            return _mcp(args.url, args.ca)
        if args.command == "worker":
            from mia.worker import run

            return run(args.parent)
        return _models_pull(args) if args.action == "pull" else _models_list()
    except (Cancelled, KeyboardInterrupt) as stopped:
        print(f"Stopped, and no image was kept. {stopped}".strip(), file=sys.stderr)
        return EXIT_STOPPED
    except (MiaError, OSError) as problem:
        print(f"mia: {problem}", file=sys.stderr)
        return 1
