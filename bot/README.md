# Bot de Moderación de Telegram

Bot profesional de moderación para grupos y supergrupos, construido con
`python-telegram-bot` v22+, Python 3.13+ y SQLite (async, vía `aiosqlite`).

## Características

- **Comandos "/" reducidos a lo esencial**: `/admin`, `/unadmin`, `/warn`,
  `/unwarn`, `/ban`, `/unban`, `/kick`, `/mute`, `/unmute` (y `/start`,
  `/menu`). Todo lo demás — bienvenida, despedida, reglamento, palabras
  prohibidas, mensajes recurrentes, auto-eliminar y advertencias — se
  configura **100% desde el menú de botones**, sin comandos que recordar.
- **Requisito de permiso**: para poder usar el bot como administrador (menú
  y comandos), Telegram debe haberte dado el permiso **"Cambiar info del
  grupo" (Change group info)** además de ser admin. El dueño del grupo
  siempre tiene control total. Esto evita que administradores "de cortesía"
  sin permisos reales puedan tocar la configuración del bot.
- Sistema AFK/BRB persistente y eficiente. Escribe `brb` directamente (sin
  `/`), opcionalmente con un motivo: `brb almorzando`.
- Menú de configuración con botones (`/start` o `/menu`, en privado o
  dentro del grupo) para administrar bienvenida, despedida, reglamento,
  AFK, mensajes recurrentes, palabras prohibidas, auto-eliminación y
  advertencias, todo con botones.
- **Advertencias (warnings)**: `/warn` (respondiendo, con `@usuario` o ID)
  suma una advertencia; al llegar al límite configurado se aplica
  automáticamente el castigo elegido (silenciar / expulsar / banear) y se
  reinicia el contador. `/unwarn` quita una advertencia. El límite, el
  castigo y la duración del silencio se configuran desde el menú
  (❗ Advertencias).
- **Mensajes recurrentes con editor de un solo menú**: define uno o varios
  mensajes (texto, foto, video, GIF, documento, audio o nota de voz) con
  botones en línea opcionales, que se reenvían automáticamente cada cierto
  intervalo (de 10 minutos a 24 horas). Un único panel con un botón para la
  foto/media, otro para el texto, otro para los botones, el intervalo, fijar
  y borrar el anterior — y un botón de **👁 Vista previa** para ver
  exactamente cómo va a quedar antes de guardarlo. Nada de wizards que van
  mandando mensaje tras mensaje.
- **Palabras prohibidas**: filtro configurable con castigo (nada / mute con
  duración / ban) y opción de borrar o no el mensaje. Se agregan/quitan
  tocando los botones ➕/➖ del menú (🚫 Palabras prohibidas): el bot pide la
  palabra, la recibes, y vuelve a mostrarte el mismo menú actualizado.
- **Auto-eliminar**: borra automáticamente el aviso nativo de Telegram al
  entrar/salir un usuario, el aviso de inicio de videollamada, y los
  mensajes que invocan comandos con `/`, todo configurable desde el menú.
- Bienvenida, despedida y reglamento configurables por grupo, con
  placeholders y limpieza automática de avisos anteriores — todo desde el
  menú (👋 Bienvenida / 🚪 Despedida / 📜 Reglas).
- Todos los comandos aceptan `@usuario`, ID numérico o respuesta a un mensaje.
- El propietario del bot nunca puede ser moderado, y solo él puede
  administrar o moderar a otros administradores.
- Logging completo a archivo y consola; registro de cada acción de
  moderación en base de datos (quién, a quién, cuándo, dónde y por qué).
- Mensajes en MarkdownV2 con formato profesional y emojis.
- ¿Quieres hospedarlo gratis para no tenerlo corriendo en tu computadora?
  Mira **[DEPLOYMENT.md](DEPLOYMENT.md)**.

## Instalación

