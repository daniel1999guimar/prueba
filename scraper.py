import json
import time
import re
import os
import smtplib
import html
import requests
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
ROUTES_FILE = 'route_cache.json'

URL = 'https://www.imoova.com/es/relocations/europe'
BASE_URL = 'https://www.imoova.com'
OFFER_SELECTOR = 'ul.grid li a[href*="/relocations/deal/"]'

MIN_NIGHTS = 4
USER_AGENT = 'imoova-scraper-alert/1.0'

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


def sort_by_shortest_route(offers):
    return sorted(
        offers,
        key=lambda offer: (
            offer.get('route_distance_km') is None,
            offer.get('route_distance_km') or 999999
        )
    )


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


def load_route_cache():
    data = load_json(ROUTES_FILE, {})
    return data if isinstance(data, dict) else {}


def save_route_cache(cache):
    save_json(ROUTES_FILE, cache)


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
    time.sleep(1.1)

    return coords


def get_driving_route(origin_coords, destination_coords):
    if not origin_coords or not destination_coords:
        return None

    coords = (
        f"{origin_coords['lon']},{origin_coords['lat']};"
        f"{destination_coords['lon']},{destination_coords['lat']}"
    )

    url = (
        f"https://router.project-osrm.org/route/v1/driving/{coords}"
        "?overview=false&steps=true&alternatives=false"
    )

    response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=25)
    response.raise_for_status()

    data = response.json()

    if data.get('code') != 'Ok' or not data.get('routes'):
        return None

    route = data['routes'][0]
    steps = []

    for leg in route.get('legs', []):
        steps.extend(leg.get('steps', []))

    ferry_steps = [
        step for step in steps
        if (step.get('mode') or '').lower() == 'ferry'
    ]

    ferry_names = []
    for step in ferry_steps:
        name = normalize_space(step.get('name'))
        if name and name not in ferry_names:
            ferry_names.append(name)

    return {
        'route_distance_km': round(route.get('distance', 0) / 1000),
        'route_duration_hours': round(route.get('duration', 0) / 3600, 1),
        'route_has_ferry': bool(ferry_steps),
        'ferry_names': ferry_names
    }


def add_route_info_to_offers(offers):
    coords_cache = load_coords_cache()
    route_cache = load_route_cache()
    geolocator = Nominatim(user_agent=USER_AGENT)

    for offer in offers:
        origin = offer.get('origin')
        destination = offer.get('destination')

        route_key = f"{origin}|{destination}".lower()

        if route_key in route_cache:
            offer.update(route_cache[route_key])
            continue

        origin_coords = geocode_city(origin, coords_cache, geolocator)
        destination_coords = geocode_city(destination, coords_cache, geolocator)

        try:
            route_info = get_driving_route(origin_coords, destination_coords)
        except requests.RequestException as e:
            log(f"No se pudo calcular ruta {origin} -> {destination}: {e}")
            route_info = None

        if not route_info:
            route_info = {
                'route_distance_km': None,
                'route_duration_hours': None,
                'route_has_ferry': False,
                'ferry_names': []
            }

        offer.update(route_info)
        route_cache[route_key] = route_info

        time.sleep(0.3)

    save_coords_cache(coords_cache)
    save_route_cache(route_cache)


