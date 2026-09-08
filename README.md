# HeroScalper

Detector de **errores de precio, promociones y liquidaciones** en tiendas online
españolas y europeas, orientado a comprar para revender en Wallapop / Vinted /
segunda mano.

Son **dos cosas a la vez**:

- Un **bot de Telegram** que te avisa al instante de cada oportunidad nueva.
- Una **web privada** estilo SlickDeals con el feed completo de ofertas vivas,
  que se refresca sola y de la que las ofertas muertas **desaparecen solas**.

El bot es para no perderte lo urgente; la web es para cuando abres el móvil por
la tarde y quieres ver todo lo que sigue en pie sin rebuscar en el chat.

**Modo actual: `experimental`.** No hay filtros de tipo de producto, ni precio
mínimo ni máximo: entra todo lo que tenga un descuento claro y tú decides qué
comprar. El criterio único es **descuento ≥ 50 % respecto al precio real de
mercado** — "algo de 200 € que de repente está a 70 €".

Cambiando `modo: selectivo` en `config.yaml` se activan los filtros de tamaño,
categoría, marca y margen mínimo (perfil de 500–1.000 € de capital, reventa en
Wallapop, "una aspiradora sí, una elíptica no").

---

## Lo que lo diferencia de un bot de chollos normal

**1. El descuento se mide contra el precio REAL, no contra el precio tachado.**
Media internet infla el "precio original" para que todo parezca un −50 %
permanente. Aquí el precio tachado de la tienda no vale como referencia: hace
falta que lo respalde otra fuente. Por orden de fiabilidad:

| Fuente | Qué es | Cuándo sirve |
|---|---|---|
| `cross_tienda` | Mediana del mismo producto en ≥3 tiendas | Lo más fiable |
| `historico_propio` | Lo que costaba en esa misma tienda | Con 1 sola tienda |
| `variantes_hermanas` | Las otras tallas/colores del mismo producto | **El primer día, sin histórico de nada** |

La tercera es la que hace que el bot sirva desde el minuto uno: la talla M a
9 € cuando S, L y XL están a 89 € es un error detectable sin comparar con nadie.

**2. Ordena por margen en euros, no por porcentaje de descuento.**
Un −80 % sobre 30 € deja 24 €. Un −35 % sobre 900 € deja 315 €. El segundo ni
aparece en un bot que ordena por porcentaje.

**3. El precio de referencia de venta es el de segunda mano, no el del retail.**
Lo que te importa no es cuánto has ahorrado sobre el PVP, sino cuánto te van a
pagar por ello en Wallapop. Un −60 % puede tener margen cero si el ratio de
reventa de esa categoría es del 35 %.

**4. Descuenta el riesgo de cancelación y los DOS envíos.**
Los portes que te cobra la tienda al comprar (con su umbral de envío gratis) y
los que pagas al revender. 4,95 € de portes sobre una compra de 20 € es un 25 %
que se come el chollo.

**5. Descuenta el riesgo de cancelación.**
Un error de precio de los espectaculares se cancela el 65 % de las veces. Una
liquidación real casi nunca. Sin ajustar por eso no puedes comparar las dos.

**6. Calcula el precio efectivo de las promos multi-unidad.**
Un 3x2 sobre algo con un 20 % de margen lo convierte en un 46 % efectivo. Casi
ningún bot lo calcula porque mira el precio de etiqueta.

**7. Detecta "tienda reventada".**
Cuando un error de importación tumba 50 productos a la vez, no te manda 50
alertas sueltas: te manda una con el lote entero ordenado por margen. Ahí es
donde se hace el dinero de golpe.

**8. El KPI final es margen/día, no margen.**
Con capital limitado, 40 € en 3 días baten a 90 € en 45.

---

## Instalación

### En un VPS (recomendado, ~4–6 €/mes)

```bash
git clone <tu-repo> heroscalper && cd heroscalper
cp .env.example .env        # token de Telegram + contraseña de la web
nano tiendas.txt            # una tienda por línea
docker compose up -d        # levanta el bot y la web
docker compose logs -f
```

La web queda en `http://TU_IP:8080`. **Pon siempre `WEB_PASSWORD`** si la
expones fuera de tu red: sin contraseña queda abierta a cualquiera.

### Ver la web antes de tener datos reales

```bash
python run.py demo-web      # mete 6 ofertas de ejemplo
python run.py web           # http://localhost:8080
```

### En local, sin Docker

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python run.py demo          # prueba en seco, sin tocar internet
```

### El bot de Telegram

1. Escribe a `@BotFather`, comando `/newbot`, y copia el token.
2. Escribe algo a tu bot recién creado.
3. Abre `https://api.telegram.org/bot<TOKEN>/getUpdates` y copia el `chat.id`.

