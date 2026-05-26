import json
import time
import re
import os
import smtplib
import requests  # <-- Necesario para la API gratuita de mapas OSRM
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

from bs4 import BeautifulSoup

JSON_FILE = 'offers.json'
DISTANCES_CACHE_FILE = 'imoova_distances.json'  # <-- Guardará los km entre ciudades para no repetir cálculos
URL = 'https://www.imoova.com/es/relocations/europe'
BASE_URL = 'https://www.imoova.com'
OFFER_SELECTOR = 'ul.grid li a[href*="/relocations/deal/"]'

smtp_server = 'smtp.gmail.com'
smtp_port = 587

smtp_user = os.environ.get('SMTP_USER')
smtp_password = os.environ.get('SMTP_PASSWORD')
from_email = smtp_user
to_email = os.environ.get('SMTP_TO', smtp_user)

if not smtp_user or not smtp_password:
    raise ValueError("Faltan variables de entorno SMTP_USER o SMTP_PASSWORD")


# Coordenadas aproximadas de las oficinas de Imoova en Europa para OSRM
# Esto evita tener que usar un geocodificador lento.
COORDENADAS_OFICINAS = {
    "LONDON": "-0.1278,51.5074", "DUBLIN": "-6.2603,53.3498", "PARIS": "2.3522,48.8566",
    "BARCELONA": "2.1734,41.3851", "MADRID": "-3.7038,40.4167", "MUNICH": "11.5820,48.1351",
    "AMSTERDAM": "4.8952,52.3702", "BRUSSELS": "4.3517,50.8503", "LISBON": "-9.1393,38.7223",
    "PORTO": "-8.6291,41.1579", "MILAN": "9.1900,45.4642", "ROME": "12.4964,41.9028",
    "FRANKFURT": "8.6821,50.1109", "BERLIN": "13.4050,52.5200", "LYON": "4.8357,45.7640",
    "MARSEILLE": "5.3698,43.2965", "VIENNA": "16.3738,48.2082", "ZURICH": "8.5417,47.3769",
    "GENEVA": "6.1432,46.2044", "PRAGUE": "14.4378,50.0755", "WARSAW": "21.0122,52.2297",
    "SPLIT": "16.4402,43.5081", "EDINBURGH": "-3.1883,55.9533", "BIRMINGHAM": "-1.8904,52.4862",
    "BRISTOL": "-2.5879,51.4545", "MANCHESTER": "-2.2426,53.4808", "CORK": "-8.4756,51.8985",
    "BELFAST": "-5.9301,54.5973", "VALENCIA": "-0.3763,39.4699", "MALAGA": "-4.4203,36.7213",
    "BILBAO": "-2.9350,43.2630", "SEVILLE": "-5.9845,37.3891", "BORDEAUX": "-0.5792,44.8378",
    "NICE": "7.2620,43.7102", "FARO": "-7.9304,37.0179", "HAMBURG": "9.9937,53.5511",
    "DUSSELDORF": "6.7735,51.2277", "COLOGNE": "6.9583,50.9375", "STUTTGART": "9.1813,48.7758"
}

