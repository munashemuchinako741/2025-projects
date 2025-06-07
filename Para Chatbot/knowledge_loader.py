
import pandas as pd
import requests
from io import BytesIO
import gspread
from bs4 import BeautifulSoup
from oauth2client.service_account import ServiceAccountCredentials

def load_csv(filepath):
    return pd.read_csv(filepath)

def load_excel(filepath):
    return pd.read_excel(filepath)

def load_google_sheet(sheet_url, creds_json_path):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_json_path, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_url(sheet_url).sheet1
    data = sheet.get_all_records()
    return pd.DataFrame(data)

def scrape_website(url):
    response = requests.get(url)
    if response.status_code != 200:
        raise Exception(f"Failed to fetch {url}")
    soup = BeautifulSoup(response.text, 'html.parser')
    paragraphs = soup.find_all('p')
    text = "\n".join([p.get_text() for p in paragraphs])
    return text
