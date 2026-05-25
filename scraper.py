import json
import time
import re
import os
import smtplib
import math
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

from bs4 import BeautifulSoup


JSON_FILE = 'offers.json'
COORDS_FILE = 'city_coords.json'

URL = 'https://www.imoova.com/es/relocations/europe'
BASE_URL = 'https://www.imoova.com'
OFFER_SELECTOR = 'ul.grid li a[href*="/relocations/deal/"]'

MIN_NIGHTS = 4

smtp_server = 'smtp.gmail.com'
smtp_port = 587

smtp_user = os.environ.get('SMTP_USER')
smtp_password = os.environ.get('SMTP_PASSWORD')
from_email = smtp_user
to_email = os.environ.get('SMTP_TO', smtp_user)

if not smtp_user or not smtp_password:
    raise ValueError("Faltan variables de entorno SMTP_USER o SMTP_PASSWORD")


def log(message):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] {message}")


def sort_by_shortest_distance(offers):
    return sorted(
        offers,
        key=lambda offer: (
            offer.get('distance_km') is None,
            offer.get('distance_km') or 999999
        )
    )


def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_previous():
    data = load_json(JSON_FILE, [])
    return data if isinstance(data, list) else []


def save_offers(offers):
    unique = {}
    for offer in offers:
        unique[offer['id']] = offer

    save_json(JSON_FILE, list(unique.values()))