Sin token configurado el bot imprime las alertas por consola: útil para la
fase 0.

---

## Uso

```bash
python run.py descubrir tiendas.txt   # clasifica dominios por plataforma
python run.py barrer midominio.es     # un barrido completo de una tienda
python run.py outlet midominio.es     # solo las páginas de liquidación
python run.py ciclo tiendas.txt       # un ciclo completo, con alertas
python run.py bucle tiendas.txt       # en bucle (lo que corre en el VPS)
python run.py reventa data/reventa.csv  # carga precios de 2ª mano
python run.py demo                    # prueba en seco
python run.py web                     # levanta la web privada
python run.py demo-web                # llena la web con ofertas de ejemplo
```

### Orden recomendado la primera vez

1. `descubrir` sobre una lista amplia de dominios → te dice cuáles son vigilables.
2. `reventa` con tus precios de referencia de Wallapop (ver `data/reventa.ejemplo.csv`).
3. `ciclo` un par de veces para llenar el histórico. **Las primeras horas no
   alertará casi nada: sin histórico no hay con qué comparar.** Es normal.
4. `bucle` cuando ya haya datos.

---

## Arquitectura

```
                 ┌──────────────────────────────────┐
  tiendas.txt →  │ fingerprint  (¿qué plataforma?)  │
                 └──────────────┬───────────────────┘
                                ↓
        ┌────────────┬──────────────────┬──────────────┐
        │  Shopify   │   WooCommerce    │   JSON-LD    │   ← 3 adapters
        │products.json│  Store API      │  schema.org  │     cubren el 98%
        └────────────┴──────────────────┴──────────────┘     del ecommerce ES
                                ↓
                     SQLite (histórico de precios)
                                ↓
                 ┌──────────────────────────────────┐
                 │ detector                         │
                 │  · cruce entre tiendas por EAN   │
                 │  · filtros de tamaño y categoría │
                 │  · margen neto tras porte        │
                 │  · ajuste por riesgo             │
                 │  · agrupación de eventos         │
                 └──────────────┬───────────────────┘
                                ↓
                            Telegram
```

**Por qué 3 adapters bastan:** en España hay ~54.650 tiendas online activas, de
las cuales 41.006 son WooCommerce, 9.017 PrestaShop y 3.879 Shopify. Un adapter
por *plataforma* (no por tienda) cubre el 98 % sin escribir código por tienda.
PrestaShop y el long tail entran por el adapter genérico de JSON-LD.

### Un aviso sobre el EAN

El EAN es la clave que permite decir "esto está a 20 € y en otras 11 tiendas
está a 130 €". Verificado contra tiendas Shopify españolas reales: **el
`products.json` público no devuelve `barcode`**. Por eso existe
`matching.py`, que genera una clave marca+modelo a partir del título
(`Sony WH-1000XM5` y `Auriculares Sony WH1000XM5 negros` → `mod:sony:wh1000xm5`)
y le asigna una fiabilidad. Los cruces por texto suelto no disparan alertas de
error de precio: comparar productos distintos sería peor que no alertar.

---

## Ajustes que querrás tocar

Todo está en `config.yaml`:

| Parámetro | Qué hace |
|---|---|
| `modo` | `experimental` (sin filtros) o `selectivo` (con ellos) |
| `descuento_min_vs_mercado` | **El dial principal.** 0.50 = solo cosas a mitad de precio o menos. Súbelo a 0.60–0.65 si llega demasiado ruido |
| `ahorro_min_eur` | Empieza en 0. Ponlo en 20–30 € cuando te canses de alertas de 4 € |
| `envio_compra` | Portes por defecto y umbral de envío gratis; se puede afinar por tienda |
| `max_unidades_por_pedido` | Tope de 3. **No lo subas**: pedir cantidades anormales de un error de precio dispara la revisión manual y te cancelan |
| `variantes.ratio_vs_hermanas` | Cuánto más barata que sus hermanas tiene que ser una variante |
| `prob_cancelacion` | Recalíbralo con tus datos reales a partir del mes 2 |

---

## Fase 0: valida antes de pagar el VPS

Déjalo corriendo 48–72 h en local contra 5–10 tiendas. Si salen 2 oportunidades
reales con margen, monta el VPS. Si sale cero, cambia de nicho — te has ahorrado
el dinero.

