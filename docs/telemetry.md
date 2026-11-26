# Telemetry

pogo counts how many people use it. It's **on by default**, anonymous, and takes one command to
turn off:

```sh
pogo telemetry off
```

## Why

pogo is free and open source. A simple count of active installs is the only way its author can
tell whether it's worth maintaining and what to work on. That's all this is: a count.

## What is sent

When you run `pogo on`, and then once every 30 minutes while pogo keeps running, pogo sends:

| Field | Example | Why |
|---|---|---|
| a random install id | `9f2c…` (made up on your computer) | so two runs from one machine aren't counted as two people |
| pogo version | `0.3.0` | to see who has updated |
| operating system | `darwin` / `win32` | Mac vs PC split |
| CPU architecture | `arm64` / `AMD64` | which builds people run |
| mode | `all-apps` / `basic` | which mode people use |

## What is never sent

Your proxy address, username or password; the sites you visit; your files; your name; or your IP
address. Geo lookup is turned off and the IP is overwritten with `0.0.0.0` before the event is
stored, so the event is a count and nothing more. The code is [`pogo/telemetry.py`](../pogo/telemetry.py) —
the list of fields is fixed there, with no way for anything else to ride along.

It's best-effort: sent from a background thread with a short timeout, and every error is ignored.
It can't slow pogo down and it works the same offline.

## Turning it off

```sh
pogo telemetry            # show whether it's on, and exactly what's sent
pogo telemetry off        # stop counting this install
pogo telemetry on         # start again
```

Or set an environment variable, which wins over the saved setting:

- `DO_NOT_TRACK=1` — the [cross-tool standard](https://consoledonottrack.com)
- `POGO_NO_TELEMETRY=1` — pogo's own

`pogo uninstall` removes the setting and the install id along with everything else.

## Where it goes

Events go to [PostHog](https://posthog.com), a product-analytics service. The key compiled into
pogo is a public, write-only project key: it can send events, not read them. If no key is built
into your copy, telemetry does nothing at all.