1. Crea un entorno virtual e instala dependencias:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copia y edita el archivo `.env`:

   ```bash
   BOT_TOKEN=tu_token_de_botfather
   OWNER_IDS=123456789,987654321
   DATABASE_PATH=database/bot.db
   LOG_LEVEL=INFO
   DEL_NOTICE_SECONDS=10
   ```

   - `OWNER_IDS` acepta uno o varios IDs de Telegram separados por coma.
     Estos usuarios tienen control total y nunca pueden ser moderados.

3. Añade el bot a tu grupo/supergrupo y otórgale permisos de administrador
   con, al menos: eliminar mensajes, restringir miembros, promover
   miembros e invitar usuarios.

   **Importante:** además, cada persona que vaya a administrar el bot
   (aparte del dueño del grupo) necesita tener ella misma el permiso de
   administrador **"Cambiar info del grupo" (Change group info)**. El bot
   revisa este permiso específico (no solo "ser admin") antes de dejar
   usar el menú o los comandos de moderación.

4. Ejecuta el bot:

   ```bash
   python main.py
   ```

## Bienvenida / Despedida / Reglamento

Comandos (solo administradores, excepto `/rules` que cualquiera puede usar):

| Comando | Descripción |
|---|---|
| `/setwelcome <texto>` | Define el mensaje de bienvenida. También puedes responder a un mensaje con `/setwelcome` para usar su texto. |
| `/welcome on\|off` | Activa o desactiva la bienvenida. Sin argumentos, muestra el estado actual. |
| `/resetwelcome` | Vuelve al mensaje de bienvenida por defecto. |
| `/setgoodbye <texto>` | Define el mensaje de despedida. |
| `/goodbye on\|off` | Activa o desactiva la despedida. |
| `/resetgoodbye` | Vuelve al mensaje de despedida por defecto. |
| `/welcomeclean on\|off` | Si está activado (por defecto), borra el aviso de bienvenida anterior antes de publicar uno nuevo — pensado para grupos grandes donde entra mucha gente seguida. |
| `/setrules <texto>` | Define el reglamento del grupo. |
| `/rules` | Muestra el reglamento (disponible para cualquier miembro). |
| `/resetrules` | Borra el reglamento configurado. |

**Placeholders disponibles** en los textos de bienvenida y despedida:

- `{name}` — nombre del usuario
- `{mention}` — mención clickeable del usuario
- `{username}` — @usuario (o "sin usuario" si no tiene)
- `{id}` — ID numérico del usuario
- `{group}` — nombre del grupo

Ejemplo:

```
/setwelcome ¡Hola {mention}! Bienvenido/a a {group} 🎉. Lee las reglas con /rules
```

El texto que escribas se trata siempre como texto plano (se escapa
automáticamente), por lo que es seguro aunque contenga símbolos como
`<`, `>` o `&` — no hace falta preocuparse por romper el formato del mensaje.

## Mensajes recurrentes

Todo se hace desde el menú: `/menu` → 🔁 Mensajes recurrentes → ➕ Agregar
(o ✏️ Editar contenido sobre uno ya existente). Se abre **un único panel**
con un botón por cada parte, y cada vez que llenas un campo vuelves
automáticamente al mismo panel (no es una fila de preguntas una tras otra):

| Botón | Qué hace |
|---|---|
| 📷 Multimedia | Pide una foto/video/GIF/documento/audio/nota de voz (con descripción opcional). |
| 📝 Texto | Pide el texto del mensaje (o la descripción, si ya elegiste multimedia). |
| 🔘 Botones | Pide los botones en línea, o `no` para quitarlos. |
| ⏱ Intervalo | Elige la frecuencia con botones: 10, 20, 30, 45 min, 1, 2, 3, 4, 6, 8, 12, 18 o 24 h. |
| 📌 Fijar | Alterna sí/no. |
| 🗑 Borrar anterior | Alterna sí/no (borra la copia anterior antes de publicar la nueva). |
| 👁 Vista previa | Envía el mensaje tal cual quedaría, **sin guardarlo**, para que revises el resultado. |
| 💾 Guardar | Crea o actualiza el mensaje recurrente y lo activa. |
| ❌ Descartar | Cancela sin guardar los cambios. |

