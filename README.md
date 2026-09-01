# Loose Ends

Your conversations are full of things you said you'd do. Loose Ends reads
them — iMessage, Mail, Calendar — finds the threads you dropped, and
hands them back to you on your phone: the reply you never sent, the thing you
promised to buy, the plan that stalled.

It does this without your data ever leaving your machine.

## How it's built

- **The engine** (`backend/`) runs on your own Mac. It ingests your messages,
  extracts commitments and open loops with an LLM (your own Anthropic API
  key), ranks them, and serves a local API. Your messages, your database,
  your key — all on your hardware.
- **The app** (`ios/`) is a pure client. It pairs with your engine over your
  local network (QR code + one-time token) and shows you what's open.
- **The push relay** (`relay/`) is the one hosted piece, and it is
  content-free by construction: its API has no title or body field. It only
  ever tells your phone "wake up, ask your engine" — the words come from
  your Mac, never through our server.

Your mail, messages and calendar are read from the copies macOS already
keeps on your Mac, behind the one Full Disk Access grant it asks for. There
is no OAuth client, no cloud project, no consent screen, and nothing for
anyone to subpoena but you.

## Install the engine

```sh
git clone https://github.com/jayjay28/loose-ends.git
cd loose-ends/backend && ./deploy/install.sh
```

The installer sets up a launchd job and opens the setup wizard in your
browser. The wizard walks the whole path — Full Disk Access for Messages
and Mail, then your Anthropic key — checking each step by actually doing it,
and ends with a QR code the app scans.

## The iPhone app

The engine is only half of it — the app is where loose ends come back to you.
Get it at **https://clyon.dev/app** (TestFlight, while it waits on review).

## Try it without your data

```sh
cd backend && python -m lifeline.cli demo
```

Loads a fictional sample corpus (`backend/sample_data/`) and runs the full
pipeline over it. Every name, number, and email in the fixtures and tests
is invented — the cast (Alex Carter, Tess, Dev, Priya…) lives entirely in
this repo.

## Develop

```sh
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest        # 800+ tests, seconds
```

The iOS project is generated with [XcodeGen](https://github.com/yonaskolb/XcodeGen):
`cd ios && xcodegen` — then set your own team for device builds. The full
product spec is in [docs/SPEC.html](docs/SPEC.html); the long-term direction
in [VISION.md](VISION.md).

## License

[MIT](LICENSE).
