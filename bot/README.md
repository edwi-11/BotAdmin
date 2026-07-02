# Bot de Moderación de Telegram

Bot profesional de moderación para grupos y supergrupos, construido con
`python-telegram-bot` v22+, Python 3.13+ y SQLite (async, vía `aiosqlite`).

## Características

- Moderación completa: `/ban`, `/kick`, `/mute` (permanente o temporal),
  `/unmute`, `/unban`.
- Gestión de administración: `/admin`, `/unadmin`, `/admins`, `/staff`.
- Utilidades: `/del`, `/id`, `/ping`, `/info`.
- Sistema AFK/BRB persistente y eficiente. Escribe `brb` directamente (sin `/`), opcionalmente con un motivo: `brb almorzando`. `/brb` se mantiene como alias.
- Menú de configuración con botones (`/start` o `/menu`, en privado o dentro del grupo) para administrar bienvenida, despedida, reglamento, AFK, mensajes recurrentes, palabras prohibidas y auto-eliminación sin escribir comandos.
- **Mensajes recurrentes**: define uno o varios mensajes (texto, foto, video,
  GIF, documento, audio o nota de voz) con botones en línea opcionales, que
  se reenvían automáticamente cada cierto intervalo (de 10 minutos a 24
  horas). Cada uno se puede fijar (pin) al enviarse y/o borrar el anterior
  antes de publicar el nuevo. Los emojis premium se conservan tal cual
  (`/addrecurrente`, `/recurrentes`).
- **Palabras prohibidas**: filtro configurable con castigo (nada / mute con
  duración / ban) y opción de borrar o no el mensaje, agregando o quitando
  palabras una por una o varias a la vez (`/agregarpalabra`,
  `/eliminarpalabra`, `/palabras`).
- **Auto-eliminar**: borra automáticamente el aviso nativo de Telegram al
  entrar/salir un usuario, el aviso de inicio de videollamada, y los
  mensajes que invocan comandos con `/`, todo configurable desde el menú.
- Bienvenida, despedida y reglamento configurables por grupo, con
  placeholders y limpieza automática de avisos anteriores (`/setwelcome`,
  `/welcome`, `/resetwelcome`, `/setgoodbye`, `/goodbye`, `/resetgoodbye`,
  `/welcomeclean`, `/setrules`, `/rules`, `/resetrules`).
- Todos los comandos aceptan `@usuario`, ID numérico o respuesta a un mensaje.
- El propietario del bot nunca puede ser moderado, y solo él puede
  administrar o moderar a otros administradores.
- Logging completo a archivo y consola; registro de cada acción de
  moderación en base de datos (quién, a quién, cuándo, dónde y por qué).
- Mensajes en MarkdownV2 con formato profesional y emojis.

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

Se configuran con `/addrecurrente` (dentro del grupo) o desde el menú
(`/menu` → 🔁 Mensajes recurrentes → ➕ Agregar). El asistente pide, en orden:

1. **Contenido**: envía texto o media (foto/video/GIF/documento/audio/nota
   de voz) con su descripción. Puedes usar negritas, cursivas, enlaces y
   **emojis premium** — se reenvían exactamente igual.
2. **Botones** (opcional): con la sintaxis

   ```
   Texto del botón - https://enlace.com
   Botón A - https://a.com | Botón B - https://b.com
   ```

   una fila por línea, botones de la misma fila separados por ` | `. O toca
   "Sin botones".
3. **Intervalo**: elige entre 10, 20, 30, 45 minutos, 1, 2, 3, 4, 6, 8, 12,
   18 o 24 horas.
4. **Fijar mensaje**: si el bot debe fijarlo (pin) cada vez que lo envía.
5. **Borrar el anterior**: si el bot debe eliminar la copia anterior de ese
   mismo recurrente antes de publicar la nueva (para no acumular mensajes).

`/recurrentes` lista todos los mensajes recurrentes del grupo con botones
para pausar/activar, cambiar el fijado o el borrado del anterior, y
eliminarlos. Puedes crear tantos como quieras; cada uno corre de forma
independiente.

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

- `/agregarpalabra <palabra>` — agrega una palabra o frase. Para agregar
  varias a la vez, ponlas en líneas separadas:

  ```
  /agregarpalabra palabra1
  palabra2
  palabra3
  ```

- `/eliminarpalabra <palabra>` — igual, pero elimina (una o varias, una por
  línea).
- `/palabras` — lista las palabras configuradas.

El castigo (nada / silenciar / banear), la duración del silencio y si se
borra o no el mensaje se configuran desde el menú (`/menu` → 🚫 Palabras
prohibidas). El propietario y los administradores del grupo nunca son
afectados por el filtro. La detección es por coincidencia de texto (no
distingue mayúsculas/minúsculas) dentro del mensaje o del pie de foto.

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
├── main.py                 # Punto de entrada, registro de handlers
├── config.py                # Carga de configuración desde .env
├── database.py               # Capa de acceso a datos (SQLite async)
├── requirements.txt
├── .env
├── handlers/
│   ├── moderation.py         # /ban /kick /mute /unmute /unban
│   ├── admin.py               # /admin /unadmin /admins /staff
│   ├── utils_cmds.py           # /del /id /ping /info
│   ├── afk.py                    # "brb" en texto plano (+ /brb) y detección automática
│   ├── recurring.py               # Mensajes recurrentes (texto/media/botones/intervalo/pin)
│   ├── filters_words.py            # Palabras prohibidas + castigo (mute/ban) + auto-borrado
│   ├── cleanup.py                   # Auto-eliminar avisos de servicio y mensajes de comandos
│   └── menu.py                       # Menú de botones (/start, /menu)
├── utils/
│   ├── permissions.py         # Reglas de permisos y jerarquía
│   ├── parsing.py               # Resolución de usuario objetivo
│   ├── time_parser.py            # Parser de duraciones (10m, 2h, 3d...)
│   ├── formatting.py              # Helpers MarkdownV2
│   ├── entities.py                 # Serialización de entities (emojis premium) y botones inline
│   └── logger.py                    # Configuración de logging
└── logs/
    └── bot.log                     # Generado en tiempo de ejecución
```
