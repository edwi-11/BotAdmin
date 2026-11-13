"""
ceo_cleaner/
Módulo independiente de CEO Cleaner (limpieza automática de grupos).

- shared.py : horarios, firma HMAC y constantes (lo usan CEO y CEO Cleaner).
- worker.py : ejecución de la limpieza (solo lo usa el proceso de CEO Cleaner).

CEO (main.py) solo programa y encola; CEO Cleaner (cleaner_bot.py) solo ejecuta.
"""
