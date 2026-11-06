# The web page

Make pictures, watch the queue, and browse everything you've made, from a browser on your Mac or your phone.

The page isn't in a release yet, and the README's steps install the last release. To try the page now, leave out `--branch v0.4.0`
when you clone.

## Start here

You need mia installed and a model downloaded: the [README](../README.md) has both steps. Start the server, which keeps running
until you stop it with Control-C. To keep it running without a window open, see [Keeping mia running](service.md).

```sh
uv run mia serve
```

Then, in another window:

```sh
uv run mia web
```

The first time, that makes a token, shows it once, and copies it to your clipboard. It then opens the page. Paste the token, and sign
in. Type a prompt, and press Generate. Keep the token somewhere safe if you'll want it for another browser. If it's lost,
`uv run mia web --new` replaces it. That token can do everything, including delete pictures: add `--scope generate` for one that can't.

## On your phone

mia takes connections from other devices only over HTTPS, and your phone has to be told to trust it. You do this once. These steps
haven't been tried on a real phone yet, so the names of the settings may differ.

You need [the service](service.md) installed. On the Mac, turn on HTTPS, make a token, and save mia's authority to a file:

```sh
uv run mia remote on
uv run mia service restart
uv run mia token create --for my-phone
uv run mia remote authority > mia-authority.crt
```

`mia remote on` prints the addresses mia answers at, such as `https://my-mac.local:8766`, and a long fingerprint. Send
`mia-authority.crt` to your phone, by AirDrop or email. It holds no secret.

**On an iPhone or iPad:**

1. Open the file, and choose to allow the profile download.
2. In Settings, open **Profile Downloaded**, then **Install**.
3. In Settings, go to **General**, **About**, then **Certificate Trust Settings**, and turn on mia's authority.

**On Android:** the place varies. In Settings, search for "CA certificate", choose to install one, and pick the file.

Then open the address in the phone's browser, paste the phone's token, and sign in. Universal Clipboard, AirDrop, or a password
manager saves typing it. To have the page on your home screen, use your browser's **Add to Home Screen**.

**On another Mac:** open the file in Keychain Access, and set it to **Always Trust**. Firefox may keep its own list. If it
complains, import the file under **Settings**, **Privacy & Security**, **View Certificates**, **Authorities**.

## Using it

- **New picture.** The prompt is all you need. **Pictures** makes up to 8 at once, each with its own seed, and each waits its
  turn in the queue. **Advanced** has the seed and steps, and **Custom** in the size menu lets you choose a width and height, in
  multiples of 16. With a seed and several pictures, the seeds count up from yours, so the same settings make the same set.
- **The finished picture.** A picture that has just finished shows large, with **New variation**, which makes another with the same
  settings and a new seed. If the tab is in the background, its title counts what's finished.
- **Queue.** One job runs at a time, and the rest wait. Each lit tile is a step that has started. The last step takes longest,
  because it also turns the result into a picture. **Stop** ends a running job at its next step, and **Remove** takes out a
  waiting one. A job that failed stays listed for an hour, with the reason.
- **Gallery.** Pictures from every device appear here, newest first. Search looks through the prompts.
- **One picture.** Press a picture to see its settings, with **New variation**, **Copy prompt**, and **Download**. **Use these settings**
  copies them to the form, including the seed, so Generate makes the same picture again. **Previous** and **Next**, or the arrow keys,
  move between pictures.
- **The keyboard.** In the prompt box, Command-Return or Control-Enter is the same as pressing Generate.
- **Status.** The line under the queue says whether the worker is running, and how much disk is free.
- **Narrow screens.** **Make** and **Gallery** switch between the form and the pictures.
- **Your choices.** This browser remembers the model, size, and number of pictures you last used, but never the prompt.

## What your token allows

| Scope | What the page lets you do |
|---|---|
| `read` | Look at the queue and gallery |
| `generate`, the default | Also make pictures and stop jobs |
| `admin` | Also delete pictures |

Deleting a picture takes two presses, and can't be undone. Add `--scope admin` when you make a token to allow it.

## Good to know

- **Staying signed in.** A browser stays signed in for 90 days, or until the token's end date if that's sooner. **Sign out** ends it
  on that browser. `uv run mia token revoke my-phone` ends it on every browser that used the token.
- **Tokens stay out of the browser.** The page swaps yours for a sign-in that scripts can't read, and forgets the token. You'll need
  a token again after 90 days, so keep it in a password manager, or make a new one.
- **Everyone shares one history.** Anyone with a token sees every prompt and picture, so give tokens only to people you'd share
  them with.
- **A model that isn't downloaded** says so in the menu, and the page tells you the command if you choose it.
- **Browsers it needs.** Current Safari, Chrome, and Firefox. A browser that doesn't send `Sec-Fetch-Site`, such as Safari before 16.4,
  can't use the page.

## If it doesn't work

[Reaching mia from your other devices](remote.md) has more on HTTPS, and on how to choose its names and addresses.

- **The browser warns about the connection.** The device doesn't trust mia's authority yet, or the address isn't one that
  `mia remote status` lists. See [If it doesn't connect](remote.md#if-it-doesnt-connect).
- **"Too many refused requests."** After 30 wrong tokens in a minute, that address waits a minute.
- **"Too many requests" or pictures missing.** A device has 240 requests a minute, and each picture is one. Pictures try again by
  themselves.
- **Signed out unexpectedly.** The 90 days ran out, or the token was revoked or ended.

## Under the hood

The page is three small files and the logo, with no build step, in `src/mia/web`. They use the same [REST API](api.md) as any other program,
with the [session cookie](api.md#signing-in-from-a-browser) in place of a token. A strict content security policy stops the page
running anything but its own script or loading anything from elsewhere.