Sintaxis de los botones:

```
Texto del botón - https://enlace.com
Botón A - https://a.com | Botón B - https://b.com
```

una fila por línea, botones de la misma fila separados por ` | `.

Desde la lista puedes pausar/activar cada mensaje recurrente, cambiar el
fijado o el borrado del anterior, editarlo de nuevo o eliminarlo. Puedes
crear tantos como quieras; cada uno corre de forma independiente.

### Emojis premium — cómo funcionan

Telegram permite que un bot envíe entidades `custom_emoji` (los emojis
animados exclusivos de Premium) siempre que **la cuenta que creó el bot en
BotFather tenga Telegram Premium**. El bot no vuelve a "interpretar" el
texto: guarda las mismas entidades (`entities`) que llegaron en tu mensaje
original y las reenvía tal cual, así que si tu cuenta owner tiene Premium,
cualquier emoji premium que uses al definir el contenido de un mensaje
recurrente se conservará automáticamente. No requiere ninguna
configuración adicional.

## Palabras prohibidas

Desde el menú (`/menu` → 🚫 Palabras prohibidas):

- **➕ Agregar palabra(s)**: el bot te pide la(s) palabra(s) (una por
  línea si son varias); en cuanto las envías, se agregan y vuelves al
  mismo menú ya actualizado.
- **➖ Eliminar palabra(s)**: igual, pero elimina.
- Ambos botones tienen un **❌ Cancelar** por si te arrepientes.

El castigo (nada / silenciar / banear), la duración del silencio y si se
borra o no el mensaje se configuran desde ese mismo menú. El propietario y
los administradores del grupo nunca son afectados por el filtro. La
detección es por coincidencia de texto (no distingue mayúsculas/minúsculas)
dentro del mensaje o del pie de foto.

## Advertencias

- `/warn` (respondiendo a un mensaje, con `@usuario` o su ID, y
  opcionalmente un motivo) suma una advertencia al usuario.
- `/unwarn` le quita una advertencia.
- Al llegar al **límite configurado** (3 por defecto), el bot aplica
  automáticamente el castigo elegido — **silenciar, expulsar o banear** —
  y reinicia el contador de ese usuario a 0.
- Desde el menú (`/menu` → ❗ Advertencias) configuras: el límite de
  advertencias, el castigo automático, y (si el castigo es "silenciar")
  su duración.

## Auto-eliminar

Desde el menú (`/menu` → 🧹 Auto-eliminar) puedes activar, de forma
independiente:

- Borrar el aviso nativo de Telegram cuando alguien **entra** al grupo.
- Borrar el aviso nativo de Telegram cuando alguien **sale** del grupo.
- Borrar el aviso de **inicio de videollamada** de grupo.
- Borrar los **mensajes que invocan comandos** con `/` (el comando se
  ejecuta con normalidad; solo se borra el mensaje que lo escribió).

Esto es independiente del sistema de Bienvenida/Despedida: uno controla si
se publica un aviso personalizado, esto controla si se oculta el mensaje
nativo de Telegram. Se pueden combinar libremente.

## Bot anunciador (opcional): enviar anuncios a todos los grupos

Si quieres avisar algo puntual a **todos los grupos donde está el bot**
(no confundir con los "mensajes recurrentes", que son por grupo y se
repiten solos), puedes correr un segundo proceso: `broadcast_bot.py`.

- Es un **bot de Telegram aparte** (necesitas crearlo en @BotFather con
  otro nombre) que solo tú, el propietario, puedes usar por privado.
