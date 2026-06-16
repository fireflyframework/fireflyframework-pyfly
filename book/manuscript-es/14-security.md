<span class="eyebrow">Capítulo 14</span>

# Seguridad, sesiones e identidad {.chtitle}

::: figure art/openers/ch14.svg | &nbsp;

En el Capítulo 13 hiciste que Lumen fuera rápido y tolerante a fallos con caché y decoradores de resiliencia. La API de Lumen maneja ahora una alta concurrencia sin romperse bajo presión, pero está abierta de par en par. Cualquier llamante puede crear monederos, leer saldos o disparar depósitos. Antes de poder enviar las preocupaciones de producción restantes de la Parte V, necesitas cerrar esa puerta.

Este capítulo blinda Lumen. Vas a:

- **Autenticar** cada petición con un JWT firmado, usando `JWTService` para emitir y validar tokens y `SecurityMiddleware` para propagar el `SecurityContext` por todo el ámbito de la petición.
- **Autorizar** manejadores y comandos individuales con el decorador `@secure`, especificando roles, permisos o expresiones de seguridad completas.
- **Hashear contraseñas** de forma segura con `BcryptPasswordEncoder` para que tu almacén de usuarios nunca sea un pasivo.
- **Gestionar sesiones del lado del servidor** con `HttpSession`, un `SessionStore` conectable y un backend de Redis para escalado horizontal.
- **Federar la identidad** a un proveedor externo —Keycloak, AWS Cognito o Azure AD— a través del puerto `IdpAdapter` sin cambiar una sola línea de lógica de negocio.

Si has trabajado antes con Spring Security, la forma te resultará familiar: configura una cadena de filtros, anota métodos individuales y sustituye la fuente de detalles de usuario por un IDP. PyFly llama a esas piezas `SecurityMiddleware + HttpSecurity`, `@secure` e `IdpAdapter`, pero los conceptos se corresponden uno a uno.

Este capítulo está escrito como un tutorial guiado. Construimos la capa de seguridad pieza a pieza —emitir un token, validarlo, proteger un manejador, hashear una contraseña, almacenar una sesión, federar a un IDP— y después de cada pieza encontrarás un punto de control **Pruébalo** con el comando exacto que teclear y la salida que deberías esperar. Si nunca antes has conectado la autenticación a un servicio web, no pasa nada: cada término nuevo se glosa en lenguaje llano la primera vez que aparece, y puedes seguir el hilo editando el ejemplo de Lumen a medida que lees.

!!! note "Versión"
    Los listados y las claves de configuración de este capítulo apuntan a PyFly **v26.6.110**.
    Si estás en una versión anterior, algunos nombres de propiedades difieren —sobre todo
    el puerto de la aplicación, que ahora es `pyfly.server.port` (el `server.port` de Spring),
    y el actuator y el panel de administración viven en un puerto de gestión separado,
    `pyfly.management.server.port` (por defecto `9090`).

::: figure art/figures/14-security.svg | Figura 14.1 — Las capas de seguridad de Lumen. Un filtro JWT rellena el SecurityContext; HttpSecurity impone reglas a nivel de URL; @secure impone reglas a nivel de manejador; el puerto IDP delega la identidad a un proveedor externo.

---

## Autenticación con JWT

### ¿Por qué JSON Web Tokens?

Lumen es una API sin estado. Las sesiones HTTP requerirían enrutamiento adhesivo (sticky) o un almacén de sesiones compartido en cada réplica. Los tokens JWT permiten que cada servicio valide las credenciales de forma independiente: sin estado compartido, sin coordinación, escalado horizontal por defecto.

Un **JWT** (JSON Web Token, que se pronuncia "yot") es una carga JSON firmada. Piénsalo como una credencial a prueba de manipulaciones: el servidor estampa un pequeño documento JSON —*quién eres*, *qué roles tienes*, *cuándo caduca*— y lo firma con una clave secreta. La credencial viaja con cada petición. Como la firma solo puede producirla alguien que posea el secreto, el servidor puede confiar en la credencial sin consultar nada en una base de datos. El servicio de autenticación de Lumen emite un token al iniciar sesión; cada petición posterior lleva ese token en la cabecera `Authorization`; `SecurityMiddleware` valida la firma y desempaqueta el token en un `SecurityContext` que el resto de la petición puede leer.

### JWTService

`JWTService` envuelve PyJWT con tres operaciones bien definidas:

| Método | Descripción |
|---|---|
| `encode(payload)` | Firma un diccionario de carga, añadiendo `exp` si falta |
| `decode(token)` | Valida la firma + `exp`; lanza `SecurityException` si falla |
| `to_security_context(token)` | Decodifica y extrae `sub`, `roles`, `permissions` en un `SecurityContext` |

El servicio siempre exige un claim `exp`: un token sin caducidad se rechaza
en el momento de decodificar. Ese invariante significa que todo token en circulación tiene un
tiempo de vida acotado.

::: listing lumen/core/services/auth/auth_service.py | Listado 14.1 — Emitir un JWT en un inicio de sesión correcto
from pyfly.container import service
from pyfly.kernel.exceptions import UnauthorizedException
from pyfly.security import BcryptPasswordEncoder, JWTService, SecurityContext


@service
class AuthService:

    def __init__(
        self,
        jwt: JWTService,
        encoder: BcryptPasswordEncoder,
        user_repo,
    ) -> None:
        self._jwt = jwt
        self._encoder = encoder
        self._users = user_repo

    async def login(self, username: str, password: str) -> str:
        user = await self._users.find_by_username(username)
        if user is None or not self._encoder.verify(
            password, user.password_hash
        ):
            raise UnauthorizedException(
                "Invalid credentials", code="INVALID_CREDENTIALS"
            )
        # encode() auto-appends exp (default: 3 600 s from now)
        return self._jwt.encode({
            "sub": str(user.id),
            "roles": [user.role],
            "permissions": _permissions_for(user.role),
        })

    async def me(self, ctx: SecurityContext) -> dict:
        user = await self._users.find_by_id(ctx.user_id)
        return {
            "id": str(user.id),
            "username": user.username,
            "role": user.role,
        }


def _permissions_for(role: str) -> list[str]:
    MAP = {
        "USER":  ["wallet:read", "wallet:deposit"],
        "ADMIN": [
            "wallet:read", "wallet:deposit",
            "wallet:create", "wallet:delete",
            "user:read", "user:write",
        ],
    }
    return MAP.get(role, [])

:::
:::

**Cómo funciona.** `login` obtiene el registro del usuario y llama a `BcryptPasswordEncoder.verify` para comparar la contraseña suministrada con el hash almacenado. Si tiene éxito, llama a `jwt.encode`, que añade automáticamente un claim `exp` a `expiration_seconds` segundos a partir de ahora (por defecto `3600`, una hora). Nunca importas `datetime`: el servicio calcula la caducidad como marca de tiempo Unix con `int(time.time()) + expiration_seconds`. El llamante recibe una cadena de token compacta y autocontenida.

Recorramos el método `login` paso a paso:

**Paso 1 — Buscar al usuario.** `find_by_username` devuelve el registro de usuario almacenado, o `None` si no existe ese nombre de usuario. Trataremos tanto "no existe ese usuario" como "contraseña incorrecta" como el mismo fallo, para que un atacante no pueda saber qué nombres de usuario están registrados.

**Paso 2 — Verificar la contraseña.** `self._encoder.verify(password, user.password_hash)` vuelve a hashear la contraseña suministrada con la sal incorporada en el hash almacenado y compara ambos en tiempo constante. Si no se encontró al usuario, o las contraseñas no coinciden, lanza `UnauthorizedException`: PyFly lo renderiza como un `401` con el código legible por máquina `INVALID_CREDENTIALS`.

**Paso 3 — Construir los claims.** Si tiene éxito, ensambla la carga JSON que se convertirá en el cuerpo del token: `sub` (el sujeto, el id del usuario), `roles` y los `permissions` que ese rol concede.

**Paso 4 — Firmar y devolver.** `self._jwt.encode({...})` firma la carga con el secreto configurado, estampa el claim obligatorio `exp` y devuelve la cadena de token compacta. Esa cadena es lo que el cliente almacena y reenvía en cada petición posterior.

!!! note "Jerga: claim"
    Un *claim* no es más que una afirmación clave/valor dentro del cuerpo JSON del token:
    `"sub": "42"` afirma que el sujeto es el usuario 42. El token lleva un saco de
    claims; el framework copia los relevantes para la seguridad (`sub`, `roles`,
    `permissions`) en el `SecurityContext`.

!!! tip "Pruébalo — emite un token en el REPL"
    No necesitas un servidor en marcha para ver `JWTService` en acción. Con el
    entorno virtual de Lumen activo, abre un REPL de Python y firma una carga:

    ```python
    >>> from pyfly.security import JWTService
    >>> jwt = JWTService(secret="dev-secret-change-me", algorithm="HS256")
    >>> token = jwt.encode({"sub": "42", "roles": ["USER"]})
    >>> token[:20]            # a compact "header.payload.signature" string
    'eyJhbGciOiJIUzI1NiIs'
    >>> ctx = jwt.to_security_context(token)
    >>> ctx.user_id, ctx.roles
    ('42', ['USER'])
    ```

    El token hace ida y vuelta: `encode` añadió el claim `exp` por ti, y
    `to_security_context` validó la firma y desempaquetó los claims en un
    `SecurityContext`. Prueba a manipular un carácter en mitad de la
    cadena y llama de nuevo a `jwt.decode(token)`: obtendrás una
    `SecurityException`, porque la firma ya no coincide con la carga.

