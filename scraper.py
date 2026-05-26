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


def extract_distance(a):
    """Extrae la distancia en km o millas y la devuelve siempre como un entero (Km aproximados)."""
    text = a.get_text(" ", strip=True).lower()
    
    # Busca patrones tipo "1500 km" o "1500km"
    km_match = re.search(r'([\d.,]+)\s*km', text)
    if km_match:
        # Quitamos puntos o comas de millares si los hay
        num_str = km_match.group(1).replace('.', '').replace(',', '')
        return int(num_str)
        
    # Si viene en millas, lo convertimos a km de forma aproximada (* 1.6)
    miles_match = re.search(r'([\d.,]+)\s*(miles|mi|millas)', text)
    if miles_match:
        num_str = miles_match.group(1).replace('.', '').replace(',', '')
        return int(int(num_str) * 1.6)
        
    return 999999  # Valor por defecto muy alto si no encuentra la distancia para no romper el orden


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
        distance = extract_distance(a)

        offers.append({
            'id': offer_id,
            'origin': origin,
            'destination': destination,
            'nights': nights,
            'distance': distance,
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
    msg['Subject'] = f"🔔 Nuevas ofertas Imoova ({len(new_offers)}) - Ordenadas por Km"

    # Construcción de la plantilla HTML con diseño limpio y responsive
    html_body = f"""
    <html>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #333333; background-color: #f4f6f8; margin: 0; padding: 20px;">
        <div style="max-width: 700px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 10px rgba(0,0,0,0.05); border: 1px solid #e1e4e8;">
            
            <!-- Encabezado -->
            <div style="background-color: #1a73e8; padding: 24px; text-align: center;">
                <h2 style="color: #ffffff; margin: 0; font-size: 22px; font-weight: 600; letter-spacing: 0.5px;">
                    Nuevas Ofertas Detectadas (+3 noches)
                </h2>
                <p style="color: #e8f0fe; margin: 6px 0 0 0; font-size: 14px;">
                    Filtradas automáticamente y ordenadas de menor a mayor distancia.
                </p>
            </div>
            
            <!-- Contenido -->
            <div style="padding: 24px;">
                <p style="font-size: 15px; line-height: 1.5; color: #5f6368; margin-top: 0;">
                    Se han encontrado los siguientes trayectos disponibles que cumplen con tus criterios:
                </p>
                
                <div style="overflow-x: auto;">
                    <table style="width: 100%; border-collapse: collapse; margin-top: 16px; min-width: 500px;">
                        <thead>
                            <tr style="background-color: #f8f9fa; border-bottom: 2px solid #e1e4e8;">
                                <th style="text-align: left; padding: 12px 8px; font-size: 13px; font-weight: 600; color: #202124;">Ruta</th>
                                <th style="text-align: center; padding: 12px 8px; font-size: 13px; font-weight: 600; color: #202124;">Distancia</th>
                                <th style="text-align: center; padding: 12px 8px; font-size: 13px; font-weight: 600; color: #202124;">Noches</th>
                                <th style="text-align: left; padding: 12px 8px; font-size: 13px; font-weight: 600; color: #202124;">Fechas Disponibles</th>
                                <th style="text-align: center; padding: 12px 8px; font-size: 13px; font-weight: 600; color: #202124;">Acción</th>
                            </tr>
                        </thead>
                        <tbody>
    """

    for offer in new_offers:
        dist_display = f"{offer['distance']} km" if offer['distance'] != 999999 else "N/D"
        
        html_body += f"""
                            <tr style="border-bottom: 1px solid #f1f3f4; font-size: 14px;">
                                <td style="padding: 14px 8px; font-weight: 500; color: #1a73e8;">
                                    {offer['origin']} <span style="color: #9aa0a6;">➔</span> {offer['destination']}
                                </td>
                                <td style="padding: 14px 8px; text-align: center; color: #3c4043; font-weight: bold;">
                                    {dist_display}
                                </td>
                                <td style="padding: 14px 8px; text-align: center; color: #3c4043;">
                                    <span style="background-color: #e6f4ea; color: #137333; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600;">
                                        {offer['nights']} noches
                                    </span>
                                </td>
                                <td style="padding: 14px 8px; color: #5f6368; white-space: nowrap;">
                                    {offer['dates'] if offer['dates'] else 'No especificadas'}
                                </td>
                                <td style="padding: 14px 8px; text-align: center;">
                                    <a href="{offer['link']}" style="background-color: #1a73e8; color: #ffffff; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-size: 12px; font-weight: 500; display: inline-block;">
                                        Ver Oferta
                                    </a>
                                </td>
                            </tr>
        """

    html_body += """
                        </tbody>
                    </table>
                </div>
            </div>
            
            <!-- Pie de página -->
            <div style="background-color: #f8f9fa; padding: 16px; text-align: center; border-top: 1px solid #e1e4e8;">
                <p style="margin: 0; font-size: 12px; color: #9aa0a6;">
                    Este es un aviso automático generado por tu scraper de Imoova.
                </p>
            </div>
        </div>
    </body>
    </html>
    """

    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

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

    # Filtrar y ordenar todas las ofertas locales que tengan más de 3 noches por distancia
    valid_offers = [o for o in offers if o['nights'] is not None and o['nights'] > 3]
    valid_offers.sort(key=lambda x: x['distance'])

    print("Ofertas con mas de 3 noches encontradas (Ordenadas por menor Km):")
    for offer in valid_offers:
        print(f"{offer['origin']} -> {offer['destination']} ({offer['distance']} km, {offer['nights']} noches)")

    previous_offers = load_previous()
    previous_ids = {o['id'] for o in previous_offers}

    # Filtrar solo las nuevas, manteniendo el orden por distancia
    new_offers = [o for o in valid_offers if o['id'] not in previous_ids]

    if new_offers:
        print(f"Nuevas ofertas detectadas: {len(new_offers)}")
        # Guardamos en el JSON combinando anteriores y nuevas
        save_offers(previous_offers + new_offers)
        # Enviamos el correo con las nuevas ofertas ordenadas por Km
        send_email(new_offers)
    else:
        print("No hay nuevas ofertas con mas de 3 noches.")


if __name__ == "__main__":
    main()
