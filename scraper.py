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
URL = 'https://www.imoova.com/en/relocations?region=EU'

# ==========================
# CONFIGURACIÓN EMAIL (SECRETS)
# ==========================
smtp_server = 'smtp.gmail.com'
smtp_port = 587

smtp_user = os.environ.get('SMTP_USER')
smtp_password = os.environ.get('SMTP_PASSWORD')
from_email = smtp_user
to_email = os.environ.get('SMTP_TO', smtp_user)

if not smtp_user or not smtp_password:
    raise ValueError("Faltan variables de entorno SMTP_USER o SMTP_PASSWORD")

# ==========================
# UTILIDADES JSON
# ==========================
def load_previous():
    try:
        with open(JSON_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_offers(offers):
    with open(JSON_FILE, 'w') as f:
        json.dump(offers, f, indent=2)

# ==========================
# PARSEO
# ==========================
def parse_nights(text):
    """
    Regla:
    - '7 + 3 noches' -> 7
    - '21+' -> 21
    - '7 días' -> 7
    """
    if not text:
        return None

    text = text.lower().strip()
    match = re.search(r'\d+', text)
    return int(match.group()) if match else None

def extract_offers(html):
    soup = BeautifulSoup(html, 'html.parser')
    offers = []

    offer_elements = soup.select('ul.grid li a[href^="/en/relocations/"]')
    print(f"[extract_offers] Ofertas encontradas: {len(offer_elements)}")

    for a in offer_elements:
        href = a.get('href')
        if not href:
            continue

        match = re.search(r'/relocations/(\d+)', href)
        if not match:
            continue

        offer_id = match.group(1)
        full_link = f'https://www.imoova.com{href}'

        h3 = a.find('h3')
        origin = destination = None
        if h3:
            parts = h3.get_text(strip=True).split('→')
            if len(parts) == 2:
                origin, destination = parts[0].strip(), parts[1].strip()

        time_elements = a.find_all('time')
        dates = " - ".join(t.get_text(strip=True) for t in time_elements) if time_elements else None

        # ⬇️ SOLO noches / días / 21+
        night_span = a.find(
            'span',
            string=re.compile(r'(noche|night|día|dias|\d+\+)', re.IGNORECASE)
        )

        nights = parse_nights(night_span.get_text(strip=True)) if night_span else None

        offers.append({
            'id': offer_id,
            'origin': origin,
            'destination': destination,
            'nights': nights,
            'dates': dates,
            'link': full_link
        })

    return offers

# ==========================
# EMAIL
# ==========================
def send_email(new_offers):
    if not new_offers:
        return

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"Nuevas ofertas imoova ({len(new_offers)})"

    body = "Se han detectado nuevas ofertas con más de 3 noches:\n\n"
    for offer in new_offers:
        body += (
            f"- {offer['origin']} → {offer['destination']} "
            f"({offer['nights']} noches) | "
            f"Fechas: {offer['dates']} | "
            f"Link: {offer['link']}\n"
        )

    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
        print("Correo enviado correctamente.")
    except Exception as e:
        print(f"Error al enviar el correo: {e}")

# ==========================
# MAIN
# ==========================
def main():
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.binary_location = "/usr/bin/chromium-browser"

    driver = webdriver.Chrome(options=options)
    driver.get(URL)

    # ⛔ SALIR SI NO HAY ANUNCIOS
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'ul.grid li a[href^="/en/relocations/"]')
            )
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

        elements = driver.find_elements(
            By.CSS_SELECTOR, 'ul.grid li a[href^="/en/relocations/"]'
        )
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

    new_offers = [
        o for o in offers
        if o['id'] not in previous_ids and o['nights'] and o['nights'] > 3
    ]

    if new_offers:
        print(f"Nuevas ofertas detectadas: {len(new_offers)}")
        save_offers(previous_offers + new_offers)
        send_email(new_offers)
    else:
        print("No hay nuevas ofertas con más de 3 noches.")

if __name__ == "__main__":
    main()