def load_distances_cache():
    if os.path.exists(DISTANCES_CACHE_FILE):
        try:
            with open(DISTANCES_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_distances_cache(cache):
    with open(DISTANCES_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)

def get_exact_road_distance(origin, destination, cache):
    """
    Obtiene los km exactos por carretera utilizando la API de OSRM.
    Si la ruta no es posible por tierra (ej. Londres -> Dublín), devuelve -1.
    """
    if not origin or not destination:
        return None

    orig_key = origin.upper().strip()
    dest_key = destination.upper().strip()
    route_key = f"{orig_key}→{dest_key}"

    # 1. Intentar obtenerlo desde el caché local para máxima velocidad
    if route_key in cache:
        return cache[route_key]

    # 2. Buscar las coordenadas preconfiguradas
    coord_orig = COORDENADAS_OFICINAS.get(orig_key)
    coord_dest = COORDENADAS_OFICINAS.get(dest_key)

    # Si la oficina es nueva y no está en el diccionario, intentamos limpiar el texto para buscar coincidencia parcial
    if not coord_orig or not coord_dest:
        for depto, coords in COORDENADAS_OFICINAS.items():
            if depto in orig_key: coord_orig = coords
            if depto in dest_key: coord_dest = coords

    # Si aun así no tenemos coordenadas, no podemos calcular mediante OSRM de forma directa
    if not coord_orig or not coord_dest:
        return None

    # 3. Consultar al servidor público de OSRM (OpenStreetMap Routing)
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{coord_orig};{coord_dest}?overview=false"
        response = requests.get(url, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 'Ok' and data.get('routes'):
                # OSRM devuelve metros, convertimos a Kilómetros reales de conducción
                dist_km = round(data['routes'][0]['distance'] / 1000, 1)
                cache[route_key] = dist_km
                save_distances_cache(cache)
                return dist_km
    except Exception as e:
        print(f"[Distancia] Error al conectar con OSRM para {route_key}: {e}")

    # Si OSRM responde que no hay ruta (porque hay un océano en medio como Londres-Dublín), guardamos -1
    cache[route_key] = -1
    save_distances_cache(cache)
    return -1


def load_previous():
    try:
        with open(JSON_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_offers(offers):
    with open(JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(offers, f, indent=2, ensure_ascii=False)


def extract_offer_id(href):
    match = re.search(r'(RLC\d+)', href, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return href.rstrip('/').split('/')[-1]


def extract_origin_destination(a):
    h3 = a.find('h3')
    if not h3:
        return None, None

    text = h3.get_text(" ", strip=True)

    if '→' in text:
        parts = text.split('→', 1)
    elif ' to ' in text.lower():
        parts = re.split(r'\s+to\s+', text, maxsplit=1, flags=re.IGNORECASE)
    else:
        return text, None

    if len(parts) != 2:
        return text, None

    return parts[0].strip(), parts[1].strip()


def extract_nights(a):
    text = a.get_text(" ", strip=True).lower()

    match = re.search(
        r'(\d+)\s*\+\s*\d+\s*(noche|noches|night|nights|día|días|dia|dias|day|days)',
        text
    )
    if match:
        return int(match.group(1))

    match = re.search(
        r'(\d+)\s*(noche|noches|night|nights|día|días|dia|dias|day|days)',
        text
    )
    if match:
        return int(match.group(1))

    return None


def extract_offers(html):
    soup = BeautifulSoup(html, 'html.parser')
    offers = []

    offer_elements = soup.select(OFFER_SELECTOR)
    print(f"[extract_offers] Ofertas encontradas: {len(offer_elements)}")

    for a in offer_elements:
        href = a.get('href')
        if not href:
            continue

        offer_id = extract_offer_id(href)

        if href.startswith('http'):
            full_link = href
        else:
            full_link = f'{BASE_URL}{href}'

        origin, destination = extract_origin_destination(a)

        time_elements = a.find_all('time')
        dates = " - ".join(
            t.get_text(" ", strip=True)
            for t in time_elements
        ) if time_elements else None

        nights = extract_nights(a)

        offers.append({
            'id': offer_id,
            'origin': origin,
            'destination': destination,
            'nights': nights,
            'dates': dates,
            'link': full_link
        })

    return offers


def send_email(new_offers):
    if not new_offers:
        return

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"Nuevas ofertas imoova ({len(new_offers)})"

    body = "Se han detectado nuevas ofertas con mas de 3 noches y viables por carretera:\n\n"

    for offer in new_offers:
        body += (
            f"- {offer['origin']} → {offer['destination']} \n"
            f"  Distancia exacta por carretera: {offer['distance_km']} km\n"
            f"  Duración: {offer['nights']} noches | Fechas: {offer['dates']}\n"
            f"  Link: {offer['link']}\n\n"
        )

    msg.attach(MIMEText(body, 'plain', 'utf-8'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
        print("Correo enviado correctamente.")
    except Exception as e:
        print(f"Error al enviar el correo: {e}")


def main():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = "/usr/bin/chromium-browser"

    driver = webdriver.Chrome(options=options)
    driver.get(URL)

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, OFFER_SELECTOR))
        )
    except TimeoutException:
        print("No hay anuncios disponibles. Saliendo.")
        driver.quit()
        return

    print("Haciendo scroll por tramos...")
    last_count = 0
    same_count_attempts = 0

    while same_count_attempts < 10:
        driver.execute_script("window.scrollBy(0, 1300);")
        time.sleep(2)

        elements = driver.find_elements(By.CSS_SELECTOR, OFFER_SELECTOR)
        current_count = len(elements)
        print(f"Anuncios visibles: {current_count}")

        if current_count == last_count:
            same_count_attempts += 1
        else:
            same_count_attempts = 0
            last_count = current_count

    html = driver.page_source
    driver.quit()

    offers = extract_offers(html)

    if not offers:
        print("No se han encontrado ofertas. Saliendo.")
        return

    previous_offers = load_previous()
    previous_ids = {o['id'] for o in previous_offers}
    
    # Cargamos el archivo de caché de distancias
    distances_cache = load_distances_cache()

    new_offers = []
    
    for offer in offers:
        # Filtro inicial básico
        if offer['id'] in previous_ids:
            continue
        if offer['nights'] is None or offer['nights'] <= 3:
            continue

        # CALCULO DE KM REALES
        km_reales = get_exact_road_distance(offer['origin'], offer['destination'], distances_cache)
        
        # Si devuelve -1 significa que OSRM determinó que NO hay conexión por carretera (Ej: Londres -> Dublín)
        if km_reales == -1 or km_reales is None:
            print(f"   [Filtro Km] Descartada ruta marítima/imposible: {offer['origin']} → {offer['destination']}")
            continue

        # Si es válida, le asignamos los km y la preparamos para el mail
        offer['distance_km'] = km_reales
        new_offers.append(offer)

    if new_offers:
        print(f"Nuevas ofertas válidas detectadas: {len(new_offers)}")
        save_offers(previous_offers + new_offers)
        send_email(new_offers)
    else:
        print("No hay nuevas ofertas terrestres válidas en esta ejecución.")


if __name__ == "__main__":
    main()
