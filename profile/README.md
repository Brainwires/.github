# Brainwires Studio

**High-performance AI infrastructure for general use, research, code intelligence, and multi-provider API access.**

Brainwires Studio is a full-stack AI platform for research, coding, and agent automation, centered on three primary apps: the Next.js web experience, the Brainwires CLI/TUI, and the Thalora headless browser. The web app, CLI, and Thalora are closed source (the CLI and Thalora ship as binary-only releases); the open repositories focus on supporting engines, agent tooling, and selected infrastructure forks used in production.

---

## Web App (Closed Source)

A **Next.js 16 Progressive Web App** powering [brainwires.studio](https://brainwires.studio).

### Core Features

- **Multi-Provider AI Chat** — OpenAI, Anthropic Claude, Google Gemini, Groq with streaming responses
- **Workspace Management** — Team workspaces with role-based access control
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

A **full-featured pure Rust headless browser** designed for AI agents. **Binary-only, closed source.**

- Chrome 131 compatibility with real JavaScript execution
- Modern Web APIs (Fetch, WebRTC, WebAssembly, Service Workers, WebGL)
- AI Memory Heap for persistent context across sessions
- 17+ MCP tools plus Chrome DevTools Protocol integration
- Native and WASM builds, single-binary deployment

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

An **AI-powered agentic CLI tool** for autonomous coding assistance. **Binary-only, closed source.**

- Multi-agent architecture with orchestrator/worker roles
- Rich tool system: file ops, bash, git, web operations
- MCP client plus MCP server mode
- Interactive, single-shot, batch, and TUI chat modes
- Multiple output formats and quiet mode for scripting
- Planning mode, task management, plan branching/templates
- Infinite context memory with entity graphs and semantic search
- Remote control via Brainwires Studio web interface

---

## Public Repositories (All)

Below is the full list of public repos in the Brainwires org, with a short note on what they are and why they exist.

### [compute-engine](https://github.com/Brainwires/compute-engine)

**What:** Standalone computational engine with 400+ mathematical operations.
**Why:** Core math engine powering Brainwires tools, open for reuse and audits.

### [project-rag](https://github.com/Brainwires/project-rag)

**What:** Rust MCP server for RAG over large codebases.
**Why:** Provides semantic search + retrieval infrastructure for assistants.

### [tool-orchestrator](https://github.com/Brainwires/tool-orchestrator)

**What:** Programmatic Tool Calling (PTC) implementation for any LLM.
**Why:** Cuts token usage and enforces structured tool routing across models.

### [mcp-secure-shell](https://github.com/Brainwires/mcp-secure-shell)

**What:** Rust MCP server exposing SSH/SCP workflows as tools.
**Why:** Safe, auditable remote operations for agents and deployments.

### [lazarus-mcp](https://github.com/Brainwires/lazarus-mcp)

**What:** MCP server that can restart Claude Code during MCP updates.
**Why:** Solves self-restart deadlocks when an MCP updates itself.

### [ratatui-interact](https://github.com/Brainwires/ratatui-interact)

**What:** Interactive TUI primitives for Ratatui.
**Why:** Powers the Brainwires CLI/TUI UX with reusable components.

### [react-skia-pack](https://github.com/Brainwires/react-skia-pack)

**What:** GPU-accelerated React components built on Skia/CanvasKit.
**Why:** High-performance data visualization for web UIs.

### [selfhosted-supabase-mcp](https://github.com/Brainwires/selfhosted-supabase-mcp)

**What:** MCP server for self-hosted Supabase deployments.
**Why:** Bridges agent tooling with our internal Supabase stacks.

### [supabase-postgres-spock](https://github.com/Brainwires/supabase-postgres-spock)

**What:** Supabase Postgres fork with Spock multi-master replication support.
**Why:** Used for pgEdge deployments (Spock = multi-master replication).

### [supabase-cli](https://github.com/Brainwires/supabase-cli)

**What:** Supabase CLI fork (migrations, local dev, edge functions, backups).
**Why:** Maintained to support pgEdge/Spock workflows and custom patches.

### [supabase-supabase](https://github.com/Brainwires/supabase-supabase)

**What:** Supabase platform repo (dashboard, infra, tooling).
**Why:** Source-of-truth fork for pgEdge-specific integration work.

### [next-pwa](https://github.com/Brainwires/next-pwa)

**What:** Next.js PWA plugin fork.
**Why:** Maintains compatibility fixes and build tweaks for our web app.

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
