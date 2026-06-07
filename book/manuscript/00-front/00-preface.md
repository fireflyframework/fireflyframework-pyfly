## Preface

Enterprise Python has long meant stitching together a dozen independent libraries — one for dependency injection, another for routing, yet another for async database access — with no shared idiom to bind them. PyFly changes that. It brings the same cohesive, convention-over-configuration experience that Spring Boot gave the Java world, but built from the ground up for Python 3.12+ and the async ecosystem.

This book teaches PyFly by doing. You will build a real, production-shaped application from scratch, making every concept concrete before moving to the next.

### Who This Book Is For

This book is for intermediate Python developers who are comfortable with `async`/`await`, type hints, and the basics of HTTP services. You do not need prior framework experience — if you have built anything with FastAPI, Flask, or SQLAlchemy you are well prepared.

Spring Boot developers will feel especially at home. Wherever PyFly mirrors a Spring concept — beans, `@Component`, `@Transactional`, application events — a **Spring parity** callout draws the parallel explicitly, so you can map what you already know rather than learning from zero.

### What You Will Build

Every chapter advances **Lumen**, a fintech digital-wallet and ledger platform. The journey follows a deliberate arc:

- **Chapters 1–4 (Foundations):** You scaffold the first Lumen service, wire up PyFly's DI container, and expose your first REST endpoints.
- **Chapters 5–8 (Modeling & Persisting):** Domain modeling, DDD aggregates, CQRS command/query handlers, and async R2DBC persistence land in sequence. By the end, Lumen tracks wallet balances in a real database.
- **Chapters 9–12 (Event-Driven):** The ledger publishes domain events; you add event sourcing so every balance change is a first-class fact, not a silent update.
- **Chapters 13–15 (Into Microservices):** Lumen splits into three services — Wallet, Payments, and Notifications. A cross-service transfer saga coordinates the distributed transaction safely.
- **Chapters 16–18 (Secure · Observe · Ship):** OAuth 2.0 / JWT security, distributed tracing, structured metrics, and a container-ready production configuration round out the platform.

At the end you have a working, observable, secured microservices system — and the mental model to extend it.

### How This Book Is Organized

The book is divided into five parts that match the arc above:

**Part I — Foundations** introduces PyFly's philosophy, its project layout, and the DI container. You build the skeleton of Lumen and run your first integration test.

**Part II — Modeling & Persisting** covers domain modeling with PyFly's aggregate support, persistence via the reactive R2DBC session factory, and the CQRS handler pattern.

**Part III — Event-Driven** moves from request/response to event-driven communication. You learn the event bus, domain events, and event sourcing with PyFly's event store.

**Part IV — Into Microservices** guides you through decomposing Lumen into independent deployable services, configuring the service registry, and implementing the transfer saga.

**Part V — Secure · Observe · Ship** secures every endpoint, adds OpenTelemetry traces and Prometheus metrics, and produces a hardened, container-ready deployment.

### How to Use This Book

Read sequentially. Each chapter builds on the one before, and the Lumen codebase grows incrementally — skipping ahead will leave context gaps.

Type every listing yourself. The act of reading and typing code simultaneously is how the patterns stick. Do not copy-paste from the companion repository until you have written each listing at least once.

Each chapter ends with a **Recap** that summarises what changed in the Lumen codebase, and a set of **Exercises** that push you one step further. Completing the exercises is optional, but highly recommended for anything you intend to apply immediately.

### Conventions in Brief

Typographic and structural conventions — code listing captions, callout types, and figure numbering — are demonstrated, with live examples, in the **Conventions** section that follows.

### Downloading the Example Code

The complete Lumen source code, chapter by chapter, is available with this book. Each chapter folder contains a working snapshot of the project at the end of that chapter, so you can compare your work or catch up if you fall behind. Refer to the book's companion resources for download instructions.
