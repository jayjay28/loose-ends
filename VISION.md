# Project Vision — Loose Ends

> The north star for the project: the *why* and the *where we're headed*,
> kept deliberately high-level and stable across sessions.

## Core objective

Transform Conversation Monitor from a single-machine local script into an
**open-source, plug-and-play framework**. Let anyone connect their own
communication channels (Gmail, Messages, CRMs, etc.) to automatically extract
actionable tasks and insights from ongoing, multi-channel conversations.

## Key intentions & strategic goals

- **Modular "plug-and-play" architecture** — third-party developers can write a
  plugin for any message source without touching the core engine.
- **Broad utility (individual → CRM)** — serve power users, professionals, and
  businesses managing high-volume communication across platforms who need
  centralized action-item tracking.
- **Open-source & reusable** — the core engine is an independent framework that
  can be embedded into other products, SaaS tools, or local workflows.
- **Cross-platform context awareness** — aggregate threads from disparate
  sources into one unified pipeline so context isn't lost when a conversation
  moves between platforms.

## How today's build relates

v1.0 (the iOS app + Python backend) is the **reference implementation** and
proof of the core loop: ingest → extract → rank → surface → act. The framework
vision generalizes that backend — ingestion sources become pluggable, the
engine becomes embeddable, and the iOS app becomes one of many possible clients.

_Last updated: 2026-08-02._
