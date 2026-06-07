## Preface

Enterprise Python has long meant stitching together a dozen independent libraries — one for dependency injection, another for routing, yet another for async database access — with no shared idiom to bind them. **PyFly** changes that. It brings the cohesive, convention-over-configuration experience that Spring Boot gave the Java world, rebuilt from the ground up for Python 3.12+ and `async`/`await`.

This book teaches PyFly **by doing**. You build one real application from an empty folder to a secured, observable, event-driven service — making every concept concrete before moving to the next. Crucially, the code in these pages is not illustrative pseudocode: it is taken from a **real project that compiles, boots, and passes its tests** against PyFly v26.6.60. Every listing was verified against the running sample, so what you read is what actually works.

### Who This Book Is For

This book is for intermediate Python developers comfortable with `async`/`await`, type hints, and the basics of HTTP services. You need no prior framework expertise — if you have built anything with FastAPI, Flask, or SQLAlchemy, you are well prepared.

Spring Boot developers will feel especially at home. Wherever PyFly mirrors a Spring concept — beans, stereotypes, declarative transactions, application events — a **Spring parity** callout draws the parallel explicitly, so you map what you already know rather than learning from zero.

### What You Will Build

Every chapter advances **Lumen**, a digital-wallet and ledger service. The journey follows a deliberate arc, one part at a time:

- **Part I — Foundations (Chapters 1–4).** You scaffold the first Lumen service with `pyfly new`, run it under an ASGI server, wire PyFly's dependency-injection container, bind typed configuration and profiles, and expose your first validated REST endpoints.
- **Part II — Modeling & Persisting (Chapters 5–7).** You introduce the repository pattern over a port, persist wallets with async SQLAlchemy (SQLite, no infrastructure required), model the domain with a `Money` value object and a `Wallet` aggregate, and split reads from writes with CQRS command and query handlers dispatched through a bus.
- **Part III — Event-Driven (Chapters 8–10).** The aggregate raises domain events; a listener projects them; an **event-sourced ledger** rebuilds every balance by replaying its event stream; and the same events flow out to Kafka or RabbitMQ for other services.
- **Part IV — Into Microservices (Chapters 11–13).** Lumen reaches beyond its own process: a typed HTTP client calls an external Payments service, an orchestrated **transfer saga** moves money across wallets and *compensates* when a step fails, and caching plus resilience patterns keep the system fast and fault-tolerant.
- **Part V — Secure · Observe · Ship (Chapters 14–18).** You secure the endpoints with JWT and `@secure`, make the service observable with metrics, tracing, health checks, and the admin dashboard, test the whole stack, connect it to the outside world with scheduling, notifications, and webhooks, and finally extend and ship it to production.

By the last page you have a working, tested, observable, secured service — and the mental model to extend it.

### How to Use This Book

**Read sequentially.** Each chapter builds on the one before, and the Lumen codebase grows incrementally; skipping ahead leaves gaps.

**Type every listing yourself.** Reading and typing code at the same time is how the patterns stick. Resist copy-pasting until you have written each listing at least once.

**Run it.** Lumen really runs — `uv run pyfly run` boots the service and `uv run --extra dev pytest` exercises it. Whenever a chapter adds a feature, start the app or the tests and watch it work. Seeing real JSON come back from a real endpoint is worth a hundred diagrams.

Each chapter closes with a **Recap** of what changed in the Lumen codebase and a set of **Exercises** that push one step further. The exercises are optional but recommended for anything you intend to apply immediately.

### Conventions in Brief

Typographic and structural conventions — code-listing captions, callout types, and figure numbering — are demonstrated, with live examples, in the **Conventions** section that follows.

### The Companion Code

The complete, runnable Lumen project lives in the framework's `samples/lumen` directory. It is a single, layered PyFly project — `interfaces`, `models`, `core`, `web` — that you grow chapter by chapter; the finished source there is the destination this book walks you to. Set it up once with `uv sync`, and use it to compare your work, catch up if you fall behind, or simply run the parts you are reading about.
