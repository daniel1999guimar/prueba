import json
import time
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

JSON_FILE = 'offers.json'
URL = 'https://www.imoova.com/en/relocations?region=EU'

# Configuración correo
smtp_server = 'smtp.gmail.com'
smtp_port = 587
smtp_user = 'danieldelgadospain@gmail.com'
smtp_password = 'ccjd yvwg wnol rzdd'
from_email = smtp_user
to_email = 'danieldelgadospain@gmail.com'

def load_previous():
    try:
        with open(JSON_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_offers(offers):
    with open(JSON_FILE, 'w') as f:
        json.dump(offers, f, indent=2)

def parse_nights(text):
    # Extrae el número de noches, soportando formato "7 + 3 nights"
    match = re.findall(r'\d+', text.replace(" ", ""))
    if match:
        total_nights = sum(int(n) for n in match)
        return total_nights
    return None

def extract_offers(html):
    soup = BeautifulSoup(html, 'html.parser')
    offers = []

    offer_elements = soup.select('ul.grid li a[href^="/en/relocations/"]')
    print(f"[extract_offers] Ofertas encontradas: {len(offer_elements)}")

    for a in offer_elements:
        href = a['href']
        full_link = 'https://www.imoova.com' + href
        offer_id_match = re.search(r'/relocations/(\d+)', href)
        if not offer_id_match:
            continue
        offer_id = offer_id_match.group(1)

        # Origen → Destino
        h3 = a.find('h3')
        if h3:
            route_text = h3.get_text(strip=True)
            parts = route_text.split('→')
            origin = parts[0].strip() if len(parts) > 0 else None
            destination = parts[1].strip() if len(parts) > 1 else None
        else:
            origin = destination = None

        # Fechas
        time_elements = a.find_all('time')
        dates = None
        if time_elements:
            dates = " - ".join(t.get_text(strip=True) for t in time_elements)

        # Noches
        night_span = a.find('span', string=re.compile(r'\d.*(night|noche|día|dias|\+)', re.IGNORECASE))
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

def send_email(new_offers):
    if not new_offers:
        return

    msg = MIMEMultipart()
    msg['From'] = from_email
    msg['To'] = to_email
    msg['Subject'] = f"Nuevas ofertas imoova ({len(new_offers)})"

    body = "Se han detectado nuevas ofertas con más de 3 noches:\n\n"
    for offer in new_offers:
        body += f"- {offer['origin']} → {offer['destination']} ({offer['nights']} noches) | Fechas: {offer['dates']} | Link: {offer['link']}\n"

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

def main():
    options = Options()
    options.add_argument('--headless')  # Ejecutar sin interfaz gráfica
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    driver = webdriver.Chrome(options=options)
    driver.get(URL)

    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, 'ul.grid li a[href^="/en/relocations/"]'))
    )

    print("Haciendo scroll por tramos...\n")
    last_count = 0
    same_count_attempts = 0
    max_same_count_attempts = 10

    while same_count_attempts < max_same_count_attempts:
        driver.execute_script("window.scrollBy(0, 1300);")
        time.sleep(2)

        elements = driver.find_elements(By.CSS_SELECTOR, 'ul.grid li a[href^="/en/relocations/"]')
        current_count = len(elements)
        print(f"Anuncios visibles: {current_count}")

        if current_count == last_count:
            same_count_attempts += 1
        else:
            same_count_attempts = 0
            last_count = current_count

    print("\nScroll finalizado. Extrayendo HTML...\n")
    html = driver.page_source
    driver.quit()

    offers = extract_offers(html)
    for o in offers:
        print(f"[DEBUG] {o['origin']} → {o['destination']} | Noches: {o['nights']} | Fechas: {o['dates']}")

    previous_offers = load_previous()
    previous_ids = {offer['id'] for offer in previous_offers}

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
