## Prefacio

El Python empresarial ha significado durante mucho tiempo ensamblar una docena de bibliotecas independientes —una para inyección de dependencias, otra para enrutamiento, otra más para acceso asíncrono a la base de datos— sin un idioma común que las una. **PyFly** cambia eso. Ofrece la experiencia cohesionada de convención sobre configuración que Spring Boot dio al mundo Java, reconstruida desde cero para Python 3.12+ y `async`/`await`.

Este libro enseña PyFly **haciendo**. Construyes una aplicación real desde una carpeta vacía hasta un servicio seguro, observable y orientado a eventos, haciendo concreto cada concepto antes de pasar al siguiente. Y lo más importante: el código de estas páginas no es pseudocódigo ilustrativo, sino que está tomado de un **proyecto real que compila, arranca y supera sus pruebas** con PyFly v26.6.110. Cada listado se verificó contra el ejemplo en ejecución, de modo que lo que lees es lo que realmente funciona.

### Para quién es este libro

Este libro es para desarrolladores de Python de nivel intermedio que se sienten cómodos con `async`/`await`, las anotaciones de tipos y los fundamentos de los servicios HTTP. No necesitas experiencia previa con frameworks: si has construido algo con FastAPI, Flask o SQLAlchemy, estás bien preparado.

Los desarrolladores de Spring Boot se sentirán especialmente como en casa. Allí donde PyFly refleja un concepto de Spring —beans, estereotipos, transacciones declarativas, eventos de aplicación— una llamada de **Equivalencia con Spring** traza el paralelismo de forma explícita, para que mapees lo que ya sabes en lugar de aprender desde cero.

### Lo que vas a construir

Cada capítulo hace avanzar **Lumen**, un servicio de monedero (wallet) digital y libro mayor. El recorrido sigue un arco deliberado, una parte cada vez:

- **Inicio rápido — Construye Lumen paso a paso.** Antes de la inmersión profunda, un único recorrido guiado te lleva desde una carpeta vacía hasta una funcionalidad de monedero en ejecución y probada —abrir un monedero, depositar, leer el saldo por HTTP— para que veas la forma completa de una aplicación PyFly antes de centrarte en cualquier parte concreta. Cada capítulo posterior expande luego una porción de lo que construiste aquí.
- **Parte I — Fundamentos (Capítulos 1–4).** Generas el andamiaje del primer servicio Lumen con `pyfly new`, lo ejecutas bajo un servidor ASGI, conectas el contenedor de inyección de dependencias de PyFly, vinculas configuración tipada y perfiles, y expones tus primeros endpoints REST validados.
- **Parte II — Modelar y persistir (Capítulos 5–7).** Introduces el patrón repositorio sobre un puerto, persistes monederos con SQLAlchemy asíncrono (SQLite, sin necesidad de infraestructura), modelas el dominio con un objeto de valor `Money` y una raíz de agregado `Wallet`, y separas las lecturas de las escrituras con manejadores de comando y consulta de CQRS despachados a través de un bus.
- **Parte III — Orientada a eventos (Capítulos 8–10).** El agregado emite eventos de dominio; un escuchador los proyecta; un **libro mayor con event sourcing** (un patrón en el que el estado se reconstruye a partir de un flujo de eventos) reconstruye cada saldo reproduciendo su flujo de eventos; y esos mismos eventos fluyen hacia Kafka o RabbitMQ para otros servicios.
- **Parte IV — Hacia los microservicios (Capítulos 11–13).** Lumen va más allá de su propio proceso: un cliente HTTP tipado llama a un servicio externo de Pagos, una **saga de transferencia** orquestada mueve dinero entre monederos y *compensa* cuando un paso falla, y los patrones de caché y resiliencia mantienen el sistema rápido y tolerante a fallos.
- **Parte V — Asegurar · Observar · Publicar (Capítulos 14–18).** Aseguras los endpoints con JWT y `@secure`, haces el servicio observable con métricas, trazas, comprobaciones de salud y el panel de administración, pruebas toda la pila, lo conectas con el mundo exterior mediante programación, notificaciones y webhooks, y finalmente lo extiendes y lo despliegas a producción.

Para la última página tendrás un servicio funcional, probado, observable y seguro, y el modelo mental para extenderlo.

### Cómo usar este libro

**Lee de forma secuencial.** Cada capítulo se apoya en el anterior, y la base de código de Lumen crece de forma incremental; saltarte capítulos deja huecos.

**Escribe tú mismo cada listado.** Leer y teclear el código a la vez es la manera de que los patrones se asienten. Resiste la tentación de copiar y pegar hasta que hayas escrito cada listado al menos una vez.

**Ejecútalo.** Lumen funciona de verdad: `uv run pyfly run` arranca el servicio y `uv run --extra dev pytest` lo ejercita. Cada vez que un capítulo añada una funcionalidad, arranca la aplicación o las pruebas y míralo funcionar. Ver JSON real que vuelve de un endpoint real vale por cien diagramas.

Cada capítulo cierra con un **Resumen** de lo que cambió en la base de código de Lumen y un conjunto de **Ejercicios** que dan un paso más allá. Los ejercicios son opcionales, pero recomendables para cualquier cosa que pretendas aplicar de inmediato.

### Convenciones en breve

Las convenciones tipográficas y estructurales —los pies de los listados de código, los tipos de llamada y la numeración de las figuras— se demuestran, con ejemplos en vivo, en la sección **Convenciones** que viene a continuación.

### El código de acompañamiento

El proyecto Lumen completo y ejecutable vive en el directorio `samples/lumen` del framework. Es un único proyecto PyFly por capas —`interfaces`, `models`, `core`, `web`— que haces crecer capítulo a capítulo; el código terminado que hay allí es el destino al que este libro te conduce. Configúralo una sola vez con `uv sync` y úsalo para comparar tu trabajo, ponerte al día si te quedas atrás o, simplemente, ejecutar las partes sobre las que estás leyendo.