### El SecurityContext

**`SecurityContext`** es una dataclass inmutable que transporta los datos de autenticación y autorización de una única petición. El middleware lo crea a partir del token validado; tus manejadores lo reciben como un parámetro inyectado.

| Campo | Tipo | Descripción |
|---|---|---|
| `user_id` | `str \| None` | Id del usuario autenticado; `None` si es anónimo |
| `roles` | `list[str]` | Roles concedidos en el token |
| `permissions` | `list[str]` | Permisos de grano fino |
| `attributes` | `dict[str, str]` | Claims adicionales (departamento, tenant, …) |

Métodos clave:

| Método / Propiedad | Devuelve | Descripción |
|---|---|---|
| `is_authenticated` | `bool` | `True` cuando `user_id` no es `None` |
| `has_role(role)` | `bool` | Coincidencia exacta con la lista `roles` |
| `has_any_role(roles)` | `bool` | Intersección de conjuntos: cualquiera de los roles listados |
| `has_permission(perm)` | `bool` | Coincidencia exacta con la lista `permissions` |
| `SecurityContext.anonymous()` | `SecurityContext` | Crea un contexto no autenticado |

### El filtro de seguridad

**`SecurityMiddleware`** (ubicación canónica `pyfly.web.adapters.starlette.security_middleware`, reexportado desde `pyfly.security`) se sitúa en la capa de middleware de Starlette. Para cada petición:

1. Comprueba si la ruta está en `exclude_paths`; si es así, establece un contexto anónimo y continúa.
2. Lee la cabecera `Authorization` y elimina el prefijo `Bearer `.
3. Llama a `jwt_service.to_security_context(token)`.
4. Si tiene éxito, almacena el contexto autenticado en `request.state.security_context`.
5. Ante cualquier `SecurityException` (token caducado, manipulado o sin `exp`), registra a nivel DEBUG y establece en su lugar un contexto anónimo.

El middleware **nunca rechaza peticiones**: el rechazo es trabajo de `@secure` y `HttpSecurity`. Un endpoint que requiere al usuario puede imponerlo; un endpoint de comprobación de salud puede ignorar el contexto por completo.

::: listing lumen/app.py | Listado 14.2 — Añadir el middleware de seguridad
from pyfly.security import JWTService, SecurityMiddleware
from pyfly.web.adapters.starlette import create_app


def build_app(context):
    app = create_app(title="Lumen", context=context)

    jwt = context.get_bean(JWTService)
    app.add_middleware(
        SecurityMiddleware,
        jwt_service=jwt,
        exclude_paths=[
            "/docs",
            "/openapi.json",
            "/api/auth/login",
            "/api/auth/register",
        ],
    )
    return app

:::
:::

**Cómo funciona.** `exclude_paths` enumera las rutas donde no se espera ningún token. El inicio de sesión y el registro no pueden requerir autenticación porque el token aún no existe; las rutas de la documentación se excluyen para que el explorador de la API funcione sin credenciales. Cualquier otra ruta pasa por la validación del token.

!!! note "Jerga: middleware"
    El *middleware* es código que envuelve cada petición, ejecutándose antes del manejador
    de la ruta a la entrada y después de él a la salida. `SecurityMiddleware`
    usa la pasada de "entrada" para leer el token y guardar un `SecurityContext` en
    `request.state` para que los manejadores posteriores puedan leerlo sin volver a parsear la
    cabecera.

**Lo que acaba de ocurrir.** Ya tienes conectadas las dos mitades de la autenticación. `AuthService.login` *acuña* un token tras comprobar la contraseña; `SecurityMiddleware` *lee* ese token en cada petición posterior y lo convierte en un `SecurityContext`. Es crucial que el middleware sea permisivo: nunca devuelve un `401`. Una petición con un token incorrecto o ausente simplemente llega como anónima, y la decisión de permitirla o rechazarla se delega a las dos capas siguientes que construirás: `HttpSecurity` (amplia, a nivel de URL) y `@secure` (precisa, por manejador). Separar *quién eres* (autenticación) de *qué puedes hacer* (autorización) es lo que mantiene cada capa pequeña y testeable.

### Reglas a nivel de URL con HttpSecurity

`@secure` protege métodos manejadores individuales. **`HttpSecurity`** protege subárboles de URL completos en la capa de filtro, antes de que se ejecute el despachador de rutas. Las dos son complementarias: `HttpSecurity` aporta una política rápida y amplia en el borde; `@secure` añade una imposición de grano fino, por manejador, detrás de ella.

::: listing lumen/config/security_config.py | Listado 14.3 — El DSL de HttpSecurity
from pyfly.container import bean, configuration
from pyfly.security.http_security import HttpSecurity


@configuration
class SecurityConfig:

    @bean
    def http_security(self) -> HttpSecurity:
        hs = HttpSecurity()
        hs.authorize_requests() \
            .request_matchers("/idp/admin/**").has_role("ADMIN") \
            .request_matchers("/api/v1/wallets/**").authenticated() \
            .request_matchers(
                "/health", "/docs", "/openapi.json",
                "/idp/login", "/idp/refresh",
            ).permit_all() \
            .any_request().permit_all()
        return hs

:::
:::

**Cómo funciona.** Las reglas se evalúan en el orden de declaración: gana la primera coincidencia. El `HttpSecurityFilter` se ejecuta en `HIGHEST_PRECEDENCE + 350`, después de que los filtros de autenticación hayan rellenado `request.state.security_context`, de modo que toda comprobación de rol y permiso dispone de un contexto totalmente hidratado para inspeccionar. Los métodos terminales —`has_role`, `has_any_role`, `has_permission`, `authenticated`, `permit_all` y `deny_all`— cubren toda política habitual; las reglas no satisfechas devuelven JSON de detalle de problema RFC 7807 (`application/problem+json`) con el estado HTTP apropiado.

Lee la cadena del DSL de arriba abajo: ese es exactamente el orden en que el filtro la evalúa:

**Paso 1 — Abre la lista de reglas.** `hs.authorize_requests()` inicia el constructor fluido. Cada llamada `.request_matchers(...)` que sigue registra una regla.

**Paso 2 — Blinda el subárbol de administración.** `.request_matchers("/idp/admin/**").has_role("ADMIN")` exige el rol `ADMIN` para cualquier cosa bajo `/idp/admin`. El glob `**` coincide con cualquier profundidad de segmentos de ruta.

**Paso 3 — Exige inicio de sesión para los monederos.** `.request_matchers("/api/v1/wallets/**").authenticated()` acepta a cualquier llamante con sesión iniciada, sin importar el rol, para el árbol de monederos. Las reglas más finas por manejador llegan después vía `@secure`.

**Paso 4 — Permite las rutas públicas.** `.request_matchers("/health", "/docs", ...).permit_all()` deja pasar sin token las comprobaciones de salud, el explorador de la documentación y los endpoints de inicio de sesión/refresco del IDP.

**Paso 5 — Establece el valor por defecto.** `.any_request().permit_all()` es el comodín para las rutas que ninguna regla anterior haya cubierto. Cámbialo a `.deny_all()` una vez que toda ruta esté explícitamente contemplada: "denegar por defecto" es la postura de producción más segura.

**Paso 6 — Devuelve el constructor.** Devuelve el propio `HttpSecurity` configurado; **no** llames aquí a `hs.build()`. Como `SecurityConfig` es una clase `@configuration`, el `HttpSecurityFilterAutoConfiguration` de la autoconfiguración (activo siempre que exista un bean `HttpSecurity`) recoge el bean `HttpSecurity`, llama a `.build()` por ti y registra en la cadena el `HttpSecurityFilter` resultante.

!!! tip "Pruébalo — observa cómo la puerta acepta y rechaza"
    Arranca la aplicación con `uv run pyfly run` (sirve en `pyfly.server.port`,
    por defecto `8080`). En otra terminal, accede a una ruta protegida sin token:

    ```bash
    curl -i http://localhost:8080/api/v1/wallets
    ```

    Esperado — la puerta rechaza la petición anónima con un cuerpo RFC 7807:

    ```text
    HTTP/1.1 401 Unauthorized
    content-type: application/problem+json

    {"type":"about:blank","title":"Unauthorized","status":401,
     "detail":"Authentication is required to access this resource.",
     "instance":"/api/v1/wallets"}
    ```

    Ahora reenvíala con un token (usa el `$TOKEN` que acuñaste en el REPL de arriba,
    o uno de `/idp/login`):

    ```bash
    curl -i -H "Authorization: Bearer $TOKEN" http://localhost:8080/api/v1/wallets
    ```

    Esperado — `HTTP/1.1 200 OK` y una página JSON de monederos. La misma URL,
    dos resultados, decididos enteramente por el token: eso es la puerta haciendo su trabajo.