Apunta cada compra en la tabla `operaciones` (precio real, si te la cancelaron,
a cuánto y en cuántos días la vendiste). **A partir del mes 3 eso sustituye a las
estimaciones** y el score deja de ser una suposición para pasar a ser tu dato.

---

## Buen comportamiento

El bot respeta `robots.txt`, va a 1 petición/segundo por dominio con jitter y
hace backoff exponencial ante un 429. No hace login, no resuelve captchas y no
compra solo: **avisa, tú decides y tú compras**. Mantenlo así — es lo que
separa un monitor de precios de algo que te bloquea la IP y la cuenta.

## Tests

```bash
python -m pytest tests/ -q     # 49 tests
```


---

## Técnicas incorporadas de los bots que ya existen

Repasando cómo trabajan los cazadores de errores de precio anglosajones
(Slickdeals, priceerrors.io, los grupos de Telegram/Discord) y las herramientas
de histórico (Keepa, CamelCamelCamel, Idealo), lo que se ha adoptado aquí:

- **El histórico como validador.** Es exactamente lo que hace Keepa: si el
  precio lleva seis meses en 150 € y hoy está a 15 €, es una anomalía obvia.
  Implementado como fuente de referencia `historico_propio`.
- **Comparar variantes del mismo producto.** Colores o tallas con precios
  incoherentes son uno de los tipos de error más citados. Es además la única
  señal que no necesita datos previos.
- **Umbrales del 20–30 % como disparo, 50 % para "esto es un error".** Aquí el
  disparo está en el 50 % porque buscas margen de reventa, no ahorro personal.
- **Los errores moderados se sirven más que los extremos.** Un −90 % casi
  siempre pasa por revisión manual y se cancela; un −40 % suele colar. Está
  reflejado en la tabla `prob_cancelacion`, y por eso el bot ordena por margen
  *esperado* y no por descuento.
- **No pedir cantidades anormales.** Comprar 20 unidades de un error dispara
  las alertas antifraude de la tienda. De ahí el tope de 3.
- **Ventana temporal.** Los errores se concentran de domingo noche a martes por
  la mañana (cargas de catálogo y cambios de campaña). No filtra, solo prioriza.

Lo que **no** se ha copiado, deliberadamente: el checkout automático. Es lo que
convierte un monitor de precios en algo que te bloquea la IP y la cuenta, y en
la práctica es también lo que más cancelaciones provoca.

### Fuentes que puedes añadir más adelante

- **Chollometro** tiene RSS público: no para copiar chollos (cuando salen ahí
  ya llegas tarde) sino para minar qué tiendas producen errores históricamente.
- **Keepa** cubre los Amazon europeos con histórico real y tiene endpoint de
  *deals*; es de pago, pero resuelve Amazon, que es justo donde el scraping
  propio es más difícil.
- **Idealo.es** para ver qué tiendas venden un EAN concreto: es la vía más
  rápida de ampliar el directorio de tiendas con las que ya son competitivas
  en precio.


---

## La web privada

Un feed de tarjetas con imagen, precio, precio real tachado y el porcentaje de
descuento en grande, más las tres cifras que importan: reventa estimada,
beneficio neto y beneficio ajustado por riesgo.

- **Se refresca sola cada 60 segundos**, sin recargar la página.
- **Las ofertas muertas desaparecen.** Si una oferta lleva 3 horas sin volver a
  detectarse (configurable en `web.expirar_tras_minutos`), se marca como
  expirada y sale del feed. No acumulas basura.
- **La estrella (★) fija una oferta**: las guardadas no expiran solas, se quedan
  hasta que las sueltas. Útil para las que quieres pensarte.
- **Filtros por tipo**, buscador, y cinco criterios de orden: mejor rotación,
  mayor beneficio, mayor descuento, precio más bajo y más recientes.
- Se adapta al tema claro u oscuro del sistema y funciona bien en el móvil.

La API que la alimenta es igual de simple, por si algún día quieres montar otra
cosa encima:

| Endpoint | Qué devuelve |
|---|---|
| `GET /api/ofertas?tipo=&orden=&limite=` | Las ofertas vivas y el resumen |
| `POST /api/ofertas/{clave}/guardar` | Fija o suelta una oferta |
| `GET /api/salud` | Contadores, para monitorización |

## Amazon

Amazon no se rastrea directamente: es la tienda con más protecciones y la que
antes bloquea. La vía practicable es **Keepa**, que lleva años guardando el
histórico de precios de todos los Amazon europeos y expone un endpoint de
*deals* ya calculado.

Tiene una ventaja añadida y grande: Keepa te da el **precio medio histórico
real** del producto, que es justo la referencia honesta que Amazon nunca enseña
y la que hace inútil el truco del PVP inflado.