def normalize_space(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def extract_offer_id(href):
    match = re.search(r'(RLC\d+)', href, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    return href.rstrip('/').split('/')[-1]


def extract_origin_destination(a):
    h3 = a.find('h3')
    if not h3:
        return None, None

    text = normalize_space(h3.get_text(" ", strip=True))

    if '→' in text:
        parts = text.split('→', 1)
    elif ' to ' in text.lower():
        parts = re.split(r'\s+to\s+', text, maxsplit=1, flags=re.IGNORECASE)
    else:
        return text, None

    return normalize_space(parts[0]), normalize_space(parts[1])


def extract_nights(a):
    text = normalize_space(a.get_text(" ", strip=True)).lower()

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


def extract_dates(a):
    time_elements = a.find_all('time')

    if time_elements:
        return " - ".join(
            normalize_space(t.get_text(" ", strip=True))
            for t in time_elements
        )

    text = normalize_space(a.get_text(" ", strip=True))
    match = re.search(
        r'Available\s+(.+?)\s+\d+\s*(?:\+|noche|noches|night|nights|día|días|dia|dias|day|days)',
        text,
        re.IGNORECASE
    )

    return normalize_space(match.group(1)) if match else None


def load_coords_cache():
    data = load_json(COORDS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_coords_cache(cache):
    save_json(COORDS_FILE, cache)


def geocode_city(city, cache, geolocator):
    if not city:
        return None

    key = city.strip().lower()

    if key in cache:
        return cache[key]

    try:
        location = geolocator.geocode(f"{city}, Europe", timeout=10)
    except (GeocoderTimedOut, GeocoderUnavailable):
        return None

    if not location:
        cache[key] = None
        return None

    coords = {
        'lat': location.latitude,
        'lon': location.longitude
    }

    cache[key] = coords
    time.sleep(1)

    return coords


def haversine_km(coord1, coord2):
    if not coord1 or not coord2:
        return None

    lat1 = math.radians(coord1['lat'])
    lon1 = math.radians(coord1['lon'])
    lat2 = math.radians(coord2['lat'])
    lon2 = math.radians(coord2['lon'])

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )

    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return round(6371 * c)


def add_distance_to_offers(offers):
    cache = load_coords_cache()
    geolocator = Nominatim(user_agent="imoova_scraper_email_alert")

    for offer in offers:
        origin = offer.get('origin')
        destination = offer.get('destination')

        origin_coords = geocode_city(origin, cache, geolocator)
        destination_coords = geocode_city(destination, cache, geolocator)

        offer['distance_km'] = haversine_km(origin_coords, destination_coords)

    save_coords_cache(cache)


def extract_offers(html):
    soup = BeautifulSoup(html, 'html.parser')
    offers = []
    seen_ids = set()

    offer_elements = soup.select(OFFER_SELECTOR)
    log(f"Ofertas encontradas en HTML: {len(offer_elements)}")

    for a in offer_elements:
        href = a.get('href')
        if not href:
            continue

        offer_id = extract_offer_id(href)

        if offer_id in seen_ids:
            continue

        seen_ids.add(offer_id)

        full_link = href if href.startswith('http') else f'{BASE_URL}{href}'
        origin, destination = extract_origin_destination(a)

        offers.append({
            'id': offer_id,
            'origin': origin,
            'destination': destination,
            'nights': extract_nights(a),
            'dates': extract_dates(a),
            'link': full_link,
            'seen_at': datetime.now().isoformat(timespec='seconds')
        })

    return offers


def build_email_html(new_offers):
    rows = ""

    sorted_offers = sort_by_shortest_distance(new_offers)

    for offer in sorted_offers:
        origin = offer.get('origin') or 'Origen desconocido'
        destination = offer.get('destination') or 'Destino desconocido'
        nights = offer.get('nights') or '-'
        dates = offer.get('dates') or 'Sin fechas'
        link = offer.get('link')
        distance = offer.get('distance_km')

        distance_text = f"{distance:,} km".replace(",", ".") if distance else "No disponible"

        rows += f"""
        <tr>
            <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;">
                <div style="font-size:16px;font-weight:700;color:#111827;">
                    {origin} → {destination}
                </div>
                <div style="font-size:13px;color:#6b7280;margin-top:4px;">
                    {dates}
                </div>
            </td>
            <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">
                <span style="display:inline-block;background:#dcfce7;color:#166534;padding:6px 10px;border-radius:999px;font-weight:700;">
                    {nights} noches
                </span>
            </td>
            <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;text-align:center;color:#374151;font-weight:700;">
                {distance_text}
            </td>
            <td style="padding:14px 12px;border-bottom:1px solid #e5e7eb;text-align:right;">
                <a href="{link}" style="background:#2563eb;color:#ffffff;text-decoration:none;padding:9px 12px;border-radius:6px;font-weight:700;">
                    Ver oferta
                </a>
            </td>
        </tr>
        """

    return f"""
    <html>
    <body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;color:#111827;">
        <div style="max-width:860px;margin:0 auto;padding:24px;">
            <div style="background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb;">
                <div style="padding:22px 24px;background:#111827;color:#ffffff;">
                    <h1 style="margin:0;font-size:22px;">
                        Nuevas ofertas Imoova
                    </h1>
                    <p style="margin:8px 0 0;color:#d1d5db;font-size:14px;">
                        {len(new_offers)} nuevas ofertas con más de {MIN_NIGHTS} noches, ordenadas por distancia más corta.
                    </p>
                </div>

                <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;background:#ffffff;">
                    <thead>
                        <tr style="background:#f9fafb;">
                            <th align="left" style="padding:12px;color:#6b7280;font-size:12px;text-transform:uppercase;">Ruta</th>
                            <th align="center" style="padding:12px;color:#6b7280;font-size:12px;text-transform:uppercase;">Noches</th>
                            <th align="center" style="padding:12px;color:#6b7280;font-size:12px;text-transform:uppercase;">Distancia</th>
                            <th align="right" style="padding:12px;color:#6b7280;font-size:12px;text-transform:uppercase;">Link</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows}
                    </tbody>
                </table>

                <div style="padding:16px 24px;color:#6b7280;font-size:12px;background:#f9fafb;">
                    Distancia aproximada en línea recta entre ciudades. Puede diferir mucho de la ruta real por carretera o ferry.
                </div>
            </div>
        </div>
    </body>
    </html>
    """


def build_email_text(new_offers):
    sorted_offers = sort_by_shortest_distance(new_offers)

    body = f"Se han detectado {len(sorted_offers)} nuevas ofertas con mas de {MIN_NIGHTS} noches, ordenadas por distancia mas corta:\n\n"

    for offer in sorted_offers:
        distance = offer.get('distance_km')
        distance_text = f"{distance} km" if distance else "Distancia no disponible"

        body += (
            f"- {offer.get('origin')} → {offer.get('destination')}\n"
            f"  Noches: {offer.get('nights')}\n"
            f"  Fechas: {offer.get('dates')}\n"
            f"  Distancia aproximada: {distance_text}\n"
            f"  Link: {offer.get('link')}\n\n"
        )

    return body


def send_email(new_offers):
    if not new_offers:
        return

    msg = MIMEMultipart('alternative')
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"Imoova: {len(new_offers)} nuevas ofertas de mas de {MIN_NIGHTS} noches"

    text_body = build_email_text(new_offers)
    html_body = build_email_html(new_offers)

    msg.attach(MIMEText(text_body, 'plain', 'utf-8'))
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        log("Correo enviado correctamente.")
    except Exception as e:
        log(f"Error al enviar el correo: {e}")


def create_driver():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--lang=es-ES')

    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = "/usr/bin/chromium-browser"

    return webdriver.Chrome(options=options)


def load_all_offers(driver):
    driver.get(URL)

    try:
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, OFFER_SELECTOR))
        )
    except TimeoutException:
        log("No hay anuncios disponibles. Saliendo.")
        return None

    log("Haciendo scroll por tramos...")

    last_count = 0
    same_count_attempts = 0
    max_scrolls = 60

    for _ in range(max_scrolls):
        driver.execute_script("window.scrollBy(0, 1600);")
        time.sleep(1.5)

        elements = driver.find_elements(By.CSS_SELECTOR, OFFER_SELECTOR)
        current_count = len(elements)
        log(f"Anuncios visibles: {current_count}")

        if current_count == last_count:
            same_count_attempts += 1
        else:
            same_count_attempts = 0
            last_count = current_count

        if same_count_attempts >= 8:
            break

    return driver.page_source


def main():
    log("Iniciando scraper Imoova.")

    driver = None

    try:
        driver = create_driver()
        html = load_all_offers(driver)
    except WebDriverException as e:
        log(f"Error con Selenium/ChromeDriver: {e}")
        return
    finally:
        if driver:
            driver.quit()

    if not html:
        return

    offers = extract_offers(html)

    if not offers:
        log("No se han encontrado ofertas. Saliendo.")
        return

    valid_offers = [
        offer for offer in offers
        if offer['nights'] is not None and offer['nights'] > MIN_NIGHTS
    ]

    previous_offers = load_previous()
    previous_ids = {offer['id'] for offer in previous_offers}

    new_offers = [
        offer for offer in valid_offers
        if offer['id'] not in previous_ids
    ]

    log(f"Ofertas totales: {len(offers)}")
    log(f"Ofertas con mas de {MIN_NIGHTS} noches: {len(valid_offers)}")
    log(f"Ofertas nuevas con mas de {MIN_NIGHTS} noches: {len(new_offers)}")

    if new_offers:
        add_distance_to_offers(new_offers)
        send_email(new_offers)
    else:
        log(f"No hay nuevas ofertas con mas de {MIN_NIGHTS} noches.")

    save_offers(previous_offers + offers)


if __name__ == "__main__":
    main()
