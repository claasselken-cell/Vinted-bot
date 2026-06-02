import requests

BASE_URL = "https://www.vinted.de"
API_URL = f"{BASE_URL}/api/v2"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "de-DE,de;q=0.9",
    "Referer": "https://www.vinted.de/",
}


def login_with_token(raw: str) -> dict | None:
    """
    Extrahiert access_token_web UND refresh_token_web aus einem Cookie-String.
    Speichert den Token direkt ohne Server-Validierung (DataDome blockiert /users/current).
    Gibt Cookies-Dict zurück oder None wenn kein Token erkennbar.
    """
    raw = raw.strip()
    cookies: dict[str, str] = {}

    for part in raw.split(";"):
        part = part.strip()
        for key in ("access_token_web", "refresh_token_web"):
            if part.startswith(f"{key}="):
                cookies[key] = part.split("=", 1)[1].strip()

    # Fallback: ganzer String ist der bare Token-Wert
    if not cookies and len(raw) > 50 and " " not in raw:
        cookies["access_token_web"] = raw

    if "access_token_web" not in cookies:
        return None

    # Token-Wert muss wie ein JWT aussehen (fängt mit eyJ an)
    token_val = cookies["access_token_web"]
    if not token_val.startswith("eyJ") or len(token_val) < 50:
        return None

    return cookies


def refresh_access_token(cookies: dict) -> dict | None:
    """
    Erneuert den Access Token mit dem Refresh Token.
    Gibt aktualisiertes Cookies-Dict zurück oder None bei Fehler.
    """
    refresh = cookies.get("refresh_token_web")
    if not refresh:
        return None

    session = _build_session(cookies)
    try:
        r = session.post(
            f"{BASE_URL}/oauth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": "web",
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            new_cookies = dict(cookies)
            if data.get("access_token"):
                new_cookies["access_token_web"] = data["access_token"]
            if data.get("refresh_token"):
                new_cookies["refresh_token_web"] = data["refresh_token"]
            return new_cookies
        # Manche Implementierungen geben Cookies im Header zurück
        if r.status_code in (200, 302):
            new_access = r.cookies.get("access_token_web")
            new_refresh = r.cookies.get("refresh_token_web")
            if new_access:
                new_cookies = dict(cookies)
                new_cookies["access_token_web"] = new_access
                if new_refresh:
                    new_cookies["refresh_token_web"] = new_refresh
                return new_cookies
    except Exception as e:
        print(f"[Vinted] Refresh-Fehler: {e}")
    return None


def get_username(cookies: dict) -> str | None:
    session = _build_session(cookies)
    try:
        r = session.get(f"{API_URL}/users/current", timeout=10)
        if r.status_code == 200:
            return r.json().get("user", {}).get("login")
    except Exception:
        pass
    return None


def search(cookies: dict, keyword: str, max_price: float, per_page: int = 20) -> tuple[list, bool]:
    """
    Gibt (items, token_expired) zurück.
    token_expired=True wenn 401 zurückkommt.
    """
    session = _build_session(cookies)
    try:
        r = session.get(
            f"{API_URL}/catalog/items",
            params={"search_text": keyword, "price_to": max_price,
                    "order": "newest_first", "per_page": per_page},
            timeout=15,
        )
        if r.status_code == 401:
            return [], True
        if r.status_code == 200:
            return r.json().get("items", []), False
    except Exception as e:
        print(f"[Vinted] Suche-Fehler: {e}")
    return [], False


def favorite(cookies: dict, item_id: str) -> bool:
    session = _build_session(cookies)
    try:
        r = session.post(f"{API_URL}/items/{item_id}/favourite", timeout=10)
        return r.status_code in (200, 201)
    except Exception:
        return False


def get_user_info(cookies: dict, user_id: int | str) -> dict | None:
    """Holt Nutzerdetails inkl. Bewertung und Standort."""
    session = _build_session(cookies)
    try:
        r = session.get(f"{API_URL}/users/{user_id}", timeout=10)
        if r.status_code == 200:
            return r.json().get("user")
    except Exception as e:
        print(f"[Vinted] User-Fehler: {e}")
    return None


def get_item(cookies: dict, item_id: str) -> dict | None:
    session = _build_session(cookies)
    try:
        r = session.get(f"{API_URL}/items/{item_id}", timeout=10)
        if r.status_code == 200:
            return r.json().get("item")
    except Exception as e:
        print(f"[Vinted] Item-Fehler: {e}")
    return None


def buy_item(cookies: dict, item_id: str, item_url: str = None,
             delivery_info: dict = None) -> tuple[bool, str]:
    """
    Kauft einen Artikel auf Vinted.
    item_url: vollständige URL des Artikels (z.B. https://www.vinted.de/items/123-...)
              wird genutzt um die korrekte Länder-Domain für die API zu ermitteln.
    delivery_info: dict mit delivery_type ('home'|'pickup'), full_name, street,
                   postal_code, city, pickup_name
    """
    from urllib.parse import urlparse

    session = _build_session(cookies)

    # Domain direkt aus der URL nehmen (kein Redirect-Follow — zu langsam)
    api_base = API_URL
    if item_url:
        parsed = urlparse(item_url)
        api_base = f"{parsed.scheme}://{parsed.netloc}/api/v2"

    # Lieferinfo aufbauen
    transaction: dict = {"item_id": int(item_id)}
    if delivery_info:
        if delivery_info.get("delivery_type") == "pickup" and delivery_info.get("pickup_name"):
            transaction["shipment"] = {
                "shipping_order_type": "pickup_point",
                "pickup_point": {
                    "name": delivery_info["pickup_name"],
                    "postal_code": delivery_info.get("postal_code", ""),
                    "city": delivery_info.get("city", ""),
                },
            }
        elif delivery_info.get("street"):
            transaction["shipment"] = {
                "shipping_order_type": "home_delivery",
                "address": {
                    "full_name": delivery_info.get("full_name", ""),
                    "line1": delivery_info.get("street", ""),
                    "postal_code": delivery_info.get("postal_code", ""),
                    "city": delivery_info.get("city", ""),
                    "country_code": "DE",
                },
            }

    try:
        r = session.post(
            f"{api_base}/transactions",
            json={"transaction": transaction},
            timeout=8,
        )
        if r.status_code in (200, 201):
            return True, "Kauf erfolgreich ausgelöst! ✅"

        # Fallback ohne Lieferdetails
        if r.status_code == 404:
            r2 = session.post(
                f"{API_URL}/transactions",
                json={"transaction": {"item_id": int(item_id)}},
                timeout=8,
            )
            if r2.status_code in (200, 201):
                return True, "Kauf erfolgreich ausgelöst! ✅"

        try:
            err = r.json().get("message") or r.json().get("error") or f"Status {r.status_code}"
        except Exception:
            err = f"Status {r.status_code}"
        return False, err
    except Exception as e:
        return False, str(e)


def send_offer(cookies: dict, item_id: str, price: float) -> bool:
    session = _build_session(cookies)
    try:
        r = session.post(
            f"{API_URL}/items/{item_id}/offers",
            json={"offer": {"price": str(price), "currency": "EUR"}},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def _build_session(cookies: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".vinted.de")
    csrf = cookies.get("XSRF-TOKEN") or cookies.get("csrf_token") or ""
    if csrf:
        session.headers["X-CSRF-Token"] = csrf
        session.headers["X-XSRF-TOKEN"] = csrf
    return session
