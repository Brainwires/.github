# Brainwires Studio

**High-performance AI infrastructure for general use, research, code intelligence, and multi-provider API access.**

Brainwires Studio is a unified platform combining a web app, CLI/TUI, and browser extension—all powered by custom Rust engines compiled to WebAssembly. The public repositories here represent a curated slice of the full platform; more components will be open-sourced as the project matures.

---

## Web App (Closed Source)

A **Next.js 16 Progressive Web App** powering [brainwires.studio](https://brainwires.studio).

### Core Features

- **Multi-Provider AI Chat** — OpenAI, Anthropic Claude, Google Gemini, Mistral, Groq with streaming responses
- **Workspace Management** — Team workspaces with role-based access control
- **Rich Text Support** — Lexical-based editor with math rendering (KaTeX/MathJax)
- **Private RAG** — Client-side document search using local embeddings
- **MCP Integration** — Model Context Protocol servers for extended AI capabilities
- **Internationalization** — Full i18n support with i18next
- **PWA & Offline** — Service worker caching, installable app

### Tech Stack

`Next.js 16` | `React 19` | `TypeScript` | `Supabase` | `Tailwind CSS` | `shadcn/ui`

---

## Rust Submodules

The studio integrates several high-performance Rust modules compiled to WebAssembly:

### Computational Engine

A comprehensive mathematical and scientific computing library providing **400+ operations** through a unified **10-tool API**.

- Custom Computer Algebra System (CAS) with no Python dependencies
- 1,685 tests with 100% pass rate and 83%+ code coverage
- Covers 24+ mathematical domains: calculus, linear algebra, tensor calculus, differential equations, quantum mechanics
- Multiple interfaces: Native Rust API, JSON/MCP Server, WebAssembly

**Tools:** `Solve` | `Differentiate` | `Integrate` | `Analyze` | `Simulate` | `Compute` | `Transform` | `FieldTheory` | `Sample` | `Optimize`

### Thalora Web Browser

A **full-featured pure Rust headless browser** designed for AI agents.

- Real Chrome 131 mimicking with complete browser fingerprinting
- Advanced JavaScript engine (Boa) with ES2017-2025 support
- 50+ modern Web APIs: WebRTC, WebAssembly, Service Workers, WebGL, Media APIs
- AI Memory Heap for persistent storage across context compression
- 17+ MCP tools with Chrome DevTools Protocol integration
- Single binary deployment — no Chrome/Chromium dependencies

### Tool Orchestrator

Universal **Programmatic Tool Calling** implementation for any LLM.

- 37-98% token reduction via Rhai script orchestration
- Model-agnostic: works with Claude, OpenAI, Gemini, local models
- Sandboxed execution with configurable safety limits
- Native Rust + WASM (browser/Node.js) targets

### Local LLM

Client-side LLM inference engine with hybrid local/cloud support.

- WebGPU acceleration (80% native performance)
- Intelligent fallback: WebGPU → WASM → Cloud
- Smart storage with File System API + Cache API
- Curated model catalog (1B-8B params) with q4/q8 quantization

### Vector Database

High-performance client-side vector database.

- WASM-accelerated with near-native performance
- HNSW index for approximate nearest neighbor search
- Automatic IndexedDB persistence
- Cosine similarity, Euclidean distance, dot product

---

## Browser Extension

**Brainwires Extension** — Client-side compute for brainwires.studio.

- **Offload heavy computation** — Run tools locally instead of paying server costs
- Run AI models locally with WebGPU acceleration
- 400+ math operations via Computational Engine WASM
- Web scraping and content extraction via Thalora
- Chrome (Manifest V3) and Firefox support
- Zero telemetry — all compute stays on your machine

---

## Brainwires CLI

An **AI-powered agentic CLI tool** for autonomous coding assistance.

- Multi-agent architecture with task decomposition
- Rich tool system: file ops, bash, git, web operations
- MCP client for extended functionality
- Multiple modes: interactive, single-shot, batch, TUI, MCP server
- Infinite context via entity extraction and semantic search
- Task management with dependencies and time tracking
- Plan branching, templates, and semantic search

---

## Public Repositories

### [Compute Engine](https://github.com/Brainwires/compute-engine)

The standalone computational engine — available for use in your own projects.

### [Project RAG](https://github.com/Brainwires/project-rag)

A Rust-based MCP server providing AI assistants with RAG capabilities for understanding massive codebases.

- Hybrid search: vector similarity + BM25 keyword matching
- AST-based semantic chunking for 12+ languages
- 40+ file types with automatic PDF-to-Markdown
- Git history search with on-demand indexing

### [Tool Orchestrator](https://github.com/Brainwires/tool-orchestrator)

Universal Programmatic Tool Calling for any LLM — implement Anthropic's PTC pattern with any model.

- 37-98% token reduction via Rhai script orchestration
- Model-agnostic: Claude, OpenAI, Gemini, local models
- Sandboxed execution with configurable safety limits
- Native Rust + WASM (browser/Node.js) targets

---

## Philosophy

- **Performance First** — All core projects are written in Rust for maximum speed and safety
- **Local-First** — No external API dependencies; everything runs on your machine
- **AI-Native** — Built for seamless integration with AI assistants via MCP
- **Full-Stack Ownership** — We build the browser, the compute engine, the CLI, and the web app

---

## Connect

- **Website:** [brainwires.studio](https://brainwires.studio)
- **YouTube:** [@BrainwiresCortex](https://youtube.com/@BrainwiresCortex)
- **Business Inquiries:** support@brainwires.net

---

<sub>*Building tools that think alongside you.*</sub>
