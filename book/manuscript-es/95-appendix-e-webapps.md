<span class="eyebrow">Apéndice E</span>

# Aplicaciones Web Nativas y Administración de Modelos {.chtitle}

PyFly sirve páginas HTML y administra los mismos datos que utilizan sus API REST.
Este apéndice sigue el catálogo ejecutable de `samples/webapp`. Su `Product` es
una entidad PyFly `BaseEntity` normal: los formularios y el panel leen y modifican
las mismas filas. Puedes registrar entidades SQLAlchemy o documentos Beanie
existentes sin definir un segundo modelo de persistencia.

Lumen sigue siendo el servicio de monederos del libro. El catálogo es otro ejemplo,
más pequeño y centrado en el navegador. No expongas saldos o asientos de Lumen como
registros genéricos editables: el movimiento de dinero debe pasar por los comandos
del dominio. Un proveedor de administración personalizado puede delegar en ellos.

## E.1. Ejecutar el catálogo completo

Desde el repositorio del framework, entra en `samples/webapp`. Su proyecto resuelve
PyFly desde el repositorio que lo contiene y ejecuta esta implementación.

```bash
cd samples/webapp
uv sync --group dev
export WEBAPP_ADMIN_PASSWORD=\
  "$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
export WEBAPP_EDIT_TOKEN_KEY=\
  "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export WEBAPP_DATABASE_URL="sqlite+aiosqlite:///catalog.db"
export PYFLY_SECURITY_CSRF_COOKIE_SECURE=false
uv run python -m catalog.init_db
uv run uvicorn catalog.app:create_webapp --factory \
  --host 127.0.0.1 --port 8080
```

Abre `http://127.0.0.1:8080/`, identifícate como `admin` con la contraseña generada
y crea un producto. En `/admin`, selecciona **Datasources → Products**: encontrarás
el mismo registro. Cambia el precio y recarga el catálogo. Reinicia el servidor y
comprueba que el archivo SQLite conserva el producto.

El comando de inicialización crea la tabla del ejemplo únicamente si no existe.
El servidor no crea ni migra el esquema. El ejemplo sitúa administración en el
puerto 8080 para compartir autenticación; el puerto de administración separado
predeterminado de PyFly es 9090. En despliegues utiliza migraciones, HTTPS y cookies
seguras. La variable que desactiva la cookie segura es solo para HTTP local.

## E.2. Un modelo, dos interfaces de navegador

::: figure art/webapps-es.svg | Figura E.1 — Ambas interfaces usan los mismos modelos y origen de datos.

| Tipo | Responsabilidad |
|---|---|
| `Product(BaseEntity)` | Columnas persistidas, claves, restricciones y auditoría. |
| `ProductWrite(BaseModel)` | Entrada y validación compartidas por ambas interfaces. |
| `ModelAdmin` | Campos visibles y editables, operaciones y selección del origen. |

`ProductForm` añade al esquema de entrada un token de edición. Los esquemas de
entrada no crean tablas y `ModelAdmin` no sustituye la entidad. El catálogo es
propietario de su fábrica de sesiones; el controlador HTML y el panel utilizan un
servicio de administración sobre esa misma fábrica. En otras aplicaciones las
páginas públicas pueden conservar sus servicios habituales y la administración
puede tener un registro independiente con permisos más restringidos.

::: listing catalog/models.py | Listado E.1 — La entidad existente y el esquema de entrada compartido
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from pyfly.data.relational.sqlalchemy.entity import BaseEntity


class Product(BaseEntity):
    __tablename__ = "webapp_products"
    name: Mapped[str] = mapped_column(String(80), unique=True)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class ProductWrite(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)


class ProductForm(ProductWrite):
    edit_version: str = ""
:::

## E.3. Renderizar páginas y resolver URLs

Instala `pyfly[webapp]` para Starlette, Jinja y seguridad de navegador; añade
`data-relational` para SQL o `data-document` para MongoDB. Los mismos contratos de
vista funcionan con el extra `fastapi`. Activa el renderizado explícitamente:

