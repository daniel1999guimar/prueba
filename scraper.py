import json
import time
import re
import os
import smtplib
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
URL = 'https://www.imoova.com/es/relocations/europe'
BASE_URL = 'https://www.imoova.com'
OFFER_SELECTOR = 'ul.grid li a[href*="/relocations/deal/"]'

# LISTA NEGRA: Si el origen o destino contiene alguna de estas ciudades, se elimina automáticamente
CIUDADES_PROHIBIDAS = [
    "LONDON", "DUBLIN", "EDINBURGH", "MANCHESTER", 
    "BRISTOL", "STOCKHOLM", "BELFAST", "CORK", "INVERNESS"
]

smtp_server = 'smtp.gmail.com'
smtp_port = 587

smtp_user = os.environ.get('SMTP_USER')
smtp_password = os.environ.get('SMTP_PASSWORD')
from_email = smtp_user
to_email = os.environ.get('SMTP_TO', smtp_user)

if not smtp_user or not smtp_password:
    raise ValueError("Faltan variables de entorno SMTP_USER o SMTP_PASSWORD")


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
    soup = BeautifulSoup(html, 'lxml')
    offers = []

    offer_elements = soup.select(OFFER_SELECTOR)
    print(f"[extract_offers] Ofertas encontradas en lista principal: {len(offer_elements)}")

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


def extract_km_from_deal(driver, url):
    """
    Entra a la oferta individual y raspea los kilómetros exactos del texto de Imoova.
    """
    try:
        driver.get(url)
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'km')]"))
        )
        
        soup = BeautifulSoup(driver.page_source, 'lxml')
        paragraphs = soup.find_all('p', class_=lambda c: c and 'text-sm' in c)
        for p in paragraphs:
            text = p.get_text(strip=True).lower()
            match = re.search(r'(\d+)\s*km', text)
            if match:
                return int(match.group(1))
                
    except Exception as e:
        print(f"   [Error] No se pudieron extraer los km de {url}: {e}")
        
    return None


def send_email(new_offers):
    if not new_offers:
        return

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"Nuevas ofertas imoova ({len(new_offers)})"

    body = "Se han detectado nuevas ofertas con mas de 3 noches y rutas deseadas:\n\n"

    for offer in new_offers:
        km_str = f"{offer['distance_km']} km" if offer.get('distance_km') else "No especificados"
        body += (
            f"- {offer['origin']} → {offer['destination']} \n"
            f"  Distancia oficial: {km_str}\n"
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

    offers = extract_offers(html)

    if not offers:
        print("No se han encontrado ofertas. Saliendo.")
        driver.quit()
        return

    previous_offers = load_previous()
    previous_ids = {o['id'] for o in previous_offers}

    potential_new_offers = [
        o for o in offers
        if o['id'] not in previous_ids
        and o['nights'] is not None
        and o['nights'] > 3
    ]

    new_offers = []
    
    for offer in potential_new_offers:
        orig_upper = offer['origin'].upper() if offer['origin'] else ""
        dest_upper = offer['destination'].upper() if offer['destination'] else ""
        
        # FILTRO DE CIUDADES PROHIBIDAS (Lista negra directa)
        # Si el origen o el destino contienen alguna palabra de la lista negra, saltamos el anuncio inmediatamente
        if any(ciudad in orig_upper for ciudad in CIUDADES_PROHIBIDAS) or any(ciudad in dest_upper for ciudad in CIUDADES_PROHIBIDAS):
            print(f"   [Lista Negra] Saltando oferta bloqueada: {offer['origin']} → {offer['destination']}")
            continue
            
        print(f"Abriendo detalle de oferta válida: {offer['id']} ({offer['origin']} -> {offer['destination']})")
        
        # Extraemos los km de la página interna del trayecto continental
        km = extract_km_from_deal(driver, offer['link'])

        offer['distance_km'] = km
        new_offers.append(offer)
        time.sleep(1)

    driver.quit()

    if new_offers:
        print(f"Nuevas ofertas detectadas: {len(new_offers)}")
        save_offers(previous_offers + new_offers)
        send_email(new_offers)
    else:
        print("No hay nuevas ofertas terrestres que cumplan los requisitos de ciudad y noches.")


if __name__ == "__main__":
    main()
