#!/usr/bin/env python3
"""
Monitor de turnos - Licencia Original (curso presencial) - Municipio de Quilmes.

Solo DETECTA y AVISA por Telegram. No reserva turnos ni evade CAPTCHAs.

Uso:
  python monitor.py                  -> una revisión
  python monitor.py --hasta 10:50    -> revisa en bucle (cada INTERVALO_SEG) hasta esa hora
  python monitor.py --probar-telegram
  python monitor.py --reporte-diario -> revisa y manda el reporte de control (espera a las 00:00)

Variables de entorno: MQ_EMAIL, MQ_PASSWORD, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
Opcionales: INTERVALO_SEG (120), MESES_ADELANTE (1)
"""
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TZ = ZoneInfo("America/Argentina/Buenos_Aires")
LOGIN_URL = "https://mi.quilmes.gov.ar/login"
PUENTE_URL = "https://mi.quilmes.gov.ar/quilmesdigital/login-turnos"
CURSO_URL = "https://arquivirtual.quilmes.gov.ar/municipal/App/RegistroTurnos/CursoV25.aspx"
CAL_ID = "ContentPlaceHolder1_Calendar1"
ESTADO = "estado.json"
HISTORIAL = "historial.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)','([^']*)'\)")
MESES_ADELANTE = int(os.environ.get("MESES_ADELANTE", "1"))
INTERVALO = int(os.environ.get("INTERVALO_SEG", "120"))
ERRORES_PARA_AVISAR = 3  # evita avisos por un corte de red aislado


class ErrorSitio(Exception):
    pass


def ahora():
    return datetime.now(TZ)


