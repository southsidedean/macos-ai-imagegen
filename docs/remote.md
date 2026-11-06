# Reaching mia from your other devices

Your laptop, a script on another computer, or an AI assistant running elsewhere can use mia over your own network, with
HTTPS. It's off until you turn it on. It needs no extra software and no port opened on your router, and it won't take
connections from the internet.

## Start here

You need mia installed, a model downloaded, and [the service](service.md) installed. On the Mac that runs mia, turn it on,
restart the service, and make a token for the other device:

```sh
uv run mia remote on
uv run mia service restart
uv run mia token create --for my-laptop
uv run mia remote authority > mia-authority.pem
```

`mia remote on` prints the addresses mia now answers at, such as `https://my-mac.local:8766`, and a long fingerprint.
Restarting fails a job that's running, and jobs that are waiting carry on. If you run `mia serve` by hand instead of the
service, stop it and start it again.

Copy `mia-authority.pem` to the other device, by any means you like. It holds no secret. On that device, check that the
file is the one your Mac made, by comparing this fingerprint with the one the Mac printed:

```sh
openssl x509 -in mia-authority.pem -noout -fingerprint -sha256
```

To use mia from a phone's browser, follow [the web page guide](web.md#on-your-phone) instead. For a script or a laptop, ask
mia for its health, and for its status, which needs the token:

```sh
curl --cacert mia-authority.pem https://my-mac.local:8766/v1/health
curl --cacert mia-authority.pem https://my-mac.local:8766/v1/status -H "Authorization: Bearer paste-your-token-here"
```

If the first command can't find `my-mac.local`, use the address that `mia remote on` printed. Everything in the
[API guide](api.md) now works from that device, with this address and `--cacert`.

## Connecting an AI assistant

With Claude Code on the other device, add mia, and start Claude Code with `NODE_EXTRA_CA_CERTS` pointing at the file:

```sh
claude mcp add --transport http mia https://my-mac.local:8766/mcp --header "Authorization: Bearer paste-your-token-here"
NODE_EXTRA_CA_CERTS=/path/to/mia-authority.pem claude
```

Claude Code needs that setting every time it starts, so you may want to set it in your shell's profile.

An assistant that only runs local commands can use `mia mcp` instead, which needs a copy of mia on that device. It works
on Linux too, and doesn't need the image engine: clone this repository, install [uv](https://docs.astral.sh/uv/), and run
`uv sync --locked`. Then:

```sh
claude mcp add mia -e MIA_TOKEN=paste-your-token-here -- uv run --directory /path/to/macos-ai-imagegen mia mcp --url https://my-mac.local:8766 --ca /path/to/mia-authority.pem
```

`--ca` can also come from `MIA_CA`. With it, `mia mcp` trusts that file and nothing else. The [assistant guide](mcp.md) says
what the assistant can do, and what it can see.

## Options

`mia remote on` uses the Mac's Bonjour name and its current address. To choose instead, name each one. Giving either means
only what you gave is used:

```sh
uv run mia remote on --name my-mac.local --address 192.168.1.20
```

Both can be repeated. `--port` chooses another port, by default 8766, and mia keeps it until you choose another. Running
`mia remote on` again replaces the names and addresses and makes a new certificate, which takes effect when mia restarts.

To see how it stands, or turn it off:

```sh
uv run mia remote status
uv run mia remote off
uv run mia service restart
```

`mia doctor` reports it too, and warns if it's on but not listening. Turning it off keeps the authority, so your devices won't have to trust a new one if you turn
it on again.

## If it doesn't connect

- **The name isn't found.** `.local` names need Bonjour on the other device. Macs and iPhones have it, and some Linux
  machines don't. Use the address, or add a name to your own DNS or to that device's hosts file.
- **Your Mac's address changed.** Run `mia remote on` again, which finds the new one, or give it every `--name` and
  `--address` you use, because the ones you leave out stop working. Then restart mia. Your devices keep trusting it.
  To stop this happening, reserve an address for the Mac in your router's settings, or use the name.
- **The certificate is refused.** The device has to be told about `mia-authority.pem`, and the address you use has to be one
  that `mia remote status` lists.
- **Nothing answers.** `mia remote status` should say `listening now`. If HTTPS can't start, mia keeps running for this
  Mac alone, and its log says why, so look there if it says `not listening` after a restart. A sleeping Mac can't answer, and mia can't wake it:
  see [Good to know](service.md#good-to-know) in the service guide.
- **The firewall.** If the macOS firewall is on, macOS may ask whether Python can accept incoming connections, or block it
  while you're away from the screen. Check System Settings, under Network, then Firewall. mia hasn't been tested with the
  firewall on yet.
- **Too many refusals.** After 30 refused requests in a minute, usually from a wrong token, that address has to wait a minute.

## Good to know

- **The Mac has to be awake.** mia keeps it awake during a job, but a sleeping Mac can't take a new request, and mia can't wake
  it. If requests should arrive at any time, stop it sleeping in System Settings.
- **Tokens are for devices you trust.** All tokens share one queue and history, so anyone with a token can see every prompt.
  Use `--scope read` for a device that should only look, `--days` for a token that should run out, and `mia token revoke`
  to stop one.
- **Your own network only.** mia drops connections from anything but this Mac and the private address ranges, so a port
  forwarded on your router normally wouldn't get through. That also rules out a VPN like Tailscale for now, which uses other
  addresses. It does mean any private network the Mac joins, so turn this off if you take the Mac somewhere you don't trust.
- **What strangers on your network can see.** Without a token they can read `/v1/health`, which gives mia's version,
  `/openapi.json`, which lists its routes, and the web page's own files, which hold nothing of yours.
- **IPv4 only,** for now. mia doesn't listen on IPv6, so a device on a network with nothing but IPv6 can't reach it.

## Under the hood

- **Files.** `remote` in mia's folder holds the authority, the certificate, their keys, and the settings, and only you can
  read it. Back it up with care: `authority.key` can make certificates your devices will trust.
- **What the authority can vouch for.** Only `.local` names and the other names you gave when it was made, so someone who stole
  its key couldn't pose as `bank.example` to your devices. Using a name it doesn't cover means replacing it with
  `mia remote on --new-authority`, and every device trusting the new one. It can't be limited by address, because
  some clients, including Claude Code, then refuse its certificates. So a stolen key could pose as a bare address, such as
  `https://203.0.113.5`.
- **How long they last.** The authority lasts 10 years. The certificate lasts 397 days, and mia makes a new one by itself
  within 30 days of its end. Devices don't notice.
- **The connection.** TLS 1.3 only. Plain HTTP stays on this Mac, on port 8765, and the names you chose aren't accepted there.
- **Limits.** A handshake has 5 seconds. At most 16 can be in progress from one address and 64 overall, so that strangers
  can't tie up the Mac before they've sent anything. After that, the usual [limits](api.md#limits) apply.