Se activa poniendo `fuentes.keepa.activo: true` y una `KEEPA_API_KEY`. Sin
clave el adapter se desactiva solo y todo lo demás sigue funcionando igual.


---

## Precio de reventa: medido, no estimado

La primera versión estimaba el beneficio como un porcentaje del precio de
mercado. Eso no vale para decidir una compra, así que ahora se **mide** en las
plataformas donde de verdad se vende en España:

| Fuente | Qué da | Fiabilidad |
|---|---|---|
| **CeX** (`es.webuy.com`) | El precio al que **te compran hoy**, en efectivo o en vale | **Garantizado.** No es lo que alguien pide: es lo que te pagan. Tienen tienda física, Málaga incluida |
| **Wallapop** | Mediana de anuncios activos, recortando extremos y aplicando un factor de cierre | Alta para electrónica, hogar y deporte |
| **Vinted** | Mediana del catálogo | Alta para moda y calzado |

CeX manda siempre que exista, porque es el único número garantizado: convierte
la operación en una apuesta con pérdida máxima acotada. Si no hay CeX, gana la
fuente con más anuncios mirados.

Cuando el dato está medido, la tarjeta enseña *Reventa 2ª mano* y *Beneficio
neto*. Cuando no, enseña solo *Te ahorras* —que sí es un hecho— y avisa de que
la reventa está sin comprobar, con botones directos a Wallapop y Vinted para
que lo mires tú en dos clics.

**Todo lo consultado se guarda** en `reventa_cotizaciones`. Con el tiempo tienes
tu propia serie histórica de a cuánto se paga cada cosa en segunda mano, que no
te la da ninguna de esas plataformas.

## Niveles: un color, no un número

Una rebaja decente y un pelotazo no se distinguen leyendo cifras, se distinguen
de un vistazo. Cada oferta recibe un nivel del 1 al 4 y un color:

| | Nivel | Cuándo | En la web | En Telegram |
|---|---|---|---|---|
| 🔴 | **BRUTAL** | Error de precio extremo, gratis, −80 % o +400 € de ahorro | Sección propia arriba, borde rojo | `🔴🔴🔴 BRUTAL` |
| 🟠 | **MUY BUENA** | −70 % o +200 € de ahorro | Borde naranja | `🟠🟠 MUY BUENA` |
| 🟡 | **BUENA** | −60 % o +100 € de ahorro | Borde amarillo | `🟡 BUENA` |
| 🟢 | **NORMAL** | Pasa el filtro del 50 % | Sin resaltar | `🟢 NORMAL` |

Se miran **las dos cosas**, descuento y ahorro en euros, porque un −85 % sobre
40 € y un −55 % sobre 700 € no se parecen en nada y los dos merecen que te
fijes. Se asigna el nivel más alto que cumpla cualquiera de sus condiciones.

La web agrupa las ofertas en una sección por nivel, de más a menos, así que lo
que merece la pena está siempre arriba. Los umbrales están en `niveles` del
`config.yaml`.

**Si Telegram te satura**, sube `nivel_minimo_telegram` a 2 o 3: solo te
llegarán mensajes de lo bueno, y la web te lo seguirá enseñando todo.

## Cuando algo se rompe, se ve

Si el lector de una tienda falla tres veces seguidas, ya no solo te llega un
mensaje a Telegram: **aparece un aviso rojo en la cabecera de la web** con
cuántas tiendas no responden y cuáles. Que un scraper muera en silencio es el
fallo número uno de estos proyectos, y ahora es imposible no enterarse.


---

## ¿Portátil o servidor?

Las dos cosas funcionan, pero no dan lo mismo.

### En tu portátil

Funciona, y para probar es lo suyo. Tres cosas que hay que dejar hechas o se
para solo:

1. **Que no se suspenda.** Es la causa número uno de que "deje de funcionar":
   al suspenderse, el proceso se congela y la red se corta.
   - macOS: `caffeinate -i docker compose up -d`, o Ajustes → Batería →
     Opciones → *Evitar que el Mac se duerma automáticamente*.
   - Windows: Configuración → Sistema → Inicio/apagado → Suspensión: *Nunca*,
     tanto con batería como enchufado.
   - Linux: `systemd-inhibit --what=sleep docker compose up -d`.
2. **Enchufado siempre.** Con batería, el sistema baja el rendimiento y acaba
   durmiéndose igual.
3. **Que arranque solo tras un reinicio.** El `docker-compose.yml` ya lleva
   `restart: unless-stopped`, así que con Docker configurado para arrancar con
   el sistema se recupera solo de reinicios y cortes de luz.