::: listing pyfly.yaml | Listado E.2 — Recursos de paquete utilizados por el catálogo
pyfly:
  web:
    templates:
      enabled: true
      directories: []
      packages: ["catalog:templates"]
    static:
      enabled: true
      directories: []
      packages: ["catalog:static"]
    errors:
      html-enabled: true
:::

Los directorios del sistema de archivos son relativos al directorio de trabajo.
Los recursos de paquete viajan con la aplicación instalada. Usa `directories: []`
si solo utilizas paquetes; de lo contrario se comprueban también los directorios
locales predeterminados. Jinja admite herencia, inclusiones, funciones asíncronas
y escape HTML automático. Las variables inexistentes producen un error por defecto.

Utiliza `@controller` y devuelve `ModelAndView("catalog.html", contexto)` para una
página; también puedes indicar `status_code` y `headers`. `@rest_controller`
conserva el comportamiento JSON. Asigna nombres únicos mediante
`@get_mapping("/", name="catalog")` para evitar rutas escritas a mano.

En plantillas, `reverse('catalog')` genera una ruta local y
`static_url('catalog.css')` resuelve un recurso estático. En Python, importa
`reverse` y `static_url` de `pyfly.web` y pasa `request` como primer argumento.
Por ejemplo, `Redirect(reverse(request, "catalog"))` devuelve un 303 local después
de un POST. Ambas funciones respetan los prefijos de montaje y la ruta estática
configurada. `url_for('catalog')` sigue disponible cuando necesitas una URL absoluta.

Pasa nombres y parámetros sin codificación previa: las funciones codifican espacios,
Unicode, `#` y `?`. Los recursos se indican respecto a la raíz estática, por ejemplo
`images/logo.svg` o `downloads/manual.pdf`, sin `/` inicial ni segmentos `..`.
Una ruta inexistente produce un error de resolución; generar una URL no comprueba
que el archivo exista. Jinja `{% include 'partials/menu.html' %}` carga otra plantilla
desde sus raíces; no genera una URL ni publica archivos del servidor.

Registra un bean `TemplateEngine` para sustituir Jinja o beans
`TemplateContextProcessor` para añadir valores por petición. El modelo de la vista
prevalece sobre los procesadores; las funciones de URL y CSRF las reserva el
framework. Las redirecciones HTTP(S) externas requieren `allow_external=True`.

## E.4. Vincular formularios y mostrar errores

La acción de creación recibe `product: Form[ProductForm]`. PyFly analiza los campos,
convierte los tipos y valida el modelo Pydantic antes de invocar el controlador.
La acción pasa `product.model_dump(exclude={"edit_version"})` a
`AdminDataService.create` y redirige. Para editar, pasa también el identificador y
`product.edit_version` a `AdminDataService.update`.

Cada formulario POST debe incluir el token CSRF:

::: listing catalog/templates/form.html | Listado E.3 — Campos del formulario del ejemplo
<input type="hidden" name="{{ csrf_field }}" value="{{ csrf_token }}">
<input type="hidden" name="edit_version"
       value="{{ values.get('edit_version', '') }}">
<label for="name">Name</label>
<input id="name" name="name" value="{{ values.get('name', '') }}"
       required maxlength="80">
<label for="price">Price</label>
<input id="price" name="price" inputmode="decimal"
       value="{{ values.get('price', '') }}" required>
:::

Captura `FormValidationException` mediante `@exception_handler` y devuelve la
plantilla con `error.values`, `error.errors` y estado 422. El controlador completo
está en `catalog/controllers.py`. Los campos identificados como secretos y los tipos
secretos de Pydantic se eliminan de los valores mostrados; los mensajes omiten la
entrada enviada. Conserva el escape HTML. Un GET normal no invalida el token de
otra pestaña; la rotación o invalidación de sesión sí lo cambia.

Se admiten alias y campos de lista repetidos; los escalares repetidos se rechazan.
Una casilla desmarcada no envía valor: utiliza `False` como valor predeterminado y
`"true"` cuando esté marcada. Texto multipart y `File[UploadedFile]` comparten un
análisis limitado: 1.000 campos, 20 archivos, 1 MiB por parte y 16 MiB por petición
de forma predeterminada, configurables bajo `pyfly.web.forms`.