- **No necesita estar agregado a los grupos.** Compone el anuncio con
  botones (foto, texto, botones en línea, vista previa) y al confirmar
  lo dice a través de la misma base de datos; el bot de moderación —que
  sí está en todos los grupos— es quien realmente lo envía, revisando
  la cola cada 15 segundos.

### Cómo activarlo

1. Crea un segundo bot con [@BotFather](https://t.me/BotFather) (token
   distinto al del bot de moderación).
2. Agrega a tu `.env`:
   ```
   BROADCAST_BOT_TOKEN=el_token_del_nuevo_bot
   ```
3. Córrelo como un proceso aparte (con su propio servicio systemd, ver
   `DEPLOYMENT.md`):
   ```bash
   python broadcast_bot.py
   ```
4. Habla con ese bot por privado y usa `/anuncio` (o `/start`).

## Notas importantes

- **Resolución por `@usuario`**: Telegram no permite a los bots resolver
  un `@username` a un ID si el usuario nunca ha escrito en un chat visible
  para el bot. El bot mantiene una caché propia (tabla `users`) que se
  actualiza automáticamente con cada mensaje. Si un `@usuario` no es
  reconocido, usa su ID numérico o responde a uno de sus mensajes.
- **Mute temporal**: Telegram gestiona automáticamente el desbloqueo al
  llegar la fecha `until_date`; no se requiere una tarea programada propia.
- **AFK eficiente**: el estado AFK vive en memoria
  (`application.bot_data["afk_cache"]`) indexado por `user_id`, por lo que
  detectar menciones/respuestas es O(1) y no se recorre la base de datos
  en cada mensaje. Al iniciar, la caché se reconstruye desde SQLite, por lo
  que el estado sobrevive a reinicios.
- **Base de datos**: las tablas se crean automáticamente al iniciar el bot
  (`database.py`, `SCHEMA`). Puedes migrar a PostgreSQL reemplazando la
  clase `Database` por una implementación equivalente con `asyncpg`
  manteniendo la misma interfaz pública.

## Estructura del proyecto

```
bot/
├── main.py                 # Punto de entrada; solo registra los comandos "/" básicos
├── config.py                # Carga de configuración desde .env
├── database.py               # Capa de acceso a datos (SQLite async)
├── requirements.txt
├── .env
├── handlers/
│   ├── moderation.py         # /ban /kick /mute /unmute /unban /warn /unwarn
│   ├── admin.py               # /admin /unadmin (admins/staff quedaron sin comando, ver menú)
│   ├── utils_cmds.py           # /del /id /ping /info (sin comando registrado; código de reserva)
│   ├── afk.py                    # "brb" en texto plano y detección automática
│   ├── recurring.py               # Mensajes recurrentes: editor de un solo menú + jobs programados
│   ├── filters_words.py            # Palabras prohibidas + castigo (mute/ban) + auto-borrado
│   ├── warnings.py                  # Configuración de advertencias (límite/castigo/duración)
│   ├── cleanup.py                   # Auto-eliminar avisos de servicio y mensajes de comandos
│   ├── greetings.py                  # Bienvenida/despedida/reglas (100% vía menú)
│   └── menu.py                       # Menú de botones (/start, /menu) — panel principal
├── utils/
│   ├── permissions.py         # Reglas de permisos, jerarquía y el requisito can_change_info
│   ├── callbacks.py            # Decorador @safe_callback: evita botones "cargando" para siempre
│   ├── parsing.py                # Resolución de usuario objetivo
│   ├── time_parser.py             # Parser de duraciones (10m, 2h, 3d...)
│   ├── formatting.py               # Helpers MarkdownV2
│   ├── entities.py                  # Serialización de entities (emojis premium) y botones inline
│   └── logger.py                     # Configuración de logging
└── logs/
    └── bot.log                     # Generado en tiempo de ejecución
```

## Hospedar el bot gratis (sin dejarlo corriendo en tu computadora)

Ver **[DEPLOYMENT.md](DEPLOYMENT.md)** para una guía paso a paso.