def extract_offers(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
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


def build_offer_card(offer):
    origin = html.escape(offer.get('origin') or 'Origen desconocido')
    destination = html.escape(offer.get('destination') or 'Destino desconocido')
    dates = html.escape(offer.get('dates') or 'Sin fechas')
    link = html.escape(offer.get('link') or '#')
    nights = offer.get('nights') or '-'

    distance = offer.get('route_distance_km')
    duration = offer.get('route_duration_hours')
    has_ferry = offer.get('route_has_ferry')
    ferry_names = offer.get('ferry_names') or []

    distance_text = f"{distance:,} km".replace(",", ".") if distance else "No disponible"
    duration_text = f"{duration} h" if duration else "No disponible"

    if has_ferry:
        if ferry_names:
            ferry_text = "Sí: " + ", ".join(html.escape(name) for name in ferry_names[:3])
        else:
            ferry_text = "Sí"
        ferry_badge = f"""
            <span style="display:inline-block;background:#fff7ed;color:#9a3412;border:1px solid #fed7aa;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:700;">
                Barco: {ferry_text}
            </span>
        """
    else:
        ferry_badge = """
            <span style="display:inline-block;background:#f0fdf4;color:#166534;border:1px solid #bbf7d0;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:700;">
                Sin barco detectado
            </span>
        """

    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:8px;background:#ffffff;margin:0 0 14px;overflow:hidden;">
        <div style="padding:16px 18px;">
            <div style="font-size:18px;font-weight:800;color:#111827;line-height:1.3;">
                {origin} → {destination}
            </div>

            <div style="font-size:13px;color:#6b7280;margin-top:5px;">
                {dates}
            </div>

            <div style="margin-top:12px;">
                <span style="display:inline-block;background:#dcfce7;color:#166534;border:1px solid #bbf7d0;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:700;">
                    {nights} noches
                </span>
                <span style="display:inline-block;background:#eff6ff;color:#1d4ed8;border:1px solid #bfdbfe;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:700;margin-left:5px;">
                    {distance_text}
                </span>
                <span style="display:inline-block;background:#f8fafc;color:#334155;border:1px solid #e2e8f0;padding:5px 9px;border-radius:6px;font-size:12px;font-weight:700;margin-left:5px;">
                    {duration_text}
                </span>
                <span style="display:inline-block;margin-left:5px;">
                    {ferry_badge}
                </span>
            </div>

            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:16px;">
                <tr>
                    <td bgcolor="#2563eb" style="border-radius:6px;">
                        <a href="{link}" target="_blank" style="display:inline-block;padding:11px 16px;font-family:Arial,sans-serif;font-size:14px;font-weight:700;color:#ffffff;text-decoration:none;white-space:nowrap;">
                            Ver oferta
                        </a>
                    </td>
                </tr>
            </table>
        </div>
    </div>
    """


def build_email_html(new_offers):
    sorted_offers = sort_by_shortest_route(new_offers)
    cards = "".join(build_offer_card(offer) for offer in sorted_offers)

    return f"""
    <html>
    <body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,sans-serif;color:#111827;">
        <div style="max-width:760px;margin:0 auto;padding:22px;">
            <div style="padding:22px 24px;background:#111827;color:#ffffff;border-radius:8px 8px 0 0;">
                <h1 style="margin:0;font-size:22px;line-height:1.25;">
                    Nuevas ofertas Imoova
                </h1>
                <p style="margin:8px 0 0;color:#d1d5db;font-size:14px;line-height:1.5;">
                    {len(new_offers)} nuevas ofertas con más de {MIN_NIGHTS} noches, ordenadas por distancia en coche.
                </p>
            </div>

            <div style="padding:16px;background:#ffffff;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb;">
                {cards}
            </div>

            <div style="padding:15px 18px;color:#6b7280;font-size:12px;line-height:1.5;background:#f9fafb;border:1px solid #e5e7eb;border-radius:0 0 8px 8px;">
                Distancia y duración aproximadas por ruta en coche usando OSRM/OpenStreetMap. Si aparece barco, la ruta calculada incluye al menos un tramo ferry.
            </div>
        </div>
    </body>
    </html>
    """


def build_email_text(new_offers):
    sorted_offers = sort_by_shortest_route(new_offers)
    body = f"Se han detectado {len(sorted_offers)} nuevas ofertas con mas de {MIN_NIGHTS} noches, ordenadas por distancia en coche:\n\n"

    for offer in sorted_offers:
        ferry_text = "Si" if offer.get('route_has_ferry') else "No"
        if offer.get('route_has_ferry') and offer.get('ferry_names'):
            ferry_text += f" ({', '.join(offer.get('ferry_names'))})"

        body += (
            f"- {offer.get('origin')} → {offer.get('destination')}\n"
            f"  Noches: {offer.get('nights')}\n"
            f"  Fechas: {offer.get('dates')}\n"
            f"  Distancia coche: {offer.get('route_distance_km') or 'No disponible'} km\n"
            f"  Duracion aprox: {offer.get('route_duration_hours') or 'No disponible'} h\n"
            f"  Barco: {ferry_text}\n"
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

    msg.attach(MIMEText(build_email_text(new_offers), 'plain', 'utf-8'))
    msg.attach(MIMEText(build_email_html(new_offers), 'html', 'utf-8'))

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
        html_content = load_all_offers(driver)
    except WebDriverException as e:
        log(f"Error con Selenium/ChromeDriver: {e}")
        return
    finally:
        if driver:
            driver.quit()

    if not html_content:
        return

    offers = extract_offers(html_content)

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
        add_route_info_to_offers(new_offers)
        send_email(new_offers)
    else:
        log(f"No hay nuevas ofertas con mas de {MIN_NIGHTS} noches.")

    save_offers(previous_offers + offers)


if __name__ == "__main__":
    main()