## E.5. Personalizar las páginas de error

Añade `errors/404.html` y `errors/500.html` a las raíces de plantillas. Para asociar
un estado explícitamente, configura `pyfly.web.errors.templates`, por ejemplo
`{"404": "errors/missing.html"}`. Se intenta primero esa asociación, después el
estado exacto, la familia (`errors/4xx.html`) y `default-template`, cuyo valor es
`errors/error.html`. Una plantilla ausente permite probar la siguiente; una rota
produce la página integrada con contenido escapado.

Las plantillas de error reciben `status`, `title`, `message`, `path` y
`transaction_id`, además de `reverse`, `static_url` y `url_for` para navegación
y recursos compartidos. Puedes usar `static_url('catalog.css')`, pero el diseño
no debe depender de procesadores de contexto ni funciones CSRF/formulario.
Los errores de servidor ocultan detalles de excepciones de forma predeterminada.
Los errores del navegador negocian HTML mediante `Accept`; REST y la API de datos
siguen devolviendo JSON. Los fallos de autenticación y CSRF conservan estado y
cabeceras de seguridad. Las páginas usan `no-store` y `Vary: Accept`.

### Identidad visual y página de bienvenida

El framework incluye `pyfly/base.html`, `pyfly/welcome.html` y `pyfly/error.html`,
con el logotipo de PyFly, su paleta verde, diseño adaptable y colores claros u oscuros
según el navegador. Solo el arquetipo `web` activa estas funciones automáticamente.
Los proyectos de API, microservicios, arquitectura hexagonal, bibliotecas y CLI las
mantienen desactivadas; instalar un extra no activa páginas. Una API existente puede
activarlas de forma deliberada mediante configuración.

Con plantillas activadas, la bienvenida aparece en `/` cuando no existe un controlador
de inicio. Los controladores descubiertos durante el arranque tienen prioridad, como
ocurre en el catálogo. `pyfly.web.welcome.enabled: false` desactiva esta página;
`welcome.template` selecciona otra plantilla. Una webapp mínima que solo utiliza
plantillas integradas configura `templates.enabled: true` y `templates.directories: []`.

`pyfly.web.branding` configura vistas y errores conjuntamente. `name` vale `PyFly`;
`tagline`, `Build something that matters.`; `primary-color`, `#4cbb2f`; y
`accent-color`, `#c2e85f`. Los colores aceptan valores hexadecimales de seis dígitos.
`logo-url` y `favicon-url` aceptan URLs HTTP(S) o rutas relativas a la aplicación como
`/static/catalog-logo.svg`; se añade el prefijo del montaje ASGI. Un logo vacío usa el
de PyFly y un favicon vacío omite el icono. Personaliza `documentation-url`,
`support-url` y `footer`; una URL vacía oculta su enlace.

Las plantillas reciben `branding`, `home_url` y `web_asset_url`. El framework publica
logo y estilos bajo `branding.assets-path`, cuyo valor es `/_pyfly/web`, solo al activar
plantillas o errores HTML. Permite ese prefijo reservado en las reglas de seguridad
si las páginas anteriores al inicio de sesión lo necesitan. Los estilos externos
funcionan con `style-src 'self'`. Los recursos de la aplicación usan `static_url`;
`web_asset_url('web.css')`, `web_asset_url('theme.css')` y `web_asset_url('logo.png')`
resuelven los recursos integrados.

Extiende una plantilla integrada y reemplaza los bloques `title`, `head`, `navigation`,
`content` o `footer`. Los errores del catálogo extienden `pyfly/error.html` y añaden el
enlace al catálogo en navegación. Las raíces de la aplicación tienen prioridad sobre
las integradas. Mantén la plantilla hija en una ruta distinta de la del padre para
evitar herencia recursiva. Un motor personalizado debe ofrecer la plantilla de
bienvenida seleccionada o desactivar esa página.