Lo que no arregla nada de eso: cerrar la tapa (en la mayoría de portátiles
suspende igual), las actualizaciones que reinician sin avisar, y que el Wi-Fi
de casa se caiga a las 4 de la mañana.

### En un VPS

Además de no tener ninguno de esos problemas, hay una diferencia que importa
más de lo que parece: **la web la ves desde el móvil, estés donde estés**. Con
el portátil en casa, el feed solo existe mientras estás delante de él, y el
sentido de tener la web es justo mirarla cuando no estás.

| | Coste | Para qué |
|---|---|---|
| Tu portátil | 0 € (~1-2 €/mes de luz) | La fase 0: los primeros días, ver qué encuentra |
| **Hetzner CX22** | **~4-5 €/mes** | Dejarlo fijo. Lo más simple que funciona |
| Oracle Cloud Always Free | 0 € | Gratis de verdad, pero el registro es tiquismiquis |

**Recomendación:** portátil esta semana para validar, y si lo que sale
convence, un VPS de 4 € y te olvidas. El mismo `docker compose up -d` funciona
igual en los tres sitios.


---

## El histórico de precio en cada oferta

Un −60 % no dice nada por sí solo: puede ser un chollo o el precio que lleva
teniendo ese producto desde hace tres meses. Por eso cada tarjeta muestra:

- **Media histórica** de lo que ha costado mientras el bot lo ha vigilado.
- **Más barato visto**: el mínimo que se ha registrado nunca.
- Una **mini-gráfica** con la evolución del precio.
- Y si el precio de ahora iguala o baja del mínimo, un distintivo verde:
  **NUNCA HA ESTADO MÁS BARATO**.

En Telegram va en una sola línea, para no ensuciar el mensaje:
`📉 Media histórica 285,68 € · más barato visto 69,00 € ← nunca ha estado más barato`

Cada tarjeta lleva además tres botones que abren en pestaña nueva la búsqueda
de ese producto en **Wallapop**, **Vinted** y **CeX**, para comprobar la
reventa en dos clics sin salir del feed.

## Por qué el disco no se llena

Guardar cada lectura de cada ciclo parece inofensivo y no lo es. Con 45 tiendas
y un barrido cada 30 minutos son **4,3 millones de filas al día**: unos 690 MB
diarios, que llenan el disco de 40 GB de un servidor pequeño **en 58 días**.

Dos medidas lo resuelven sin perder información útil:

1. **Solo se guarda lo que cambia.** El 97 % de los precios no se mueve de un
   día para otro, así que una lectura se guarda si el precio cambió o si han
   pasado ~20 h desde la anterior. Pasa de 4,3 M de filas al día a unas 220.000.
2. **Poda diaria.** Más allá de 120 días se queda una lectura por producto y
   día (lo que necesita la gráfica); más allá de 2 años se borra.

Resultado: unos **35 MB al día**, o **13 GB al año**. Con el CX22 tienes para
tres años largos.

Un efecto secundario que conviene saber: como un precio estable genera una sola
lectura diaria, `deteccion.historico.min_lecturas: 5` significa en la práctica
**5 días de histórico**, no cinco lecturas seguidas.


---

## Ofertas sin verificar

A veces no hay forma de contrastar un precio: el producto solo lo vende esa
tienda, o el bot lleva pocos días y todavía no tiene histórico. Antes esas
ofertas se descartaban. Ahora **se enseñan marcadas como SIN VERIFICAR**, con
el descuento que declara la tienda y un aviso claro de que nadie lo confirma.

Es mejor verlas que no verlas: entras, lo compruebas en un clic con los
botones de Wallapop, Vinted y CeX de la propia tarjeta, y decides. Lo que no
hacen es engañarte:

- Llevan un distintivo naranja **SIN VERIFICAR** y borde discontinuo.
- **Nunca suben de nivel 2**, aunque el descuento declarado sea del 90 %. Un
  PVP inflado no puede presumir de ofertón.
- En Telegram el mensaje dice explícitamente que puede ser un PVP inflado.

Si prefieres no verlas, pon `mostrar_sin_verificar: false` en `config.yaml`.


---

## Seguridad

La web vive en una IP pública y hay bots recorriendo internet probando puertos
y contraseñas. Esto es lo que hay puesto y por qué.