def log(msg):
    print(f"[{ahora():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)
    resumen = os.environ.get("GITHUB_STEP_SUMMARY")
    if resumen:
        with open(resumen, "a", encoding="utf-8") as f:
            f.write(f"- {ahora():%H:%M:%S} {msg}\n")


def telegram(texto):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("Telegram no configurado. Mensaje: " + texto)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": texto, "disable_web_page_preview": "true"},
            timeout=20,
        )
        if not r.ok:
            log(f"Error de Telegram {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"No se pudo enviar a Telegram: {e}")


# ---------- Acceso al sitio ----------

def nueva_sesion():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"})
    r = s.get(LOGIN_URL, timeout=30)
    r.raise_for_status()
    tok = BeautifulSoup(r.text, "html.parser").find("input", {"name": "_token"})
    if tok is None:
        raise ErrorSitio("no encontré el formulario de login (¿cambió la página o pide CAPTCHA?)")
    r = s.post(LOGIN_URL, timeout=30, data={
        "_token": tok.get("value", ""),
        "email": os.environ["MQ_EMAIL"],
        "password": os.environ["MQ_PASSWORD"],
    })
    r.raise_for_status()
    if r.url.rstrip("/").endswith("/login"):
        raise ErrorSitio("el login falló: revisá MQ_EMAIL y MQ_PASSWORD en los secrets de GitHub")
    r = s.get(PUENTE_URL, timeout=30)  # pasa la sesión de Mi Quilmes al sistema de turnos
    r.raise_for_status()
    if "arquivirtual.quilmes.gov.ar" not in r.url:
        raise ErrorSitio("no pude pasar de Mi Quilmes al sistema de turnos")
    return s


def leer_calendario(html):
    soup = BeautifulSoup(html, "html.parser")
    cal = soup.find(id=CAL_ID)
    if cal is None:
        return None
    fechas, siguiente = [], None
    for a in cal.find_all("a", href=True):
        m = POSTBACK_RE.search(a["href"])
        if not m:
            continue
        arg = m.group(2)
        if arg.startswith("V"):  # flechas de mes
            if "siguiente" in (a.get("title") or "").lower():
                siguiente = (m.group(1), arg)
        elif arg.isdigit():  # día elegible = hay turno. El número es días desde 1/1/2000
            fechas.append(date(2000, 1, 1) + timedelta(days=int(arg)))
    return {"soup": soup, "fechas": fechas, "siguiente": siguiente}


def campos_formulario(soup):
    form = soup.find("form")
    datos = {}
    for i in form.find_all("input"):
        tipo = (i.get("type") or "text").lower()
        if i.get("name") and tipo not in ("submit", "button", "image", "checkbox", "radio"):
            datos[i["name"]] = i.get("value", "")
    for sel in form.find_all("select"):
        if sel.get("name"):
            op = sel.find("option", selected=True) or sel.find("option")
            datos[sel["name"]] = op.get("value", "") if op else ""
    return datos


def revisar(s):
    """Devuelve la lista de fechas con turno, o None si la sesión venció."""
    r = s.get(CURSO_URL, timeout=30)
    r.raise_for_status()
    if "arquivirtual.quilmes.gov.ar" not in r.url:
        return None
    cal = leer_calendario(r.text)
    if cal is None:
        raise ErrorSitio("no encontré el calendario de turnos en la página")
    fechas = set(cal["fechas"])
    for _ in range(MESES_ADELANTE):  # mira también el mes siguiente (solo cambia la vista)
        if not cal["siguiente"]:
            break
        datos = campos_formulario(cal["soup"])
        datos["__EVENTTARGET"], datos["__EVENTARGUMENT"] = cal["siguiente"]
        r = s.post(CURSO_URL, data=datos, timeout=30)
        r.raise_for_status()
        cal = leer_calendario(r.text)
        if cal is None:
            raise ErrorSitio("no pude ver el mes siguiente del calendario")
        fechas.update(cal["fechas"])
    hoy = ahora().date()
    return sorted(f for f in fechas if f >= hoy)


def una_revision(s):
    try:
        if s is None:
            s = nueva_sesion()
        fechas = revisar(s)
        if fechas is None:  # sesión vencida: vuelvo a entrar una vez
            s = nueva_sesion()
            fechas = revisar(s)
            if fechas is None:
                raise ErrorSitio("la sesión se cierra al entrar al sistema de turnos")
        return s, fechas, None
    except ErrorSitio as e:
        return None, [], str(e)
    except requests.RequestException as e:
        return None, [], f"no se pudo conectar con el sitio ({type(e).__name__})"


# ---------- Estado, historial y avisos ----------

def cargar_estado():
    try:
        with open(ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"fechas": [], "errores_seguidos": 0, "error_avisado": False, "ultimo_dia": None}


def guardar_estado(e):
    with open(ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, indent=2, ensure_ascii=False)


def anotar(resultado, detalle=""):
    nuevo = not os.path.exists(HISTORIAL)
    with open(HISTORIAL, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha_hora", "resultado", "detalle"])
        w.writerow([ahora().strftime("%Y-%m-%d %H:%M"), resultado, detalle])


def fmt(fechas):
    return ", ".join(f.strftime("%d/%m") for f in fechas) or "ninguna"


def procesar(estado, fechas, error):
    if error:
        estado["errores_seguidos"] = estado.get("errores_seguidos", 0) + 1
        log(f"ERROR ({estado['errores_seguidos']} seguidos): {error}")
        if estado["errores_seguidos"] == ERRORES_PARA_AVISAR:
            telegram(f"⚠️ El monitor de turnos no puede revisar la página: {error}. Sigo intentando.")
            anotar("error", error)
            estado["error_avisado"] = True
        return
    if estado.get("error_avisado"):
        telegram("✅ El monitor de turnos volvió a funcionar.")
        anotar("recuperado")
    estado["errores_seguidos"], estado["error_avisado"] = 0, False

    previas = set(estado.get("fechas", []))
    nuevas = [f for f in fechas if f.isoformat() not in previas]
    hoy = ahora().date().isoformat()
    if nuevas:
        telegram(
            "🚨 ¡HAY TURNOS para el curso de Licencia Original!\n"
            f"Fechas nuevas: {fmt(nuevas)}\n"
            f"Todas las disponibles: {fmt(fechas)}\n\n"
            "Entrá ya: https://mi.quilmes.gov.ar/quilmesdigital/login-turnos → LICENCIA ORIGINAL"
        )
        anotar("DISPONIBLE", fmt(fechas))
    elif previas and not fechas:
        anotar("agotado")
    elif estado.get("ultimo_dia") != hoy:
        anotar("sin turnos" if not fechas else "disponible", "primera revisión del día")
    estado["fechas"] = [f.isoformat() for f in fechas]
    estado["ultimo_dia"] = hoy
    log(f"Turnos disponibles: {fmt(fechas)}")


def reporte_diario():
    """Espera hasta las 00:00 (si arrancó antes), hace una revisión y manda el reporte."""
    t = ahora()
    if t.hour == 23:  # GitHub lo lanza 23:45 para compensar sus atrasos
        medianoche = (t + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        espera = (medianoche - t).total_seconds()
        log(f"Esperando {int(espera)} s hasta las 00:00")
        time.sleep(espera)
    estado = cargar_estado()
    s, fechas, error = una_revision(None)
    procesar(estado, fechas, error)  # si hay turnos nuevos, el aviso urgente sale igual
    guardar_estado(estado)
    hora = ahora().strftime("%H:%M")
    if error:
        texto = ("🟠 Reporte diario: el monitor está activo, pero en la revisión de las "
                 f"{hora} no pudo leer la página: {error}. Sigue intentando.")
    elif fechas:
        texto = ("🟢 Reporte diario: el monitor sigue activo y funcionando. "
                 f"En la última revisión ({hora}) HAY turnos disponibles: {fmt(fechas)}.")
    else:
        texto = ("🟢 Reporte diario: el monitor sigue activo y funcionando. "
                 f"En la última revisión ({hora}) no se encontraron turnos disponibles.")
    telegram(texto)
    log("Reporte diario enviado")


def main():
    if "--probar-telegram" in sys.argv:
        telegram("✅ Prueba: el monitor de turnos puede enviarte mensajes.")
        return
    if "--reporte-diario" in sys.argv:
        reporte_diario()
        return
    hasta = None
    if "--hasta" in sys.argv:
        hh, mm = map(int, sys.argv[sys.argv.index("--hasta") + 1].split(":"))
        hasta = ahora().replace(hour=hh, minute=mm, second=0, microsecond=0)
    estado, s = cargar_estado(), None
    while True:
        s, fechas, error = una_revision(s)
        procesar(estado, fechas, error)
        guardar_estado(estado)
        if hasta is None or ahora() + timedelta(seconds=INTERVALO) > hasta:
            break
        time.sleep(INTERVALO)


if __name__ == "__main__":
    main()