### Trazas durante el desarrollo

`pyfly.web.errors.include-stacktrace` acepta `never`, `on-debug` (predeterminado) y
`always`. Activa `pyfly.web.debug: true` en un perfil local, o pasa `debug=True` a la
factoría del adaptador, para mostrar trazas con la política predeterminada. También
funciona `PYFLY_WEB_DEBUG=true`. En producción el panel permanece oculto; los parámetros
de la URL no lo activan. Con errores HTML habilitados, PyFly controla las páginas de
depuración negociadas y los controladores REST siguen devolviendo JSON.

Las plantillas reciben `show_stacktrace`, `exception_type`, `exception_message`,
`stack_trace` y `stack_frames`. Cada marco contiene `filename`, `lineno`, `function`
y `source`. La traza corresponde a la excepción original, incluso cuando un conversor
modifica el estado HTTP. Si el diagnóstico está desactivado, se reciben cadenas y
lista vacías. Los rechazos de seguridad tempranos pueden no tener excepción.

`max-stack-frames` vale 30 (rango 1–200); `max-trace-length`, 20.000 caracteres
(rango 256–100.000). Se incluyen los marcos más recientes de la excepción actual, sin
cadenas de excepciones ni variables locales. No se recopilan cabeceras, cookies,
parámetros de consulta ni cuerpos de peticiones. El mensaje y las líneas de código
pueden contener secretos: mantén las trazas desactivadas en producción pública.
Jinja escapa su texto; no apliques `safe`.

`pyfly/error.html` incluye el panel. En un diseño independiente usa
`{% if show_stacktrace %}<pre>{{ stack_trace }}</pre>{% endif %}`. Si falla una plantilla
de error de la aplicación, el renderizador integrado independiente conserva la traza
original. También existe una respuesta con identidad visual y contenido escapado
cuando Jinja no está instalado.

## E.6. Registrar la entidad existente

En una aplicación con escaneo, publica un bean `ModelAdmin`. Esta alternativa
sustituye el registro explícito y el proveedor que configura el ejemplo:

::: listing catalog/administration.py | Listado E.4 — Registrar el Product existente
from catalog.models import Product, ProductWrite
from pyfly.admin import ModelAdmin
from pyfly.container import bean, configuration


@configuration
class Administration:
    @bean
    def products(self) -> ModelAdmin:
        return ModelAdmin(
            "products", Product, label="Products",
            datasource="primary",
            fields=("id", "name", "price", "updated_at"),
            editable_fields=("name", "price"),
            search_fields=("name",), filter_fields=("name",),
            ordering=("name",),
            operations=("list", "read", "create", "update", "delete"),
            create_schema=ProductWrite, update_schema=ProductWrite,
        )
:::

El origen primario reutiliza el bean `async_session_factory`; uno con nombre usa
`NamedDataSources.get(name)`. Para un documento Beanie ya inicializado, indica
`datasource="document"`. Un `provider` explícito tiene prioridad. Administración
no crea clientes, migra esquemas ni cierra recursos propiedad de la aplicación.

Activa `pyfly.admin.data.enabled`, configura `allowed-roles` y proporciona
`edit-token-key` mediante un secreto de entorno de al menos 32 caracteres si
habilitas escrituras. Comparte la clave entre procesos; rotarla invalida ediciones
abiertas. Por defecto, un registro solo permite listar y leer. Las tablas no se
exponen automáticamente. Las claves primarias y los campos de auditoría, versión
y binarios son de solo lectura; selecciona explícitamente los campos editables.

## E.7. Permisos, conflictos y reglas de negocio

La administración de datos exige un `SecurityContext` autenticado y un rol
permitido aunque la monitorización use `require-auth: false`. Un puerto de
administración separado debe activar `pyfly.management.security.enabled`; el
arranque falla si falta. Configura autenticación y reglas de rutas para ese puerto.
Toda mutación exige la cookie CSRF y su cabecera `X-XSRF-TOKEN`, incluso con bearer.

