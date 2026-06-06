# Brainwires

**High-performance, AI-native infrastructure written in Rust.**

We build tools that think alongside you — browser-resident model inference, an agent framework, a pure-Rust browser for AI, and a handful of focused libraries. Almost everything is Rust, much of it compiled to WebAssembly so it runs client-side with no server round-trip.

---

## Flagship Products

### [Rullama](https://github.com/Brainwires/rullama) ![globe](globe.svg)

**Browser-resident Gemma 4 inference in pure Rust → WebAssembly + WebGPU.**

Loads the same GGUF blobs Ollama already has on disk and runs the forward pass on your local GPU through hand-written WGSL — never touching a remote server. A PWA-pluggable inference engine, not a port of Ollama-the-server.

- Greedy output bit-identical to Ollama (`gemma4:e2b`, validated on desktop)
- Runs on-device: full Q4_K_M model streaming tokens on an iPhone 16e
- Vision + audio multimodal (ViT + Conformer towers on the same wgpu device)
- In-browser LoRA fine-tuning — backward kernels, Adam over GPU buffers, adapters export as safetensors
- Streaming load via HTTP byte-range or OPFS sync handles — the 7 GB GGUF never enters wasm linear memory in bulk

[rullama.com](https://rullama.com)

### [Brainwires Framework](https://github.com/Brainwires/brainwires-framework) ![globe](globe.svg)

**A modular Rust framework for building complete AI solutions.** A workspace of 32 framework crates (plus extras), each independently publishable to crates.io and composable through the `brainwires` facade crate. A subset is ported to Deno.

- **Multi-provider AI** — Anthropic, OpenAI, Google, Ollama, and local LLMs behind a unified `Provider` trait
- **Agent orchestration** — hierarchical task decomposition, multi-agent coordination, MDAP voting
- **MCP protocol** — full client and server support, exposing agents as MCP tools
- **Agent networking** — 5-layer protocol stack (IPC, TCP, A2A, Pub/Sub) with pluggable transports
- **Training** — cloud fine-tuning across 6 providers plus local LoRA/QLoRA/DoRA via Burn
- **RAG & code search** — AST-aware chunking, hybrid vector + keyword search, Git-aware indexing
- **Audio & security** — STT/TTS, encrypted storage (ChaCha20-Poly1305), permission policies

[`crates.io/crates/brainwires`](https://crates.io/crates/brainwires) · [`docs.rs/brainwires`](https://docs.rs/brainwires) · [brainwires.dev](https://brainwires.dev/)

### [Thalora](https://github.com/Brainwires/thalora-web-browser) ![globe](globe.svg)

**A full-featured pure-Rust headless web browser designed for AI agents.** Complete Chrome 131 compatibility with real JavaScript execution and modern web APIs — no Chrome or Chromium dependency.

- **Real JS engine** — Boa with ES2017–2025 support; no mocks, no fake timers
- **Modern Web APIs** — Fetch, WebSocket, WebRTC, WebAssembly, Service Workers, WebGL, Web Crypto, device APIs
- **AI-first** — persistent AI Memory Heap that survives context compression, 17+ MCP tools, Chrome DevTools Protocol
- **Single binary** — no dependencies, deploy anywhere; passes Google's anti-bot protection

[`crates.io/crates/thalora`](https://crates.io/crates/thalora) · [`docs.rs/thalora`](https://docs.rs/thalora) · 658 tests passing

---

## Smaller Projects

### [rsqlite-wasm](https://github.com/Brainwires/rsqlite-wasm) ![globe](globe.svg)

WASM implementation of SQLite with vector-database support, written in Rust. Powers the SQLite tooling across our browser-side stack.

### [opfs-extension](https://github.com/Brainwires/opfs-extension) ![globe](globe.svg)

**Brainwires OPFS** — a Chrome DevTools extension for browsing, editing, uploading, and exporting files in any origin's Origin Private File System. Includes a full SQLite IDE (schema browser, data grid, SQL console) powered by rsqlite-wasm. No telemetry, no remote code. Install with `npx brainwires-opfs`.

### [ratatui-interact](https://github.com/Brainwires/ratatui-interact) ![globe](globe.svg)

Interactive TUI components for [ratatui](https://github.com/ratatui/ratatui) — focus management (Tab/Shift+Tab), mouse click hit-testing, and a library of ready-to-use widgets (Input, Select, TreeView, FileExplorer, TabView, and more). [`crates.io/crates/ratatui-interact`](https://crates.io/crates/ratatui-interact)

### [ParolNet](https://github.com/Brainwires/parol-social) ![globe](globe.svg)

A secure, decentralized communication platform promoting free expression and open access to information — built for people living under authoritarian regimes. No phone number, email, or account; your identity is a cryptographic key that never leaves your device. Traffic camouflage, onion-routed conversations, panic wipe, and decoy mode. *(Active prototype — see the repo's `IMPLEMENTATION_STATUS.md`.)*

### [react-skia-pack](https://github.com/Brainwires/react-skia-pack) ![globe](globe.svg)

A library of high-performance, GPU-accelerated React components built on Google's Skia graphics engine via CanvasKit — for data visualization, interactive widgets, and custom graphics.

---

## Philosophy

- **Performance First** — core projects are written in Rust for speed and safety
- **AI-Native** — built for seamless integration with AI assistants via MCP
- **Client-Side** — WebAssembly + WebGPU keep compute on your machine, not a server

---

## Connect

- **Website:** [brainwires.net](https://brainwires.net)
- **YouTube:** [@BrainwiresCortex](https://youtube.com/@BrainwiresCortex)
- **Business Inquiries:** support@brainwires.net

---

<sub>*Building tools that think alongside you.*</sub>