!!! note "Defensa en dos capas"
    `HttpSecurity` aporta una política rápida a nivel de URL antes incluso de que las rutas se
    despachen, ideal para reglas generales como "todo lo que esté bajo `/api/v1/wallets`
    necesita autenticación". Los decoradores `@secure` sobre métodos individuales son
    la segunda capa, de grano más fino. Usa ambos juntos para una defensa en profundidad.

!!! spring "Equivalencia con Spring"
    `HttpSecurity` refleja la cadena `HttpSecurity.authorizeHttpRequests()` de Spring Security. `request_matchers` se corresponde con `requestMatchers`, `authenticated()` con `.authenticated()`, `has_role` con `hasRole`, y el `build()` que la autoconfiguración llama sobre tu bean `HttpSecurity` finaliza el filtro subyacente igual que `build()` finaliza la cadena de filtros de Spring. Los patrones glob de fnmatch (`/api/admin/**`) se comportan de forma idéntica a la coincidencia de rutas estilo Ant de Spring.

---

## Autoconfiguración

No necesitas registrar `JWTService` ni `BcryptPasswordEncoder` manualmente. Añade las propiedades pertinentes a `pyfly.yaml` y la autoconfiguración lo conecta todo:

::: listing lumen/resources/pyfly.yaml | Listado 14.4 — Autoconfiguración de seguridad
pyfly:
  security:
    enabled: true
    jwt:
      secret: "${JWT_SECRET}"
      algorithm: HS256
      filter:
        enabled: true
      exclude-patterns: >-
        /docs,/openapi.json,
        /api/auth/login,/api/auth/register
    password:
      bcrypt-rounds: 12

:::
:::

| Propiedad | Por defecto | Descripción |
|---|---|---|
| `pyfly.security.jwt.secret` | `change-me-in-production` | Clave de firma HMAC; **debe** sobreescribirse |
| `pyfly.security.jwt.algorithm` | `HS256` | Algoritmo de firma |
| `pyfly.security.jwt.filter.enabled` | *(ausente)* | Pon `true` para registrar el bean `SecurityFilter` automáticamente |
| `pyfly.security.jwt.exclude-patterns` | *(ausente)* | Rutas separadas por comas que se saltan |
| `pyfly.security.password.bcrypt-rounds` | `12` | Factor de coste de Bcrypt |

Fíjate dónde vive cada clave: `filter.enabled` está anidada bajo `jwt.filter`, pero `exclude-patterns` se sitúa un nivel más arriba, directamente bajo `jwt`: esa es la clave que lee la autoconfiguración (`pyfly.security.jwt.exclude-patterns`). Ya no hay un `/actuator/health` en la lista de exclusión: a partir de la v26.6.110 el actuator y el panel de administración se ejecutan en un **puerto de gestión separado** (`pyfly.management.server.port`, por defecto `9090`), de modo que nunca pasan por el filtro JWT de la aplicación.

!!! warning "Secreto de producción"
    Nunca confirmes el secreto JWT real al control de versiones. Usa `${JWT_SECRET}` e
    inyecta el valor desde una variable de entorno o un gestor de secretos en el
    momento del despliegue.

!!! warning "El puerto de gestión está abierto por defecto"
    Por paridad con Spring, el servidor de gestión (`/actuator/*` y el panel de
    administración, puerto por defecto `9090`) está **no autenticado por defecto**: no
    comparte la puerta de seguridad de la aplicación. En cualquier despliegue que no sea local,
    asegúralo explícitamente:

    ```yaml
    pyfly:
      management:
        security:
          enabled: true       # apply the security gate to the management port
    ```

    Establecer `pyfly.management.server.port: -1` deshabilita por completo los endpoints
    de gestión. La exposición HTTP por defecto del actuator es solo `health,info`; amplíala
    con `pyfly.management.endpoints.web.exposure.include`.

!!! tip "Pruébalo — confirma la conexión automática en el arranque"
    Con las claves anteriores en `pyfly.yaml`, arranca la aplicación y observa el log de arranque:

    ```bash
    uv run pyfly run
    ```

    Esperado — verás que la aplicación se vincula a `:8080` y el servidor de gestión a
    `:9090`, y los beans de seguridad aparecen sin ningún código `@configuration` propio. Una prueba rápida de que el bean del encoder existe:

    ```bash
    curl -s http://localhost:8080/api/auth/login \
      -d '{"username":"alice","password":"hunter2"}' \
      -H 'content-type: application/json'
    ```

    Esperado — un cuerpo JSON que contiene un `access_token` (o un `401` con
    `INVALID_CREDENTIALS` si las credenciales son incorrectas). En cualquier caso, los beans
    `JWTService` y `BcryptPasswordEncoder` se conectaron automáticamente.

---

## Autorización con @secure

**`@secure`** es un decorador de funciones que impone autenticación y autorización en manejadores y comandos individuales. Lee el argumento de palabra clave `security_context` que el middleware ya ha inyectado y luego evalúa las comprobaciones de rol, permiso y expresión antes de que se ejecute el cuerpo de la función.

### Firma

```python
def secure(
    roles: list[str] | None = None,
    permissions: list[str] | None = None,
    expression: str | None = None,
) -> Callable: ...
```

La función decorada debe aceptar `security_context: SecurityContext` como argumento de palabra clave: así es como `@secure` alcanza al usuario actual.

### Protección basada en roles

Los endpoints de monederos de Lumen (`/api/v1/wallets`) son el lugar natural para aplicar
`@secure`. El `WalletController` real inyecta los buses de comandos y consultas;
añade `security_context: SecurityContext` a cada método que necesite protección
y apila `@secure` encima.

El controlador expone siete endpoints bajo `/api/v1/wallets`:

| Método | Ruta | Manejador | Protección |
|---|---|---|---|
| POST | `""` | `open_wallet` | USER o ADMIN |
| POST | `/{wallet_id}/deposit` | `deposit` | USER/ADMIN + `wallet:deposit` |
| POST | `/{wallet_id}/withdraw` | `withdraw` | USER/ADMIN + `wallet:deposit` |
| GET | `""` | `list_wallets` | autenticado |
| GET | `/rich` | `list_rich_wallets` | autenticado |
| GET | `/{wallet_id}/balance` | `wallet_balance` | USER o ADMIN |
| GET | `/{wallet_id}` | `wallet_detail` | solo ADMIN |

Los manejadores de colección (`list_wallets`, `list_rich_wallets`) se nombran con
el prefijo `list_` para que el framework los registre antes que los manejadores
`wallet_detail` / `wallet_balance`: Starlette resuelve por
primero-registrado-gana, así que el segmento literal `/rich` se encuentra antes que la
variable de ruta `/{wallet_id}`.

::: listing lumen/web/controllers/wallet_controller.py | Listado 14.5 — Protecciones de rol y permiso en endpoints reales de Lumen
from lumen.core.services.wallets.deposit_funds_command import DepositFunds
from lumen.core.services.wallets.get_balance_query import GetBalance
from lumen.core.services.wallets.get_wallet_query import GetWallet
from lumen.core.services.wallets.list_rich_wallets_query import ListRichWallets
from lumen.core.services.wallets.list_wallets_query import ListWallets
from lumen.core.services.wallets.open_wallet_command import OpenWallet
from lumen.core.services.wallets.withdraw_funds_command import WithdrawFunds
from lumen.interfaces.dtos.v1.balance_dto import BalanceDto
from lumen.interfaces.dtos.v1.deposit_request import DepositRequest
from lumen.interfaces.dtos.v1.open_wallet_request import OpenWalletRequest
from lumen.interfaces.dtos.v1.page_dto import PageDto
from lumen.interfaces.dtos.v1.wallet_dto import WalletDto
from pyfly.container import rest_controller
from pyfly.cqrs import DefaultCommandBus, DefaultQueryBus
from pyfly.data import Pageable, Sort
from pyfly.kernel import ResourceNotFoundException
from pyfly.security import SecurityContext, secure
from pyfly.web import (
    Body,
    PathVar,
    QueryParam,
    Valid,
    get_mapping,
    post_mapping,
    request_mapping,
)

_NEWEST_FIRST = Sort.by("created_at").descending()


