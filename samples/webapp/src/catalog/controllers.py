from starlette.exceptions import HTTPException
from starlette.requests import Request

from catalog.models import ProductForm
from pyfly.admin.data.models import AdminOperationContext, AdminQuery
from pyfly.admin.data.service import AdminDataService
from pyfly.container import controller
from pyfly.security.context import SecurityContext
from pyfly.web import Form, ModelAndView, PathVar, Redirect, exception_handler, get_mapping, post_mapping, reverse
from pyfly.web.forms import FormValidationException


@controller
class CatalogController:
    def __init__(self, data: AdminDataService) -> None:
        self.data = data

    @staticmethod
    def actor(request: Request) -> AdminOperationContext:
        security = getattr(request.state, "security_context", SecurityContext.anonymous())
        if not security.is_authenticated:
            raise HTTPException(401, "Sign in to the catalog", headers={"WWW-Authenticate": 'Basic realm="Catalog"'})
        return AdminOperationContext(security)

    @get_mapping("/", name="catalog")
    async def index(self, request: Request) -> ModelAndView:
        page = await self.data.list("products", AdminQuery(), self.actor(request))
        return ModelAndView("catalog.html", {"products": page.items})

    @get_mapping("/products/new", name="new_product")
    async def new(self, request: Request) -> ModelAndView:
        self.actor(request)
        return ModelAndView("form.html", {"values": {}, "errors": []})

    @post_mapping("/products/new")
    async def create(self, request: Request, product: Form[ProductForm]) -> Redirect:
        await self.data.create("products", product.model_dump(exclude={"edit_version"}), self.actor(request))
        return Redirect(reverse(request, "catalog"))

    @get_mapping("/products/{id}/edit", name="edit_product")
    async def edit(self, request: Request, id: PathVar[str]) -> ModelAndView:
        record = await self.data.get("products", id, self.actor(request))
        return ModelAndView("form.html", {"values": {**record.values, "edit_version": record.edit_token}, "errors": []})

    @post_mapping("/products/{id}/edit")
    async def update(self, request: Request, id: PathVar[str], product: Form[ProductForm]) -> Redirect:
        await self.data.update(
            "products", id, product.model_dump(exclude={"edit_version"}), product.edit_version, self.actor(request)
        )
        return Redirect(reverse(request, "catalog"))

    @exception_handler(FormValidationException)
    async def invalid(self, error: FormValidationException) -> ModelAndView:
        return ModelAndView("form.html", {"values": error.values, "errors": error.errors}, status_code=422)