Las operaciones permitidas son la intersección de configuración global, registro
y `has_permission(operation, context, record=None)`. El método puede restringir,
pero no ampliar esas listas. Usa `scope(context)` para restricciones de igualdad
definidas por el servidor, como el tenant. Se aplican a recuentos, listas, lecturas
y mutaciones; las altas heredan el ámbito y las modificaciones no pueden salir de
él. Los permisos por objeto no bastan para ocultar filas de una lista.

El panel agrupa orígenes y recursos y permite paginación, búsqueda, filtros y orden.
Los formularios muestran controles tipados, nulos explícitos y errores de validación.
`relations={"category_id": "categories"}` obtiene opciones autorizadas y comprueba
la selección antes de escribir. Las escrituras anidadas requieren lógica propia.

El token de edición representa todo el estado persistido, incluidos campos ocultos.
PATCH lo envía como `editToken`; DELETE, en `If-Match`. PostgreSQL bloquea la fila,
SQLite mantiene una transacción inmediata y MongoDB ejecuta una sustitución o
borrado condicionado al BSON original. Una edición obsoleta devuelve 409 y conserva
la entrada hasta una recarga explícita. Las restricciones conflictivas también
devuelven 409; la entrada inválida, 422. Verifica bloqueos equivalentes antes de
habilitar escrituras concurrentes en otros dialectos SQL.

Los decimales y enteros escalares grandes conservan precisión como cadenas ante el
navegador. `AdminFieldAdapter(type, serialize, parse)` adapta tipos propios.
Si necesitas reglas de servicio o action hooks de Beanie, implementa
`AdminDataProvider` y pásalo como `provider`. El proveedor Mongo integrado valida
el documento resultante, pero no ejecuta action hooks arbitrarios. Un proveedor
propio debe conservar ámbito, validación y comprobaciones atómicas de edición.
La auditoría de éxito registra actor, recurso, identificador, operación, nombres
de campos y correlación, sin los valores enviados.

## E.8. Configuración, pruebas y diagnóstico

Las propiedades utilizan YAML/TOML, perfiles y entorno como el resto de PyFly.
`PYFLY_WEB_TEMPLATES_AUTO_RELOAD=true` activa recarga local de plantillas;
`PYFLY_ADMIN_DATA_OPERATIONS=list,read` restringe globalmente a lectura. Usa perfiles
para diferencias entre entornos y secretos para credenciales. Plantillas, estáticos,
errores HTML y administración de modelos requieren activación explícita.

Ejecuta `uv run pytest` en `samples/webapp`: usa una base temporal y comprueba HTML,
validación, CSRF, persistencia y los mismos IDs en administración. El framework
añade pruebas de ambos adaptadores, PostgreSQL/MongoDB reales, CRUD en navegador e
instalación del wheel. `docs/modules/webapps.md` contiene comandos y referencia completa.

| Síntoma | Comprobación |
|---|---|
| Falla la raíz de plantillas | Directorio existente, paquete instalado y raíces no usadas desactivadas. |
| POST local devuelve 403 | Cookie segura sobre HTTP, campo CSRF ausente o sesión rotada. |
| Datasources no aparece o está vacío | Activación, registro descubierto, rol y permiso para listar. |
| Falla el arranque de administración | Activa su seguridad y configura autenticación real. |
| Guardar devuelve 409 | Recarga el registro; revisa restricciones y valores únicos. |
| Enlaces rotos bajo un prefijo | Usa `reverse`/`static_url` y configura el root path ASGI. |

## Pruébalo tú mismo {.exercises}

1. Crea un producto en el catálogo, edítalo en el panel y verifica ID y precio
   desde una sesión nueva de base de datos.
2. Edita un registro en dos pestañas. Guarda la primera y después la segunda:
   comprueba que 409 impide sobrescribir silenciosamente el valor más reciente.
3. Limita las operaciones globales a `list,read`. Comprueba tanto los controles
   del panel como el rechazo de peticiones de mutación directas.
4. Cambia la plantilla 404 y compara una ruta inexistente con
   `Accept: text/html` y `Accept: application/json`.
