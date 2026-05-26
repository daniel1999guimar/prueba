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


# LISTA NEGRA
CIUDADES_PROHIBIDAS = [
    "LONDON",
    "DUBLIN",
    "EDINBURGH",
    "MANCHESTER",
    "BRISTOL",
    "STOCKHOLM",
    "BELFAST",
    "CORK",
    "INVERNESS"
]


smtp_server = 'smtp.gmail.com'
smtp_port = 587

smtp_user = os.environ.get('SMTP_USER')
smtp_password = os.environ.get('SMTP_PASSWORD')

from_email = smtp_user
to_email = os.environ.get('SMTP_TO', smtp_user)


if not smtp_user or not smtp_password:
    raise ValueError(
        "Faltan variables de entorno SMTP_USER o SMTP_PASSWORD"
    )


def load_previous():

    try:

        with open(JSON_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_offers(offers):

    with open(JSON_FILE, 'w', encoding='utf-8') as f:
        json.dump(
            offers,
            f,
            indent=2,
            ensure_ascii=False
        )


def extract_offer_id(href):

    match = re.search(
        r'(RLC\d+)',
        href,
        re.IGNORECASE
    )

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
        parts = re.split(
            r'\s+to\s+',
            text,
            maxsplit=1,
            flags=re.IGNORECASE
        )

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

    print(
        f"[extract_offers] "
        f"Ofertas encontradas: {len(offer_elements)}"
    )

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

    try:

        driver.get(url)

        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located(
                (By.XPATH, "//*[contains(text(), 'km')]")
            )
        )

        soup = BeautifulSoup(driver.page_source, 'lxml')

        paragraphs = soup.find_all(
            'p',
            class_=lambda c: c and 'text-sm' in c
        )

        for p in paragraphs:

            text = p.get_text(
                strip=True
            ).lower()

            match = re.search(
                r'(\d+)\s*km',
                text
            )

            if match:
                return int(match.group(1))

    except Exception as e:

        print(
            f"[Error] "
            f"No se pudieron extraer km de {url}: {e}"
        )

    return None


def send_email(new_offers):

    if not new_offers:
        return

    msg = MIMEMultipart("alternative")

    msg['From'] = from_email
    msg['To'] = to_email

    msg['Subject'] = (
        f"Imoova • "
        f"{len(new_offers)} nuevas ofertas"
    )

    html = f"""
    <html>

    <head>

        <style>

            body {{
                font-family: Arial, sans-serif;
                background: #f7f7f7;
                margin: 0;
                padding: 20px;
                color: #111;
            }}

            .container {{
                max-width: 700px;
                margin: auto;
            }}

            .header {{
                font-size: 22px;
                font-weight: bold;
                margin-bottom: 6px;
            }}

            .subtitle {{
                color: #666;
                font-size: 14px;
                margin-bottom: 20px;
            }}

            .offer {{
                background: white;
                border-radius: 10px;
                padding: 14px 16px;
                margin-bottom: 8px;
                border: 1px solid #ececec;
            }}

            .route {{
                font-size: 17px;
                font-weight: 600;
                margin-bottom: 5px;
            }}

            .meta {{
                font-size: 13px;
                color: #666;
                line-height: 1.4;
            }}

            .km {{
                color: #111;
                font-weight: 600;
            }}

            .link {{
                display: inline-block;
                margin-top: 7px;
                color: #1565c0;
                text-decoration: none;
                font-size: 13px;
                font-weight: 600;
            }}

            .footer {{
                margin-top: 18px;
                color: #999;
                font-size: 12px;
                text-align: center;
            }}

        </style>

    </head>

    <body>

        <div class="container">

            <div class="header">
                🚐 Nuevas ofertas Imoova
            </div>

            <div class="subtitle">
                {len(new_offers)} nuevas rutas detectadas
            </div>
    """

    for offer in new_offers:

        km = offer.get('distance_km')

        if km:
            km_text = f"{km} km"
        else:
            km_text = "Sin km"

        nights = offer.get('nights') or "?"
        dates = offer.get('dates') or "Sin fechas"

        html += f"""

            <div class="offer">

                <div class="route">
                    {offer['origin']} → {offer['destination']}
                </div>

                <div class="meta">
                    <span class="km">📏 {km_text}</span>
                    · 🌙 {nights} noches
                    · 📅 {dates}
                </div>

                <a
                    href="{offer['link']}"
                    class="link"
                >
                    Ver oferta →
                </a>

            </div>
        """

    html += """

            <div class="footer">
                Scraper Imoova
            </div>

        </div>

    </body>

    </html>
    """

    msg.attach(
        MIMEText(
            html,
            'html',
            'utf-8'
        )
    )

    try:

        server = smtplib.SMTP(
            smtp_server,
            smtp_port
        )

        server.starttls()

        server.login(
            smtp_user,
            smtp_password
        )

        server.send_message(msg)

        server.quit()

        print("Correo enviado correctamente.")

    except Exception as e:

        print(
            f"Error al enviar correo: {e}"
        )


