# Cómo subir el bot gratis (para que no dependa de tu computadora)

Este bot usa **polling** (se conecta él mismo a Telegram, no necesita que
te escriban a una URL pública) y tiene **tareas programadas** (los
mensajes recurrentes, los mutes temporales). Eso significa una cosa
importante:

> El proceso de Python tiene que quedarse **encendido las 24 horas**. Si
> el servidor "duerme" el bot deja de responder y los mensajes recurrentes
> no se disparan a tiempo.

Muchos servicios "gratis" (Render, Railway, Replit) en su plan gratuito
apagan tu aplicación cuando no tiene tráfico durante un rato, lo cual
rompe justo esas dos cosas. Por eso, para este bot en concreto, la opción
que de verdad se mantiene encendida gratis y sin trucos es una
**máquina virtual gratuita para siempre (Oracle Cloud "Always Free")**.
Abajo dejo esa opción como la recomendada, y otras alternativas más
rápidas de configurar pero con sus limitaciones, por si prefieres
probarlas primero.

---

## Opción recomendada: Oracle Cloud "Always Free" (VM gratis para siempre)

Oracle Cloud regala, de forma permanente (no es una prueba de 30 días),
una máquina virtual Linux con hasta 4 núcleos ARM y 24 GB de RAM — muchísimo
más de lo que este bot necesita. Es la única opción de esta lista con un
servidor que **nunca se apaga por inactividad**.

**Antes de empezar:** Oracle pide una tarjeta para verificar tu identidad,
pero **no te cobra nada** mientras te quedes dentro de los límites
"Always Free" (y este bot los usa de sobra sin acercarse a ellos). En
algunas regiones/países el registro puede tardar en aprobarse o decirte
que no hay "capacidad" disponible; si eso pasa, simplemente intenta de
nuevo más tarde o prueba eligiendo otra región al registrarte.

### Pasos

1. **Crea la cuenta**: entra a https://signup.oraclecloud.com , completa
   tus datos y verifica tu correo y tarjeta. Elige con cuidado el "home
   region" (no se puede cambiar después).

2. **Crea la máquina virtual**:
   - En el menú ☰ → *Compute* → *Instances* → *Create instance*.
   - En "Image and shape" pulsa *Edit*, elige *Change shape*, selecciona
     **Ampere (ARM)** → `VM.Standard.A1.Flex` y déjalo con 1-2 OCPUs y
     6-12 GB de RAM (sobra para este bot; así ni siquiera usas todo tu
     cupo gratis, por si luego quieres otra cosa).
   - En "Image" elige **Ubuntu** (la versión LTS más reciente disponible).
   - Descarga la clave SSH privada que te ofrece generar, o sube la tuya.
   - Dale a *Create* y espera a que el estado quede en verde ("Running").

3. **Conéctate por SSH** desde tu computadora (esto es solo para
   configurarlo una vez; después el bot corre solo en el servidor):

   ```bash
   ssh -i tu_clave_privada.key ubuntu@IP_PUBLICA_DE_LA_VM
   ```

4. **Instala Python y las dependencias del sistema:**

   ```bash
   sudo apt update && sudo apt install -y python3-pip python3-venv git unzip
   ```

5. **Sube el proyecto.** La forma más simple: comprime la carpeta `bot/`
   en tu computadora y súbela con `scp`:

   ```bash
   scp -i tu_clave_privada.key bot.zip ubuntu@IP_PUBLICA_DE_LA_VM:~
   ```

   Luego, ya conectado por SSH:

   ```bash
   unzip bot.zip -d bot
   cd bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

6. **Configura tu `.env`** con tu token real (`nano .env`, pega los
   valores, `Ctrl+O` para guardar y `Ctrl+X` para salir):

   ```
   BOT_TOKEN=tu_token_de_botfather
   OWNER_IDS=tu_id_de_telegram
   DATABASE_PATH=database/bot.db
   LOG_LEVEL=INFO
   ```

7. **Haz que el bot se quede corriendo siempre**, incluso si cierras la
   sesión SSH o la VM se reinicia, creando un servicio con `systemd`:

   ```bash
   sudo nano /etc/systemd/system/telegrambot.service
   ```

   Pega esto (ajusta la ruta si tu usuario/carpeta se llama distinto):

   ```ini
   [Unit]
   Description=Bot de moderacion de Telegram
   After=network.target

   [Service]
   User=ubuntu
   WorkingDirectory=/home/ubuntu/bot
   ExecStart=/home/ubuntu/bot/venv/bin/python main.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   Y actívalo:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable telegrambot
   sudo systemctl start telegrambot
   ```

8. **Verifica que esté vivo:**

   ```bash
   sudo systemctl status telegrambot
   journalctl -u telegrambot -f     # ver los logs en vivo, Ctrl+C para salir
   ```

Listo: el bot queda corriendo 24/7 en Oracle, gratis, y `Restart=always`
hace que si algo lo tumba (un error, un reinicio de la VM) vuelva a
levantarse solo.

Para actualizar el código más adelante: sube los archivos nuevos por
`scp`, y corre `sudo systemctl restart telegrambot`.

---

## Alternativas más rápidas de configurar (con matices)

Si por ahora solo quieres **probar** el bot sin pelearte con una VM,
estas opciones son más fáciles de arrancar, pero ninguna te da un
proceso verdaderamente 24/7 gratis para siempre:

- **Render** (render.com): despliegue gratuito por GitHub muy simple,
  pero el plan gratis "duerme" el servicio tras ~15 minutos sin tráfico
  entrante, y para un bot con polling y tareas programadas eso significa
  que se puede saltar advertencias, mutes temporales o mensajes
  recurrentes mientras está dormido. Sirve para probar, no para producción.
- **Railway** (railway.com): ya no tiene un plan gratuito indefinido;
  da un pequeño crédito mensual (no alcanza para 24/7 real) y luego pide
  el plan Hobby de pago (~5 USD/mes).
- **PythonAnywhere**: el plan gratuito no permite procesos "always-on"
  (eso es de pago); solo sirve para tareas puntuales/programadas cortas.

En resumen: si de verdad quieres olvidarte de tu computadora y que el
bot funcione siempre, la VM gratuita de Oracle (o, si prefieres pagar
algo simbólico, una VPS económica de 3-5 USD/mes tipo Hetzner/DigitalOcean/
Railway Hobby) es el camino más confiable. Las plataformas 100% gratis
del resto están pensadas para apps web que responden a visitas, no para
procesos en segundo plano que deben estar despiertos todo el tiempo.

---

## Bot anunciador (opcional)

Si decides usar `broadcast_bot.py` (ver README para configurarlo), es
un proceso Python independiente — dale su propio servicio systemd para
que también se quede corriendo 24/7:

```bash
sudo nano /etc/systemd/system/telegrambroadcast.service
```

```ini
[Unit]
Description=Bot anunciador de Telegram
After=network.target

[Service]
User=root
WorkingDirectory=/root/BotAdmin/bot
ExecStart=/root/BotAdmin/bot/venv/bin/python broadcast_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegrambroadcast
sudo systemctl start telegrambroadcast
```

Ambos procesos (`telegrambot` y `telegrambroadcast`) pueden correr al
mismo tiempo sin problema: usan tokens distintos y comparten la misma
base de datos solo para leer/escribir la cola de anuncios.

---

*(Esta guía se escribió en julio de 2026; las condiciones de estos
servicios cambian con frecuencia — conviene revisar la página oficial de
cada uno antes de decidir.)*