@rest_controller
@request_mapping("/api/v1/wallets")
class WalletController:
    """Digital-wallet REST API: open, deposit, withdraw, list, inspect."""

    def __init__(
        self,
        commands: DefaultCommandBus,
        queries: DefaultQueryBus,
    ) -> None:
        self._commands = commands
        self._queries = queries

    # Any authenticated user may open a wallet.
    @secure(roles=["USER", "ADMIN"])
    @post_mapping("", status_code=201)
    async def open_wallet(
        self,
        request: Valid[Body[OpenWalletRequest]],
        security_context: SecurityContext,
    ) -> dict[str, str]:
        wallet_id = await self._commands.send(
            OpenWallet(
                owner_id=request.owner_id,
                currency=request.currency,
            )
        )
        return {"wallet_id": wallet_id}

    # Deposit: USER/ADMIN role + wallet:deposit permission.
    @secure(roles=["USER", "ADMIN"], permissions=["wallet:deposit"])
    @post_mapping("/{wallet_id}/deposit")
    async def deposit(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
        security_context: SecurityContext,
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            DepositFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # Withdraw: same guard as deposit.
    @secure(roles=["USER", "ADMIN"], permissions=["wallet:deposit"])
    @post_mapping("/{wallet_id}/withdraw")
    async def withdraw(
        self,
        wallet_id: PathVar[str],
        request: Valid[Body[DepositRequest]],
        security_context: SecurityContext,
    ) -> dict[str, int | str]:
        balance = await self._commands.send(
            WithdrawFunds(wallet_id=wallet_id, amount=request.amount)
        )
        return {"wallet_id": wallet_id, "balance_minor": balance}

    # Paged list — collection routes registered before /{wallet_id}.
    @secure(roles=["USER", "ADMIN"])
    @get_mapping("")
    async def list_wallets(
        self,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
        security_context: SecurityContext = None,
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListWallets(
                pageable=Pageable.of(page, size, _NEWEST_FIRST)
            )
        )
        return PageDto.from_page(result)

    # Rich list: wallets filtered by minimum balance.
    @secure(roles=["USER", "ADMIN"])
    @get_mapping("/rich")
    async def list_rich_wallets(
        self,
        min_minor: QueryParam[int] = 0,
        page: QueryParam[int] = 1,
        size: QueryParam[int] = 20,
        security_context: SecurityContext = None,
    ) -> PageDto[WalletDto]:
        result = await self._queries.query(
            ListRichWallets(
                min_minor=min_minor,
                pageable=Pageable.of(page, size, _NEWEST_FIRST),
            )
        )
        return PageDto.from_page(result)

    # Single-wallet balance: any authenticated user.
    @secure(roles=["USER", "ADMIN"])
    @get_mapping("/{wallet_id}/balance")
    async def wallet_balance(
        self,
        wallet_id: PathVar[str],
        security_context: SecurityContext,
    ) -> BalanceDto:
        result = await self._queries.query(
            GetBalance(wallet_id=wallet_id)
        )
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

    # Full wallet view: ADMIN only.
    @secure(roles=["ADMIN"])
    @get_mapping("/{wallet_id}")
    async def wallet_detail(
        self,
        wallet_id: PathVar[str],
        security_context: SecurityContext,
    ) -> WalletDto:
        result = await self._queries.query(
            GetWallet(wallet_id=wallet_id)
        )
        if result is None:
            raise ResourceNotFoundException(
                f"Wallet {wallet_id!r} not found",
                code="WALLET_NOT_FOUND",
                context={"wallet_id": wallet_id},
            )
        return result

:::
:::

**Cómo funciona.** Apila `@secure` **encima** de `@post_mapping` / `@get_mapping`
para que la autorización se ejecute antes que la vinculación de la ruta. El framework inyecta
`security_context` desde `request.state.security_context`, que
`SecurityMiddleware` ya rellenó.

Cuando listas varios roles, el usuario necesita **al menos uno** (semántica OR).
Cuando listas varios permisos, el usuario necesita **todos** ellos (semántica
AND). Cuando suministras tanto `roles` como `permissions`, ambas comprobaciones deben
pasar de forma independiente.

Los manejadores de colección (`list_wallets`, `list_rich_wallets`) ordenan
alfabéticamente antes que `wallet_balance` / `wallet_detail`, de modo que el framework
registra primero las rutas literales `""` y `/rich`, garantizando que Starlette
resuelva `/api/v1/wallets/rich` como un segmento fijo y no como un id de monedero.

Para añadir `@secure` a un manejador, la receta es siempre los mismos tres pasos:

**Paso 1 — Añade el parámetro.** Dale al método un parámetro de palabra clave `security_context: SecurityContext`. (Para los manejadores de listado `GET` que ya tienen parámetros de consulta con valor por defecto, dale como valor por defecto `None` para que el orden de los parámetros siga siendo válido: `security_context: SecurityContext = None`.)

**Paso 2 — Apila el decorador.** Pon `@secure(...)` *encima* de `@get_mapping` / `@post_mapping`. El orden importa: la comprobación de autorización debe ejecutarse antes de que lo haga la vinculación de la ruta.

**Paso 3 — Elige la protección mínima.** Escoge la regla de mínimo privilegio que aun así deje pasar a los usuarios correctos: `roles=["USER", "ADMIN"]` para las lecturas ordinarias, un `permissions=["wallet:deposit"]` adicional para las escrituras que mueven dinero, `roles=["ADMIN"]` para la vista de detalle completa.

!!! tip "Pruébalo — observa un 403 por el rol incorrecto"
    `wallet_detail` es solo para `ADMIN`. Llámalo con un token `USER` (la
    autenticación tiene éxito, pero la autorización falla):

    ```bash
    curl -i -H "Authorization: Bearer $USER_TOKEN" \
      http://localhost:8080/api/v1/wallets/abc-123
    ```

    Esperado — la petición supera la puerta (*sí* está autenticada) pero
    `@secure(roles=["ADMIN"])` la rechaza. El manejador de excepciones renderiza un
    cuerpo de problema que incluye el `code` legible por máquina:

    ```text
    HTTP/1.1 403 Forbidden
    content-type: application/problem+json

    {"type":"about:blank","title":"Forbidden","status":403,
     "detail":"Insufficient roles: requires one of ['ADMIN']",
     "code":"FORBIDDEN"}
    ```

    Observa la diferencia con el `401` de la puerta de antes: un token ausente o inválido
    en la puerta da `401` ("no sé quién eres"), mientras que un token válido
    sin el rol requerido da `403 FORBIDDEN` ("sé quién eres,
    y no puedes hacer esto"). El mismo token `USER` *sí* tendría éxito contra
    `GET /api/v1/wallets/abc-123/balance`, que está protegido con
    `roles=["USER", "ADMIN"]`.

!!! note "Importes en unidades menores"
    `DepositRequest.amount` es un `int` en **unidades menores** (céntimos). 10,50 € son
    `1050`. Esta convención evita errores de redondeo de coma flotante en todo el
    dominio Money. `WalletDto.balance_minor` lleva el mismo entero;
    `WalletDto.balance` es un `float` renderizado solo para mostrar.

### Autorización basada en expresiones

Para políticas que no pueden expresarse con una lista plana de roles, usa el parámetro `expression`. PyFly evalúa las expresiones mediante un parseo AST seguro: no hay `eval()` ni `exec()` en ningún punto de la cadena.

::: listing lumen/web/controllers/wallet_controller.py | Listado 14.6 — Expresiones de seguridad en endpoints de monederos
from pyfly.security import SecurityContext, secure


# ADMIN, or a MANAGER who also holds wallet:write.
@secure(
    expression=(
        "hasRole('ADMIN')"
        " or (hasRole('MANAGER') and hasPermission('wallet:write'))"
    )
)
async def approve_large_deposit(
    self,
    deposit_id: str,
    security_context: SecurityContext,
) -> None:
    ...


# Any authenticated, non-guest user can see the wallet dashboard.
@secure(expression="isAuthenticated and not hasRole('GUEST')")
async def dashboard(
    self,
    security_context: SecurityContext,
) -> dict:
    ...

:::
:::

Vocabulario de expresiones soportado (conjunto completo):

| Token | Descripción |
|---|---|
| `hasRole('X')` | El usuario tiene el rol `X` |
| `hasAnyRole('X', 'Y')` | El usuario tiene al menos uno de los roles listados |
| `hasAuthority('X')` | El usuario tiene el rol **o** el permiso `X` |
| `hasAnyAuthority('X', 'Y')` | Al menos uno de los roles/permisos listados |
| `hasPermission('X')` | El usuario tiene el permiso `X` |
| `isAuthenticated` | El usuario está autenticado |
| `isAnonymous` | El usuario **no** está autenticado |
| `permitAll` | Permite siempre |
| `denyAll` | Deniega siempre |
| `principal` / `authentication` | El objeto `SecurityContext` actual |
| `and` / `or` / `not` | Operadores booleanos |
| `(...)` | Agrupación |

!!! note "Las expresiones son seguras"
    PyFly reduce cada constructo a `True` o `False` y luego evalúa solo un
    AST booleano puro. Cualquier nodo que no sea una constante, un `BoolOp` o un
    `UnaryOp(Not)` lanza `SecurityException(code="INVALID_EXPRESSION")`.
    Esto elimina por completo los riesgos de inyección.

### Aplicar @secure a manejadores CQRS

`@secure` no se limita a los controladores REST. Puedes proteger manejadores de comandos CQRS exactamente de la misma forma: se dispara antes del cuerpo del manejador porque el contenedor de DI inyecta `security_context` desde `request.state.security_context` cuando resuelve el manejador:

::: listing lumen/core/services/wallets/deposit_funds_handler.py | Listado 14.7 — @secure en un manejador de comandos CQRS
from pyfly.cqrs import command_handler
from pyfly.security import SecurityContext, secure

from lumen.core.services.wallets.deposit_funds_command import DepositFunds


@command_handler
class DepositFundsHandler:

    @secure(roles=["USER", "ADMIN"], permissions=["wallet:deposit"])
    async def handle(
        self,
        command: DepositFunds,
        security_context: SecurityContext,
    ) -> int:
        # command.amount is in minor units (cents)
        ...

:::
:::

La comprobación se dispara antes de que se ejecute cualquier lógica de negocio.

---

## Contraseñas

### ¿Por qué bcrypt?

MD5 y SHA-256 están diseñados para ser rápidos: ideal para la integridad de datos, catastrófico para las contraseñas. Un atacante que robe tu tabla de usuarios puede probar miles de millones de conjeturas SHA-256 por segundo en hardware de consumo. **Bcrypt** es deliberadamente lento y de coste ajustable: el factor de coste (rounds) te permite afinar el algoritmo para que un ataque requiera órdenes de magnitud más de tiempo, sin afectar de forma apreciable a la latencia normal del inicio de sesión.

### BcryptPasswordEncoder

**`BcryptPasswordEncoder`** implementa el protocolo `PasswordEncoder` (un Protocol `runtime_checkable` con métodos `hash` y `verify`):

::: listing lumen/auth/password_service.py | Listado 14.8 — Hashear y verificar contraseñas
from pyfly.security import BcryptPasswordEncoder

encoder = BcryptPasswordEncoder(rounds=12)

# During registration — store only the hash, never the raw password
hashed = encoder.hash("correct-horse-battery-staple")

# During login — verify without storing the plaintext
is_match = encoder.verify("correct-horse-battery-staple", hashed)
# True

is_match = encoder.verify("wrong-password", hashed)
# False

:::
:::

| Parámetro | Por defecto | Notas |
|---|---|---|
| `rounds` | `12` | Cada incremento duplica el tiempo de hashing. 12 es el valor por defecto recomendado para producción. |

**Cómo funciona.** `hash` llama a `bcrypt.gensalt(rounds=self._rounds)` para generar una sal aleatoria nueva y luego a `bcrypt.hashpw` para producir el hash. Tanto la sal como el hash quedan incrustados en la cadena devuelta: el prefijo `$2b$12$…` codifica la versión del algoritmo y el factor de coste, de modo que cada hash almacenado se autodescribe. `verify` llama a `bcrypt.checkpw`, que vuelve a derivar el hash a partir de la contraseña en claro y la sal incrustada, y compara el resultado con una comprobación de igualdad segura frente a temporización, evitando ataques de oráculo de tiempo.

### El protocolo PasswordEncoder

`PasswordEncoder` es un Protocol `runtime_checkable`. Cualquier clase que implemente `hash(raw: str) -> str` y `verify(raw: str, hashed: str) -> bool` lo satisface, incluida `BcryptPasswordEncoder`. Puedes sustituirlo por argon2 o scrypt en cualquier momento sin tocar el código de servicio:

```python
from pyfly.security import PasswordEncoder

class Argon2PasswordEncoder:
    def hash(self, raw: str) -> str: ...
    def verify(self, raw: str, hashed: str) -> bool: ...

isinstance(Argon2PasswordEncoder(), PasswordEncoder)  # True
```

!!! tip "Autoconfiguración"
    Cuando `pyfly.security.enabled=true` y `bcrypt` está instalado, PyFly
    autoconfigura un bean `BcryptPasswordEncoder` con `rounds` leído de
    `pyfly.security.password.bcrypt-rounds`. Declara tú mismo el bean en una
    clase `@configuration` para sobreescribirlo sin tocar la autoconfiguración.

---

## Sesiones

### ¿Por qué sesiones del lado del servidor?

Los tokens JWT son sin estado: una vez emitidos, no pueden revocarse antes de que caduquen. Si un usuario cierra sesión, su token sigue siendo válido hasta `exp`. Para muchas API ese compromiso es aceptable. Para aplicaciones de cara al navegador —el panel de administración de Lumen, por ejemplo— necesitas que el servidor sea la fuente autoritativa: cerrar sesión debe significar cerrar sesión.

Las **sesiones del lado del servidor** te dan ese control. Un `SessionStore` guarda los datos de sesión indexados por un ID de sesión aleatorio. El navegador recibe solo el ID de sesión en una cookie. El servidor revoca una sesión al instante eliminando su entrada del almacén.

### HttpSession

**`HttpSession`** envuelve el diccionario de datos de una sesión con accesores tipados y rastrea el estado de mutación para que el filtro sepa cuándo persistir:

| Propiedad / Método | Descripción |
|---|---|
| `id` | Identificador de sesión, cadena hex UUID |
| `is_new` | `True` si se creó durante esta petición |
| `created_at` | Marca de tiempo Unix (`float`) de la creación |
| `last_accessed` | Marca de tiempo Unix (`float`) del acceso más reciente |
| `modified` | `True` si se escribió algún atributo o la sesión es nueva |
| `invalidated` | `True` si se llamó a `invalidate()` |
| `previous_id` | Id anterior tras `rotate_id()` (lo usa el filtro para limpiar la entrada antigua) |
| `get_attribute(name)` | Devuelve el valor del atributo o `None` |
| `set_attribute(name, value)` | Escribe un atributo; marca la sesión como modificada |
| `remove_attribute(name)` | Elimina el atributo si existe |
| `get_attribute_names()` | Lista los nombres de atributos puestos por el usuario (excluye las claves internas `_*`) |
| `rotate_id()` | Asigna un id de sesión nuevo, preservando todos los datos |
| `invalidate()` | Marca para eliminación en la siguiente pasada del filtro |
| `get_data()` | Devuelve el diccionario de sesión en bruto (incluye los metadatos internos) |

::: listing lumen/core/services/auth/session_handler.py | Listado 14.9 — Usar la sesión tras el inicio de sesión
from pyfly.session import HttpSession


async def post_login(
    username: str,
    password: str,
    session: HttpSession,
    auth_service,
) -> dict:
    user = await auth_service.authenticate(username, password)

    # Rotate the id before writing auth state (session-fixation prevention)
    session.rotate_id()
    session.set_attribute("user_id", str(user.id))
    session.set_attribute("role", user.role)

    return {"message": "Logged in"}


async def logout(session: HttpSession) -> dict:
    session.invalidate()
    return {"message": "Logged out"}

:::
:::

**Cómo funciona.** `rotate_id()` genera un ID de sesión UUID nuevo y registra el antiguo en `session.previous_id`. Cuando `SessionFilter` persiste la sesión al final de la petición, elimina la entrada antigua del almacén (el ID anterior ya no puede resolver a esta sesión) y guarda la nueva. Un atacante que hubiera obtenido el ID de sesión previo a la autenticación no puede arrastrarlo a la sesión autenticada: la clásica mitigación de la **fijación de sesión** (session-fixation).

### El protocolo SessionStore

Todos los backends implementan el protocolo `SessionStore`:

```python
class SessionStore(Protocol):
    async def get(
        self, session_id: str
    ) -> dict[str, Any] | None: ...

    async def save(
        self, session_id: str,
        data: dict[str, Any],
        ttl: int,
    ) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    async def exists(self, session_id: str) -> bool: ...
```

Vienen dos adaptadores de serie:

| Adaptador | Módulo | Notas |
|---|---|---|
| `InMemorySessionStore` | `pyfly.session.adapters.memory` | Protegido con `asyncio.Lock`; solo monoproceso; los datos se pierden al reiniciar |
| `RedisSessionStore` | `pyfly.session.adapters.redis` | Serializado en JSON; claves con prefijo `pyfly:session:`; TTL gestionado por Redis |

### SessionFilter

**`SessionFilter`** es un `OncePerRequestFilter` ordenado en `HIGHEST_PRECEDENCE + 150`. Enmarca cada petición:

1. Lee la cookie de sesión (`PYFLY_SESSION` por defecto).
2. Carga la sesión del almacén, o crea una nueva.
3. La adjunta a `request.state.session`.
4. Llama a `call_next(request)`: se ejecuta el resto de la cadena de filtros y el manejador.
5. A la vuelta, persiste una sesión modificada o nueva, elimina una invalidada y reemite la cookie con un `max_age` deslizante (el TTL se desplaza hacia adelante en cada petición).

Atributos de cookie que establece `SessionFilter`:

| Atributo | Valor | Razón |
|---|---|---|
| `httponly` | `True` | Impide el acceso desde JavaScript; mitigación de XSS |
| `samesite` | `lax` | Bloquea la mayoría de los flujos de falsificación de petición entre sitios |
| `secure` | configurable | Pon `True` en producción (solo HTTPS) |
| `max_age` | `ttl` | Caducidad deslizante |

### Sesiones de Redis en producción

::: listing lumen/config/session_config.py | Listado 14.10 — Almacén de sesiones en Redis
import redis.asyncio as aioredis

from pyfly.container import bean, configuration
from pyfly.session import SessionFilter
from pyfly.session.adapters.redis import RedisSessionStore


@configuration
class SessionConfig:

    @bean
    def session_store(self) -> RedisSessionStore:
        client = aioredis.from_url(
            "redis://localhost:6379/0"
        )
        return RedisSessionStore(client=client)

    @bean
    def session_filter(
        self,
        store: RedisSessionStore,
    ) -> SessionFilter:
        return SessionFilter(
            store=store,
            cookie_name="LUMEN_SESSION",
            ttl=1800,
            secure=True,
        )

:::
:::

**Cómo funciona.** `RedisSessionStore.save` serializa en JSON el diccionario de sesión (incluidos atributos dataclass como `SecurityContext`, que hacen ida y vuelta mediante un mecanismo de etiquetas de tipo con lista de permitidos) y llama a `client.set(key, raw, ex=ttl)`. El TTL lo gestiona enteramente Redis, de modo que las sesiones caducadas desaparecen del lado del servidor sin ninguna sobrecarga de limpieza. Las claves usan el prefijo `pyfly:session:` para aislamiento de espacio de nombres. Al leer, solo se deserializan los tipos de la lista de permitidos, eliminando los riesgos de instanciación de objetos arbitrarios.

!!! tip "Autoconfiguración"
    Añade `pyfly.session.enabled: true` y `pyfly.session.store: redis` a
    `pyfly.yaml`. PyFly autoconfigura `RedisSessionStore` (cuando
    `redis.asyncio` está instalado) y `SessionFilter` por ti. La
    `redis.url` por defecto es `redis://localhost:6379/0`; sobreescríbela con
    `pyfly.session.redis.url`.

!!! spring "Equivalencia con Spring"
    `SessionFilter` refleja el `SessionRepositoryFilter` de Spring Session.
    `HttpSession` refleja `javax.servlet.http.HttpSession` (o
    `jakarta.servlet.http.HttpSession` en Boot 3). `InMemorySessionStore`
    es equivalente al `MapSessionRepository` de Spring Session;
    `RedisSessionStore` es equivalente a `RedisSessionRepository`.
    Los atributos de cookie (`HttpOnly`, `SameSite=Lax`, `Max-Age` deslizante)
    coinciden exactamente con los valores por defecto de Spring Session.

---

## Validar tokens de un IdP externo (servidor de recursos OAuth2)

Hasta ahora Lumen ha *acuñado sus propios tokens* con `JWTService` y un
secreto HMAC compartido. Eso es perfecto para un servicio autocontenido. Pero en la mayoría de los
despliegues del mundo real, los tokens los emite un proveedor de identidad dedicado —Keycloak, Microsoft
Entra ID (antes Azure AD) o AWS Cognito— y el único trabajo de tu servicio es
**validarlos**. Este es el rol de **servidor de recursos OAuth2**, y a partir de la
v26.6.110 PyFly te lo da solo con configuración: sin código, y la misma
configuración funciona en los tres proveedores.

!!! note "Jerga: servidor de recursos"
    En términos de OAuth2, el IdP que emite tokens es el *servidor de autorización*;
    tu API, que custodia los datos que los llamantes quieren y comprueba sus tokens, es el
    *servidor de recursos*. Lumen es un servidor de recursos. Nunca ve la contraseña del
    usuario: solo verifica la credencial que el IdP ya firmó.

La diferencia con `JWTService` es el **esquema de firma**. Tus propios tokens se
firman con un secreto simétrico (`HS256`): la misma clave firma y verifica,
de modo que solo los servicios que poseen el secreto pueden validar. Los tokens del IdP se firman
de forma *asimétrica* (`RS256`): el IdP guarda una clave privada y publica las
claves públicas correspondientes en un endpoint **JWKS** (JSON Web Key Set). Tu servidor
de recursos descarga esas claves públicas, las cachea y las usa para verificar la firma de cada
token; nunca necesita el secreto del IdP en absoluto.

### Activarlo

`JWKSTokenValidator` hace el trabajo, y `OAuth2ResourceServerAutoConfiguration`
lo conecta desde `pyfly.security.oauth2.resource-server.*`. Aquí tienes la
configuración completa de Keycloak:

::: listing lumen/resources/pyfly.yaml | Listado 14.11 — Servidor de recursos OAuth2 multi-IdP (Keycloak)
pyfly:
  security:
    oauth2:
      resource-server:
        enabled: true
        # Either point at the JWKS endpoint directly...
        jwks-uri: "https://keycloak.example.com/realms/lumen/protocol/openid-connect/certs"
        # ...or give an issuer-uri and let OIDC discovery find jwks-uri + issuer:
        # issuer-uri: "https://keycloak.example.com/realms/lumen"
        issuer: "https://keycloak.example.com/realms/lumen"
        audiences: "lumen-backend"
        validate-audience: true
        algorithms: "RS256"
        clock-skew-seconds: 60
        exclude-patterns: "/idp/login,/idp/refresh,/docs,/openapi.json"

:::
:::

Paso a paso:

**Paso 1 — Actívalo.** `enabled: true` activa `OAuth2ResourceServerAutoConfiguration` (PyJWT debe estar instalado). Eso registra un bean `JWKSTokenValidator` y el filtro de token portador (bearer) en la cadena de la aplicación.

**Paso 2 — Apunta a las claves.** Establece `jwks-uri` directamente, o establece `issuer-uri` y deja que PyFly obtenga `<issuer-uri>/.well-known/openid-configuration` para descubrir tanto el `jwks-uri` como el `issuer` autoritativo, exactamente como el `issuer-uri` de Spring.

**Paso 3 — Fija la audiencia y el emisor.** `audiences` enumera los valores con los que debe coincidir el claim `aud` del token (cualquiera de ellos). `issuer` (cuando se establece) se comprueba contra el `iss` del token. Juntos garantizan que un token acuñado para una aplicación o realm *diferente* no pueda reenviarse contra Lumen.

**Paso 4 — Deja los valores por defecto para el mapeo de claims.** No configuraste dónde viven los roles y, aun así, funciona para Keycloak, Entra y Cognito. Ese es el trabajo de `ClaimMappings`, que veremos a continuación.

### Cómo los claims se convierten en un SecurityContext

Cada IdP pone los roles y scopes en sitios distintos. Keycloak anida los roles de realm bajo `realm_access.roles` y los roles por cliente bajo `resource_access.<client>.roles`; Entra usa un array plano `roles` (o `groups`); Cognito usa `cognito:groups`. `ClaimMappings` los reconcilia con un pequeño lenguaje de rutas, de modo que `has_role(...)` y `has_permission(...)` funcionan igual independientemente del proveedor.

| Campo de mapeo | Orden de búsqueda por defecto | Se convierte en |
|---|---|---|
| `principal_claims` | `oid`, `sub` | `SecurityContext.user_id` |
| `authority_claims` | `roles`, `scopes`, `authorities`, `realm_access.roles`, `resource_access.*.roles`, `groups`, `cognito:groups` | `SecurityContext.roles` |
| `scope_claims` | `scp`, `scope` (separados por espacios) | `SecurityContext.permissions` |
| `attribute_claims` | *(ninguno)* | `SecurityContext.attributes` |

Dos reglas hacen que los valores por defecto abarquen todos los proveedores:

- Las **rutas con puntos** descienden a objetos anidados: `realm_access.roles` lee `payload["realm_access"]["roles"]`.
- Un **comodín `*`** de un solo nivel itera sobre cada clave de ese nivel: `resource_access.*.roles` recopila el array `roles` de *cada* entrada de cliente bajo `resource_access`. Los segmentos de ruta se dividen solo por `.`, así que un nombre de claim que lleve dos puntos, como `cognito:groups`, se compara textualmente.

Los roles se recopilan de *todas* las rutas coincidentes y se deduplican, de modo que un token de Keycloak aporta tanto sus roles de realm como sus roles de cliente a una única lista plana `roles`.

!!! note "Jerga: JWKS y kid"
    Un *JWKS* es el directorio de claves públicas del IdP. Cada clave lleva un `kid` (key
    id); la cabecera de cada token nombra el `kid` con el que se firmó. El validador
    busca esa clave exacta, cachea el conjunto (por defecto 300 s) y rota
    automáticamente cuando el IdP publica claves nuevas: sin necesidad de redespliegue.

Si los valores por defecto integrados no coinciden con tu IdP, sobreescribe solo lo que difiera:

::: listing lumen/resources/pyfly.yaml | Listado 14.12 — Ajustar el mapeo de claims para Entra ID y Cognito
pyfly:
  security:
    oauth2:
      resource-server:
        enabled: true
        # Microsoft Entra ID (v2.0):
        issuer-uri: "https://login.microsoftonline.com/<tenant-id>/v2.0"
        audiences: "api://lumen-backend"
        principal-claim-names: "oid,sub"      # Entra's stable user id first
        authorities-claim-names: "roles,groups"
        scope-claim-names: "scp"              # Entra delegated scopes
        attribute-claims: "tid,preferred_username"
        # AWS Cognito access tokens carry no 'aud' — disable audience checks:
        # validate-audience: false
        # authorities-claim-names: "cognito:groups"

:::
:::

**Paso 5 (cuando sea necesario) — Cognito no tiene `aud`.** Los tokens de *acceso* de Cognito llevan `client_id` en lugar de `aud`, así que establece `validate-audience: false` para ellos; las comprobaciones de firma, `iss` y `exp` siguen aplicándose.

!!! tip "Pruébalo — acepta un token real del IdP, rechaza uno falsificado"
    Con el servidor de recursos activado, reenvía un token emitido por tu IdP contra una
    ruta protegida:

    ```bash
    curl -i -H "Authorization: Bearer $KEYCLOAK_TOKEN" \
      http://localhost:8080/api/v1/wallets
    ```

    Esperado — `HTTP/1.1 200 OK`. El filtro obtuvo el JWKS, encontró la
    coincidencia con el `kid` del token, verificó la firma `RS256`, el `iss`, el `aud` y el
    `exp` (con 60 s de tolerancia de desfase de reloj) y luego construyó un `SecurityContext`
    a partir de los claims del token.

    Ahora cambia un carácter del token y reinténtalo. Esperado — `HTTP/1.1 401`
    (o un contexto anónimo que la puerta rechaza después), porque la firma
    ya no coincide con ninguna clave publicada. No escribiste ni desplegaste una sola
    línea de código de validación para conseguir ninguno de los dos resultados.

**Lo que acaba de ocurrir.** Has añadido un validador de tokens multi-IdP de nivel de producción solo con configuración. `JWKSTokenValidator` verifica la firma contra las claves publicadas del IdP (de modo que el secreto nunca abandona el IdP), comprueba `iss` / `aud` / `exp` y mapea los claims específicos de cada proveedor al mismo `SecurityContext` que el resto del capítulo ya usa. Tus decoradores `@secure` y tus reglas `HttpSecurity` no cambian en absoluto: leen `roles` y `permissions` exactamente como antes, tanto si el token vino del propio `JWTService` de Lumen como si vino de Keycloak.

!!! warning "modo de error: anónimo vs 401"
    Por defecto (`authenticate-error-mode: anonymous`) un token *presente pero inválido*
    produce un contexto anónimo y la petición continúa hasta la puerta de
    `HttpSecurity`, que decide. Establece `authenticate-error-mode: "401"` para
    rechazar un token inválido directamente en el filtro con
    `WWW-Authenticate: Bearer error="invalid_token"` (RFC 6750). Un token *ausente*
    siempre cae hasta la puerta en cualquier caso.

!!! spring "Equivalencia con Spring"
    Este es el `spring-boot-starter-oauth2-resource-server` de PyFly. Las claves
    `resource-server.issuer-uri` / `jwks-uri` reflejan
    `spring.security.oauth2.resourceserver.jwt.*`; el descubrimiento OIDC, la lista de
    `audiences` aceptada, los `algorithms` configurables y el desfase de reloj por defecto de 60 segundos
    coinciden todos con el `JwtDecoder` y el `JwtTimestampValidator` de Spring Security.
    `ClaimMappings` es el equivalente de un
    `JwtAuthenticationConverter` / `JwtGrantedAuthoritiesConverter` personalizado, pero
    expresado de forma declarativa en YAML.

---

## Identidad externa (IDP)

### El problema de gestionar la identidad internamente

Lumen almacena actualmente las credenciales en su propia base de datos, lo que significa que Lumen debe implementar restablecimiento de contraseñas, MFA, verificación de correo, bloqueo de cuentas, borrado GDPR, inicio de sesión social y SSO: trabajo no diferenciador que todo servicio acaba necesitando. La respuesta del sector es delegar la identidad a un proveedor dedicado: Keycloak para entornos on-premise, AWS Cognito para pilas nativas de AWS, Azure AD para entornos de Microsoft.

El puerto **`IdpAdapter`** de PyFly hace esa delegación conectable tras una única interfaz. Cambia el adaptador y la capa de negocio nunca se entera.

### IdpAdapter — el puerto

Todo adaptador debe satisfacer el protocolo `IdpAdapter`:

```python
class IdpAdapter(Protocol):
    name: str

    # User management
    async def create_user(
        self, user: IdpUser, password: str
    ) -> IdpUser: ...
    async def get_user(self, user_id: str) -> IdpUser | None: ...
    async def find_by_username(
        self, username: str
    ) -> IdpUser | None: ...
    async def update_user(self, user: IdpUser) -> IdpUser: ...
    async def delete_user(self, user_id: str) -> bool: ...
    async def list_users(self, *, limit: int = 100) -> list[IdpUser]: ...

    # Authentication
    async def login(
        self, request: LoginRequest
    ) -> AuthResult: ...
    async def logout(self, access_token: str) -> bool: ...
    async def refresh(
        self, refresh_token: str
    ) -> AuthResult: ...
    async def introspect(
        self, access_token: str
    ) -> SessionIntrospection: ...

    # Password / MFA
    async def change_password(
        self, request: PasswordChangeRequest
    ) -> bool: ...
    async def reset_password(self, user_id: str) -> str: ...

    # Roles
    async def assign_role(
        self, user_id: str, role: str
    ) -> bool: ...
    async def revoke_role(
        self, user_id: str, role: str
    ) -> bool: ...
    async def list_roles(self) -> list[IdpRole]: ...
```

DTOs clave:

| Clase | Propósito |
|---|---|
| `IdpUser` | Registro de usuario: `id`, `username`, `email`, `roles`, `attributes`, … |
| `LoginRequest` | `username`, `password`, `mfa_code` (opcional) |
| `AuthResult` | `user`, `access_token`, `refresh_token`, `expires_in`, `token_type` |
| `SessionIntrospection` | `active`, `user_id`, `username`, `scopes`, `expires_at` |
| `PasswordChangeRequest` | `user_id`, `old_password`, `new_password` |
| `IdpRole` | `name`, `description`, `scopes` |

### Adaptador de Keycloak

::: listing lumen/config/idp_config.py | Listado 14.13 — Conectar el adaptador de Keycloak
from pyfly.container import bean, configuration
from pyfly.idp import IdpAdapter, KeycloakIdpAdapter


@configuration
class IdpConfig:

    @bean
    def idp_adapter(self) -> IdpAdapter:
        return KeycloakIdpAdapter(
            base_url="https://keycloak.example.com",
            realm="lumen",
            client_id="lumen-backend",
            client_secret="${KEYCLOAK_SECRET}",
            verify_ssl=True,
        )

:::
:::

**Cómo funciona.** `KeycloakIdpAdapter` se comunica con la API REST de administración de Keycloak (`/admin/realms/{realm}/users`) y con el endpoint de tokens (`/realms/{realm}/protocol/openid-connect/token`) vía `httpx`. Cachea internamente el token de administración `client_credentials` y lo vuelve a obtener dentro de un margen de seguridad de diez segundos antes de la caducidad; el TTL por defecto de las client-credentials de Keycloak es de 60 s, así que sin esta caché cada llamada de administración requeriría dos viajes de ida y vuelta por red.

### Usar el IDP en un servicio

::: listing lumen/core/services/auth/idp_auth_service.py | Listado 14.14 — Usar IdpAdapter en el servicio de autenticación
from pyfly.container import service
from pyfly.idp import IdpAdapter, IdpUser, LoginRequest
from pyfly.kernel.exceptions import UnauthorizedException


@service
class IdpAuthService:

    def __init__(self, idp: IdpAdapter) -> None:
        self._idp = idp

    async def register(
        self,
        username: str,
        email: str,
        password: str,
        role: str = "USER",
    ) -> str:
        user = IdpUser(
            username=username,
            email=email,
            roles=[role],
        )
        created = await self._idp.create_user(user, password)
        result = await self._idp.login(
            LoginRequest(
                username=username, password=password
            )
        )
        return result.access_token

    async def login(
        self, username: str, password: str
    ) -> str:
        try:
            result = await self._idp.login(
                LoginRequest(
                    username=username, password=password
                )
            )
        except PermissionError as exc:
            raise UnauthorizedException(
                "Invalid credentials",
                code="INVALID_CREDENTIALS",
            ) from exc
        return result.access_token

    async def introspect(self, token: str) -> dict:
        info = await self._idp.introspect(token)
        return {
            "active": info.active,
            "user_id": info.user_id,
            "username": info.username,
            "scopes": info.scopes,
        }

:::
:::

**Cómo funciona.** `IdpAuthService` depende solo de `IdpAdapter`: el contenedor de DI resuelve el `KeycloakIdpAdapter` concreto en el arranque. La capa de servicio nunca importa código específico de Keycloak, Cognito ni Azure. Cambia de proveedor modificando una sola línea en `IdpConfig`; el servicio queda intacto.

### Autoconfiguración y las rutas HTTP integradas

Activa el subsistema del IDP en `pyfly.yaml` y PyFly conecta el adaptador y un controlador REST ya hecho de forma automática:

::: listing lumen/resources/pyfly.yaml | Listado 14.15 — Autoconfiguración del IDP
pyfly:
  idp:
    enabled: true
    provider: keycloak
    keycloak:
      base-url: https://keycloak.example.com
      realm: lumen
      client-id: lumen-backend
      client-secret: "${KEYCLOAK_SECRET}"

:::
:::

| Valor de `provider` | Adaptador |
|---|---|
| `internal-db` | `InternalDbIdpAdapter` (almacén bcrypt en memoria) |
| `keycloak` | `KeycloakIdpAdapter` |
| `cognito` / `aws-cognito` | `AwsCognitoIdpAdapter` |
| `azure-ad` / `azuread` / `entra` | `AzureAdIdpAdapter` |

Cuando Starlette está presente, `IdpAutoConfiguration` también registra un bean `IdpController` que expone la API completa del IDP bajo `/idp`:

| Ruta | Método | Descripción |
|---|---|---|
| `/idp/login` | POST | Autenticar (usuario + contraseña + MFA opcional) |
| `/idp/refresh` | POST | Refrescar un token de acceso |
| `/idp/logout` | POST | Revocar un token |
| `/idp/introspect` | POST | Inspeccionar una sesión activa |
| `/idp/admin/users` | POST | Crear un usuario |
| `/idp/admin/users` | GET | Listar usuarios |
| `/idp/admin/users/{user_id}` | GET / DELETE | Obtener o eliminar un usuario |
| `/idp/admin/users/{user_id}/roles/{role}` | POST / DELETE | Asignar o revocar un rol |
| `/idp/admin/roles` | GET | Listar todos los roles |

!!! tip "Adaptadores personalizados"
    Cualquier clase que satisfaga el Protocol `IdpAdapter` puede conectarse como el
    bean `IdpAdapter`. Regístralo en una clase `@configuration` y el
    `@conditional_on_missing_bean` de PyFly se salta la autoconfiguración por completo. Este
    es el punto de extensión estándar para LDAP on-premise, SSO interno o
    adaptadores de doble de prueba (test-double).

!!! spring "Equivalencia con Spring"
    `IdpAdapter` es el equivalente en PyFly de la combinación
    `UserDetailsService` + `AuthenticationProvider` de Spring Security. `IdpUser`
    se corresponde con `UserDetails`; `AuthResult` se corresponde con el objeto `Authentication`
    devuelto por `AuthenticationManager.authenticate()`. `KeycloakIdpAdapter`
    desempeña el papel del adaptador de Spring Security para Keycloak.

---

## Juntándolo todo — la capa de autenticación de Lumen

El listado siguiente muestra la conexión completa: adaptador del IDP, filtro JWT, reglas a nivel de URL y un almacén de sesiones en Redis para el panel de administración.

::: listing lumen/config/security_full.py | Listado 14.16 — Configuración de seguridad completa
from pyfly.container import bean, configuration
from pyfly.idp import IdpAdapter, KeycloakIdpAdapter
from pyfly.security.http_security import HttpSecurity


@configuration
class LumenSecurityConfig:

    @bean
    def idp_adapter(self) -> IdpAdapter:
        return KeycloakIdpAdapter(
            base_url="https://keycloak.example.com",
            realm="lumen",
            client_id="lumen-backend",
            client_secret="${KEYCLOAK_SECRET}",
        )

    @bean
    def http_security(self) -> HttpSecurity:
        hs = HttpSecurity()
        hs.authorize_requests() \
            .request_matchers(
                "/idp/login", "/idp/refresh",
                "/docs", "/openapi.json",
            ).permit_all() \
            .request_matchers(
                "/idp/admin/**"
            ).has_role("ADMIN") \
            .request_matchers(
                "/api/v1/wallets/**"
            ).authenticated() \
            .any_request().permit_all()
        return hs

:::
:::

Con `pyfly.security.enabled=true`, `pyfly.session.enabled=true` y `pyfly.session.store=redis` en `pyfly.yaml`, la autoconfiguración se encarga de `JWTService`, `BcryptPasswordEncoder`, `SessionFilter` y `RedisSessionStore`. La clase `@configuration` de arriba aporta solo lo que la autoconfiguración no puede inferir: las coordenadas de Keycloak y la política a nivel de URL.

---

## Lo que construiste {.recap}

Este capítulo abrió la Parte V cerrando la puerta de entrada abierta de Lumen. Tú:

- Usaste **`JWTService`** para emitir tokens firmados al iniciar sesión y para decodificarlos de vuelta
  a un `SecurityContext` en cada petición posterior. El claim `exp` es
  obligatorio: `encode()` lo añade automáticamente usando una marca de tiempo Unix, de modo que nunca
  necesitas importar `datetime`; los tokens sin `exp` se rechazan en la frontera.
- Añadiste **`SecurityMiddleware`** a la aplicación Starlette para que toda petición
  lleve un `SecurityContext` rellenado en el momento en que alcanza un manejador.
- Declaraste la política a nivel de URL con el constructor **`HttpSecurity`**: un DSL fluido
  que produce un `HttpSecurityFilter` evaluado antes del despachador de rutas,
  cubriendo el árbol real `/api/v1/wallets/**` de Lumen y las rutas de administración del IDP.
- Protegiste los endpoints de monederos reales de Lumen en `WalletController` con
  **`@secure`**, especificando roles, permisos o expresiones de seguridad completas.
  Las siete rutas —`open_wallet`, `deposit`, `withdraw`, `list_wallets`
  (listado paginado), `list_rich_wallets` (listado filtrado), `wallet_balance` y
  `wallet_detail`— llevan cada una la protección mínima: USER+ADMIN para la mayoría,
  el permiso `wallet:deposit` para las mutaciones, solo ADMIN para la vista de detalle
  completa. Cuando `@secure` rechaza una llamada lanza `SecurityException` (código
  `AUTH_REQUIRED`) para un llamante no autenticado o `ForbiddenException` (código
  `FORBIDDEN`) para un llamante autenticado que carece del rol o permiso
  requerido: ambas se renderizan como HTTP `403` a través del manejador de excepciones. La
  puerta más amplia `HttpSecurity`, situada delante de esos manejadores, es la que
  devuelve el `401` escueto para un token ausente o inválido.
- Hasheaste contraseñas con **`BcryptPasswordEncoder`**, el adaptador por defecto del
  protocolo `PasswordEncoder`. El factor de coste es ajustable; el hash almacenado se
  autodescribe; la verificación es segura frente a temporización.
- Gestionaste sesiones del lado del servidor con **`HttpSession`** y el protocolo conectable
  **`SessionStore`**. En desarrollo, `InMemorySessionStore` no requiere
  dependencias; en producción, `RedisSessionStore` serializa a JSON y
  deja que Redis gestione el TTL. El `SessionFilter` desplaza el TTL de la cookie en cada
  petición y la elimina al invalidarse.
- Validaste tokens emitidos por un IdP externo con el **servidor de recursos OAuth2**
  guiado por configuración (`JWKSTokenValidator`). Un bloque de
  configuración `pyfly.security.oauth2.resource-server.*` acepta tokens de Keycloak, Microsoft
  Entra ID y AWS Cognito de serie: verifica la firma `RS256`
  contra las claves JWKS del IdP, comprueba `iss` / `aud` / `exp` con un
  desfase de reloj de 60 segundos y mapea los roles, scopes y principal de cada proveedor
  al mismo `SecurityContext` vía `ClaimMappings`, de modo que `@secure` y
  `HttpSecurity` quedan inalterados.
- Delegaste la identidad a un proveedor externo vía el puerto **`IdpAdapter`** y
  la implementación **`KeycloakIdpAdapter`**. El `IdpController` autoconfigurado
  expone inicio de sesión, refresco, cierre de sesión, introspección y gestión de usuarios
  de administración bajo `/idp` sin código adicional.
- Aprendiste que el actuator y el panel de administración se ejecutan en un **puerto de gestión
  separado** (`pyfly.management.server.port`, por defecto `9090`) que está
  **no autenticado por defecto** por paridad con Spring, y que lo aseguras con
  `pyfly.management.security.enabled: true` (o lo deshabilitas por completo con
  el puerto `-1`).

---

## Pruébalo tú mismo {.exercises}

**Ejercicio 1 — Jerarquía de roles.** Lumen trata actualmente `ADMIN` y `USER` como
roles independientes. Añade un rol de "superusuario" `SUPER` que posea implícitamente todos los
privilegios de `ADMIN`. Implementa un envoltorio `RoleHierarchy` que pre-expanda los roles
antes de almacenarlos en `SecurityContext`, y actualiza `_permissions_for` en el
Listado 14.1 para que un token con rol `SUPER` pase toda comprobación `@secure(roles=["ADMIN"])`
sin llevar explícitamente el rol `ADMIN`. Escribe una prueba unitaria que
cree un `SecurityContext(roles=["SUPER"])` tras la expansión y afirme que
`has_any_role(["ADMIN"])` devuelve `True`.

**Ejercicio 2 — Control de concurrencia de sesiones.** Los usuarios de administración de Lumen no deben estar
con sesión iniciada en más de dos dispositivos al mismo tiempo (un requisito habitual de
cumplimiento financiero). Activa `pyfly.session.concurrency.enabled: true` con
`max-sessions: 2` y `strategy: evict-oldest` en `pyfly.yaml`. Escribe una
prueba de integración que use `InMemorySessionStore` y que cree tres sesiones para
el mismo `user_id`, llame al `SessionConcurrencyController` y afirme que
la sesión más antigua ha sido expulsada mientras las dos más nuevas siguen siendo válidas.

**Ejercicio 3 — Adaptador de IDP personalizado.** El entorno de staging de Lumen usa un
servidor OAuth2 casero. Implementa un `StagingIdpAdapter` que satisfaga
`IdpAdapter`, respaldado por un diccionario de usuarios en memoria. El método `login` debería
emitir un JWT firmado usando un `JWTService` inyectado a través del constructor. Conéctalo
como el bean `IdpAdapter` en una clase `@configuration` etiquetada con
`@conditional_on_property("lumen.env", having_value="staging")` y confirma
que el bean de producción `KeycloakIdpAdapter` no se crea cuando esa propiedad
está activa.
