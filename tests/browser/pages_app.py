"""Native browser pages served for visual and CSP integration checks."""

from contextlib import asynccontextmanager

from pyfly.container import controller
from pyfly.context.application_context import ApplicationContext
from pyfly.core.config import Config
from pyfly.web import ModelAndView, SecurityHeadersConfig, get_mapping
from pyfly.web.adapters.starlette.app import create_app
from pyfly.web.adapters.starlette.security_headers import SecurityHeadersMiddleware


@controller
class DemoPages:
    @get_mapping("/example-error")
    async def example_error(self) -> ModelAndView:
        raise RuntimeError("The inventory service could not complete this request.")


context = ApplicationContext(
    Config(
        {
            "pyfly": {
                "web": {
                    "templates": {"enabled": True, "directories": []},
                    "errors": {"html-enabled": True, "max-stack-frames": 6},
                    "debug": True,
                }
            }
        }
    )
)
context.register_bean(DemoPages)


@asynccontextmanager
async def lifespan(app):
    await context.start()
    try:
        yield
    finally:
        await context.stop()


app = SecurityHeadersMiddleware(
    create_app(context=context, lifespan=lifespan, docs_enabled=False),
    SecurityHeadersConfig(content_security_policy="default-src 'self'; style-src 'self'; script-src 'none'"),
)
