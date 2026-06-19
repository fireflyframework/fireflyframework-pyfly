# Contributing to PyFly

Thanks for your interest in improving PyFly — the official Python implementation of the
[Firefly Framework](https://github.com/fireflyframework). This guide covers everything you
need to make a change and open a pull request.

## Ground rules

- **Be respectful and constructive.** Assume good intent.
- **Keep changes focused.** One logical change per pull request; small PRs review faster.
- **Add tests for new behaviour** and a note in `CHANGELOG.md` under the next version.
- By contributing, you agree your work is licensed under **Apache-2.0** (see [`LICENSE`](LICENSE)).

## Development setup

PyFly targets **Python 3.12+** and uses [uv](https://github.com/astral-sh/uv) (pip also works).

```bash
git clone https://github.com/fireflyframework/fireflyframework-pyfly.git
cd fireflyframework-pyfly

# Install all extras + dev tooling
uv sync --all-extras --group dev
# (or: python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[full]")
```

## The quality gates (what CI runs)

Every pull request must pass the same gates CI enforces. Run them locally first:

```bash
uv run ruff format .        # format
uv run ruff check .         # lint
uv run mypy src             # type-check (strict)
uv run pytest               # tests
```

- **Formatting & linting:** [Ruff](https://docs.astral.sh/ruff/).
- **Typing:** [mypy](https://mypy-lang.org/) in **strict** mode — every public surface is fully annotated. If it compiles, it's consistent.
- **Tests:** [pytest](https://docs.pytest.org/). Add tests next to the code they cover under `tests/`, and prefer fast, isolated unit tests; integration tests that need real infrastructure (Postgres, Kafka, Redis, …) live behind the Docker Compose stack (`docker-compose.yml`).

## Making a change

1. **Branch from `main`:** `git switch -c feat/short-description` (or `fix/…`, `docs/…`).
2. Make your change with tests.
3. Run the quality gates above until green.
4. Add a `CHANGELOG.md` entry under the next version heading (we use [Keep a Changelog](https://keepachangelog.com/)).
5. **Commit** with a clear, imperative message (Conventional-Commits style is welcome: `feat:`, `fix:`, `docs:`, `refactor:`, `test:`).
6. **Open a pull request** against `main` describing the what and the why.

## Versioning & releases

PyFly uses **Calendar Versioning** ([CalVer](https://calver.org/)) — `YY.MM.PATCH` — to stay
aligned with the rest of the Firefly Framework family (Java, .NET, Go, Rust). Releases are
published via [GitHub Releases](https://github.com/fireflyframework/fireflyframework-pyfly/releases)
(PyFly is **not** published to PyPI). Maintainers cut releases; you don't need to bump the
version in your PR — just add the changelog entry.

## Documentation & the book

- Framework docs live under [`docs/`](docs/README.md) — keep them in sync with code changes.
- The project-driven book, *PyFly by Example*, lives under [`book/`](book/); its figures and
  cover are generated (see [`assets/README.md`](assets/README.md) for the shared brand system).

## Questions

Open a [discussion or issue](https://github.com/fireflyframework/fireflyframework-pyfly/issues).
Thank you for contributing! 🐍✨