def main():

    options = Options()

    options.add_argument('--headless=new')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    if os.path.exists("/usr/bin/chromium-browser"):
        options.binary_location = (
            "/usr/bin/chromium-browser"
        )

    driver = webdriver.Chrome(
        options=options
    )

    driver.get(URL)

    try:

        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (
                    By.CSS_SELECTOR,
                    OFFER_SELECTOR
                )
            )
        )

    except TimeoutException:

        print(
            "No hay anuncios disponibles."
        )

        driver.quit()

        return

    print("Haciendo scroll por tramos...")

    last_count = 0
    same_count_attempts = 0

    while same_count_attempts < 10:

        driver.execute_script(
            "window.scrollBy(0, 1300);"
        )

        time.sleep(2)

        elements = driver.find_elements(
            By.CSS_SELECTOR,
            OFFER_SELECTOR
        )

        current_count = len(elements)

        print(
            f"Anuncios visibles: "
            f"{current_count}"
        )

        if current_count == last_count:
            same_count_attempts += 1
        else:
            same_count_attempts = 0
            last_count = current_count

    html = driver.page_source

    offers = extract_offers(html)

    if not offers:

        print(
            "No se han encontrado ofertas."
        )

        driver.quit()

        return

    previous_offers = load_previous()

    previous_ids = {
        o['id']
        for o in previous_offers
    }

    potential_new_offers = [

        o for o in offers

        if o['id'] not in previous_ids
        and o['nights'] is not None
        and o['nights'] > 3
    ]

    new_offers = []

    for offer in potential_new_offers:

        orig_upper = (
            offer['origin'].upper()
            if offer['origin']
            else ""
        )

        dest_upper = (
            offer['destination'].upper()
            if offer['destination']
            else ""
        )

        # FILTRO LISTA NEGRA
        if any(
            ciudad in orig_upper
            for ciudad in CIUDADES_PROHIBIDAS
        ) or any(
            ciudad in dest_upper
            for ciudad in CIUDADES_PROHIBIDAS
        ):

            print(
                f"[Lista Negra] Saltando: "
                f"{offer['origin']} "
                f"→ "
                f"{offer['destination']}"
            )

            continue

        print(
            f"Abriendo detalle: "
            f"{offer['id']} "
            f"({offer['origin']} "
            f"-> "
            f"{offer['destination']})"
        )

        km = extract_km_from_deal(
            driver,
            offer['link']
        )

        offer['distance_km'] = km

        new_offers.append(offer)

        time.sleep(1)

    # PRIMERO SIN KM, LUEGO ORDENADOS POR KM
    new_offers.sort(
        key=lambda x: (
            x.get('distance_km') is not None,
            x.get('distance_km', 999999)
        )
    )

    driver.quit()

    if new_offers:

        print(
            f"Nuevas ofertas detectadas: "
            f"{len(new_offers)}"
        )

        save_offers(
            previous_offers + new_offers
        )

        send_email(new_offers)

    else:

        print(
            "No hay nuevas ofertas "
            "que cumplan los requisitos."
        )


if __name__ == "__main__":
    main()