| Riesgo | Qué hay |
|---|---|
| Que alguien pruebe contraseñas toda la noche | **5 intentos por IP y se cierra 15 minutos** |
| Que se cuele por la API en vez de por el login | **Todos** los endpoints piden sesión, incluidos los contadores |
| Que roben la cookie con JavaScript | Cookie `HttpOnly` + `SameSite=Lax`, y `Secure` con HTTPS |
| Que la sesión siga viva tras cerrarla | Sesiones en base de datos con caducidad: cerrar sesión la revoca de verdad |
| Que quien lea la base de datos suplante tu sesión | Se guarda el **hash** del token, nunca el token |
| Que metan la web en un iframe para engañarte | `X-Frame-Options: DENY` + `frame-ancestors 'none'` |
| Que la página cargue algo de fuera | CSP estricta: solo imágenes de producto |
| Que un error te enseñe las tripas del sistema | Nunca se devuelve la traza |
| Que aparezca en Google | `noindex, nofollow` |

**Datos personales: cero.** La base de datos son precios, tiendas y ofertas.
No hay nombres, correos, teléfonos ni pagos — hay un test que lo comprueba en
cada ejecución.

**Una fuga que había y ya no:** `httpx` incluye la URL completa en sus
excepciones, y la URL de la API de Telegram lleva el token dentro. Un simple
fallo de red te escribía el token en los logs del servidor. Ahora los mensajes
de error pasan por un filtro que tapa el token de Telegram y la clave de Keepa.

### Compartir con un colega

En vez de darle tu contraseña, usa `WEB_USERS` en el `.env`:

```
WEB_USERS=pavel:mi-clave-larga,luis:otra-clave-larga
```

Así le quitas el acceso a uno sin cambiar la del resto.

### Lo único que falta: HTTPS

Sin cifrar, la contraseña viaja en claro. Es el agujero más gordo y se cierra
en dos comandos:

1. Crea un dominio gratis en `duckdns.org` apuntando a la IP de tu servidor.
2. En el servidor:

```bash
export DOMINIO=tuflow.duckdns.org EMAIL=tucorreo@ejemplo.com
echo "WEB_HTTPS=true" >> .env
docker compose -f docker-compose.yml -f docker-compose.https.yml up -d
```

Caddy pide el certificado a Let's Encrypt solo, lo renueva solo y redirige
`http` a `https`. No hay que tocarlo nunca más. Y de paso la web deja de estar
expuesta directamente: solo Caddy da la cara.

**Un detalle que juega a tu favor:** al estar en un VPS, lo que se ve desde
fuera es la IP del servidor, no la de tu casa. Corriendo el bot en tu portátil
sería al revés.

## Triangulación: verificar sin depender de las tiendas

Antes, si ninguna otra tienda vendía el producto, no había forma de saber si el
precio era bueno. Ahora la referencia se busca en cuatro sitios, en cascada:

1. **Otras tiendas** — la mediana del mismo producto en ≥3 tiendas
2. **Su propio histórico** — lo que costaba ahí mismo
3. **Lo que se paga en 2ª mano** — CeX, Wallapop, Vinted
4. **Otras variantes** del mismo producto

La tercera es la novedad y cambia bastante: **aunque nadie más lo venda, si en
CeX te lo compran por 40 € y aquí cuesta 10 €, el margen es real y está
comprobado.** No hace falta un precio de tienda para verlo.

Un matiz que importa: cuando la referencia es un precio de segunda mano, el bot
**no lo llama "error de precio"**. La segunda mano ya está por debajo del
retail, así que estar por debajo de ella es buen margen, no una anomalía — y su
riesgo de cancelación es completamente distinto.


---

## Desplegar en Render (en vez de un VPS)

Funciona, y si ya pagas Render te ahorras gestionar un servidor. Hay **un
detalle que condiciona el montaje**: en Render un disco persistente solo se
puede montar en un servicio, y el bot y la web comparten la misma base de datos
SQLite. Dos servicios no pueden tocar el mismo disco.

La solución está en el código: `python run.py todo` arranca **el bot y la web
en un solo proceso**. Un servicio, un disco, todo funcionando.

### Pasos

1. Sube el proyecto a un repositorio **privado** de GitHub.
2. En Render: **New → Blueprint** y conecta el repo.
3. Render lee `render.yaml` y te pide las claves de una en una:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WEB_PASSWORD` (o `WEB_USERS`),
   `WEB_URL` y `KEEPA_API_KEY` (esta puede ir vacía).
4. Deploy.

Ventajas frente al VPS: **HTTPS ya viene puesto** (y con él el aviso de
contraseña en claro desaparece), no hay que actualizar el sistema nunca, y los
despliegues son un `git push`.

Coste: el plan Starter más el disco. Sale más caro que los 5 € del VPS, pero si
ya tienes Render y no quieres administrar máquinas, compensa.

### Comandos según dónde lo montes

| Dónde | Comando |
|---|---|
| Render / PaaS con un solo servicio | `python run.py todo tiendas.txt` |
| VPS con Docker | `docker compose up -d` (dos contenedores) |
| Tu portátil, para probar | `python run.py todo tiendas.txt` |

## Cómo ampliar la cobertura

**No hace falta registrarse en nada.** El bot lee las tiendas directamente de
sus propias webs: lo mismo que ellas publican para Google Shopping. No hay
acuerdos, ni altas, ni permisos.

Por orden de rentabilidad del esfuerzo:

**1. Sube `max_productos_por_tienda`.** Es lo más barato con diferencia. Los
feeds vienen de 250 en 250, así que leer 4.000 productos de una tienda son 16
peticiones. Pasar de 2.000 a 4.000 duplica los artículos vigilados sin añadir
ni una tienda. Antes de buscar tiendas nuevas, exprime las que ya tienes.

**2. Añade tiendas que conozcas.** Cualquier sitio español donde hayas comprado
alguna vez. Escribes el dominio en `tiendas.txt`, ejecutas `descubrir` y en diez
segundos sabes si sirve.

**3. Vuelca listas de golpe.** Si te encuentras un listado en un blog, un
ranking o un CSV:

```bash
python run.py importar-tiendas listado.txt
```

Extrae los dominios, quita duplicados y basura, y los añade solo.

**4. idealo.es.** Busca un producto de tu categoría y mira qué tiendas lo
venden: salen tiendas que no conocías y que ya compiten en precio.


## ¿Cuántos artículos hay que vigilar?

Las cifras de referencia del sector: **idealo** compara 2,8 millones de
productos de más de 50.000 tiendas, con 200 millones de entradas de oferta.
**Chollometro** publica *"cientos de ofertas al día"*, pero puestas a mano por
una comunidad de más de un millón de personas.

Ninguno de los dos es el objetivo correcto, porque su negocio es otro: ellos
enseñan ofertas a millones de visitantes. El tuyo es comprar unas cuantas
cosas al mes.

### El embudo

| Artículos vigilados | Tiendas | Oportunidades accionables |
|---|---|---|
| 10.000 | ~10 | 0,1/día — 2 al mes |
| 50.000 | ~35 | 0,4/día — 13 al mes |
| 150.000 | ~100 | 1,3/día — 38 al mes |
| **400.000** | **~250** | **3,4/día — 100 al mes** |
| 1.000.000 | ~600 | 8,4/día |
| 3.000.000 | feeds de afiliación | 25/día |

De 400.000 artículos, unos 4.000 están a −50 % o más en cualquier momento;
unos 200 aparecen nuevos cada día; unos 70 se pueden verificar; y de esos, 3 o
4 dejan más de 30 € de margen con reventa real.

### El techo no es la detección, es tu capital

Con 1.000 € y un ticket medio de 150 €, tienes **6-7 operaciones simultáneas**
y unas **14 al mes** — medio artículo al día. Ese es el límite real.

Así que el objetivo **no es cazarlas todas**: es tener un flujo entre 5 y 10
veces mayor de lo que puedes comprar, para poder quedarte solo con las mejores.

**Para 1.000 € de capital, el punto óptimo son ~400.000 artículos (unas 250
tiendas):** 3-4 oportunidades diarias de las que compras la mejor. Ir a 3
millones te daría 25 alertas al día para comprar media: ruido puro.

El número sube cuando suba tu capital, no antes. Con 5.000 € tocaría el millón.

### El barrido tiene que ir en paralelo

Con 250 tiendas y ~6 peticiones por tienda, hacerlo de una en una tarda **47
minutos** y no cabe en el ciclo de 30. Por eso las tiendas se barren en
paralelo (`rastreo.tiendas_a_la_vez`, 16 por defecto): baja a **3 minutos**.

No es maltratar a nadie: el freno de 1 petición/segundo es **por dominio**, así
que atender a 16 tiendas a la vez no supone ni una petición extra para ninguna.


---

## El suelo de beneficio: 50 €

Una oferta solo llega si deja **50 € netos o más**, ya descontados los portes de
compra, los de venta y el embalaje. Por debajo de eso no compensa el rato de
comprar, esperar el paquete, fotografiarlo, publicarlo y responder mensajes.

Lo que cuesta subir el listón, con 468.000 artículos vigilados:

| Suelo | Oportunidades/día | Al mes |
|---|---|---|
| 30 € | 3,9 | 118 |
| **50 €** | **2,2** | **67** |
| 100 € | 1,0 | 31 |

Parece que pierdes la mitad, pero no: con 1.000 € de capital solo puedes hacer
unas 14 operaciones al mes. Pasar de 118 candidatas a 67 no te quita ninguna
compra — te quita las que ibas a descartar igual. Y sube el beneficio medio de
cada una.

Está en `filtros.margen_neto_min_eur`. Si algún mes ves poco movimiento, bájalo
a 35; si te sobra dónde elegir, súbelo a 80.

**Las ofertas gratis están exentas** de este filtro: ahí no arriesgas dinero,
solo tiempo, así que se enseñan todas.


---

## Mis compras: el bucle que hace que aprenda

Durante varias versiones esto fue una promesa incumplida: la tabla existía pero
nadie la escribía. Ya funciona.

En la web, pestaña **Mis compras**:

- Botón **"Ya la he comprado"** en cada oferta. Apuntas lo que has pagado.
- Cuando la vendas, **"Vendida"** con el precio. Si te la cancelan, **"Me la
  cancelaron"**.
- Arriba: beneficio real acumulado, capital inmovilizado y tu tasa de
  cancelación de verdad.

Y lo importante: cuando una tienda acumula **5 compras registradas**, su tasa
real de cancelación **sustituye a mi estimación**. Si una tienda te sirve
siempre los errores y otra te los cancela, el bot deja de tratarlas igual. Eso
no lo puede saber ningún modelo: solo tus datos.

También se muestra ahora el **retorno** (%) en cada tarjeta. Ganar 60 € sobre
40 invertidos no es lo mismo que ganar 60 € sobre 400.


---

## Avisos con sonido en el navegador

Para empezar a probar sin montar Telegram. En la cabecera de la web hay una
campana: la pulsas, suena una prueba y a partir de ahí te avisa de cada oferta
nueva mientras la pestaña esté abierta.

Cada oportunidad nueva dispara **tres cosas a la vez**:

1. **Un pitido**, generado por la propia web.
2. **Una tarjeta dentro de la web** (abajo a la derecha) con la etiqueta de
   nivel, la tienda, el producto, el precio y lo que deja. Se va sola a los
   15 s (25 s las muy buenas, 40 s las brutales), se queda quieta si pasas el
   ratón por encima, y al pulsarla abre la tienda en otra pestaña. Como mucho
   5 en pantalla. **No pide permiso a nadie ni necesita HTTPS: siempre sale.**
3. **La notificación del sistema de Chrome**, solo si le has dado permiso y
   estás en HTTPS. Es la única que se ve con la web en segundo plano.

Además, si tienes la pestaña en segundo plano, el **título de la pestaña lleva
un contador** — `(3) HeroScalper` — que se borra al volver a mirarla.

- **El sonido cambia según lo buena que sea**: una nota para las normales, dos
  para las muy buenas, tres ascendentes para las brutales. Aprendes a
  distinguirlas sin mirar la pantalla.
- **Avisan solo de oportunidades NUEVAS**, nunca de tus compras ni de tus
  ganancias. El título lo dice literalmente: *"Nueva oportunidad BRUTAL ·
  45,00 €"*, y debajo el producto, la tienda y cuánto deja.
- **Funcionan mires lo que mires.** La vigilancia va por su cuenta: aunque
  tengas la web abierta en "Mis compras" o en el histórico, sigue avisándote
  de lo que entra nuevo.
- Al pulsar la notificación se abre la tienda directamente.
- Como mucho **5 avisos seguidos** por ciclo, separados casi un segundo, para
  que no se solapen los pitidos.
- Recuerda lo que ya te avisó, así que al recargar la página no repite.
- El estado se guarda en el navegador: si lo dejas activado, sigue activado.

El sonido se genera con el propio navegador, sin cargar ningún archivo — así no
choca con la política de seguridad de la página.

**Dos límites que conviene saber:**

1. **Solo funciona con la pestaña abierta.** No es una notificación push del
   móvil: es la pestaña avisándote. Para avisos con el navegador cerrado,
   Telegram.
2. **Los navegadores solo permiten notificaciones en páginas seguras.** En
   `localhost` funciona siempre; sobre `http://TU_IP:8080` el navegador las
   bloquea. En Render, que da HTTPS, funcionan sin hacer nada. Si vas por IP y
   sin HTTPS, el **sonido sí suena** aunque no salga la notificación de sistema.

Cuando termines de probar y quieras avisos en el móvil con todo cerrado, pon el
token de Telegram y ya está: los dos canales conviven.
