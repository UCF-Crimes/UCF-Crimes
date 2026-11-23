'''
UCF Crimes: utils.py
Various functions to assist with backend & discord bot functionality.
Written by Ethan Frakes and Maverick Reynolds

'''

import discord
import json
import re
import pandas as pd
import requests
import threading
import time
import editdistance
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta
from functools import lru_cache
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine.base import Engine
from configparser import ConfigParser

async def bot_help(interaction: discord.Interaction):
    '''
    Help command for discord bot with embed showing all possible commands.
    '''
    embed = discord.Embed(
        title = "UCF Crimes Help",
        description = "Available bot commands:\n\n"
            + "/crimes {MM/DD/YY} or {location} or {address} or {crime title} or {disposition} \n"
            + "   - View all crime reports searched with the given parameter. \n"
            + "   - Ex: /crimes 3/30/24 OR /crimes Nike 106 \n\n"
            + "/heatmap {main} or {downtown} or {rosen} \n"
            + "   - View heatmap of all crime reports in database within the area of the selected campus. \n"
            + "   - Ex: /crimes main \n\n"
            + "/locations \n"
            + "   - List all available locations and addresses available to query. \n\n"
            + "/ping \n"
            + "   - Test if bot is online by pinging.",
        color = discord.Color.red()
    )

    await interaction.response.send_message(embed=embed)

@lru_cache(maxsize=None)
def _get_engine(db_uri: str) -> Engine:
    '''
    Create one engine (and therefore one connection pool) per database URI for the life of the
    process. Creating a new engine per call leaves each engine's pooled connections open until
    garbage collection, which slowly exhausts the server's max_connections.
    pool_pre_ping discards pooled connections the server has dropped while the bot sat idle.
    '''
    return create_engine(db_uri, pool_pre_ping=True)

def setup_db(main_config: ConfigParser) -> Engine:
    '''
    Return the shared engine for the PostgreSQL database. Safe to call repeatedly.
    '''
    host = main_config.get('POSTGRESQL', 'host')
    database = main_config.get('POSTGRESQL', 'database')
    user = main_config.get('POSTGRESQL', 'user')
    password = main_config.get('POSTGRESQL', 'password')

    db_uri = f'postgresql://{user}:{password}@{host}/{database}'

    return _get_engine(db_uri)

def is_valid_date(date_string: str) -> bool:
    '''
    Returns if date string token passed is valid.
    '''
    # First is for mm/dd/yy and second is for mm/dd/yyyy
    valid_formats = ['%m/%d/%y', '%m/%d/%Y']
    for date_format in valid_formats:
        try:
            datetime.strptime(date_string, date_format)
            return True
        except ValueError:
            pass
    return False

def is_valid_time_label(time_str: str) -> bool:
    '''
    Returns if time string token passed is valid.
    '''
    try:
        datetime.strptime(time_str, '%H:%M')
        return True
    except ValueError:
        return False

def is_valid_case_id(case_id_str: str) -> bool:
    '''
    Checks if input string is or is not a case id.
    Used in tokenizer to make sure that delimiter is indeed the disposition and not word in crime title.
    '''
    id_patterns = [r'^\d{4}-\d{4}$', r'^\d{4}-[A-Za-z]{3}\d{2}$']
    for pattern in id_patterns:
        if re.match(pattern, case_id_str):
            return True
        
    return False

def titlize(title: str) -> str:
    '''
    Returns title function applied to string with exceptions found in title_exceptions.json
    '''
    with open('title_exceptions.json') as f:
        exceptions: dict = json.load(f)

    new_title = ''
    for token in title.split():
        if token in exceptions.keys():
            new_title += exceptions[token]
        else:
            new_title += token.title()
        new_title += ' '

    return new_title.strip()

def get_emojis(title: str) -> str:
    '''
    Because emojis are fun
    '''
    with open('emojis.json', 'r', encoding="utf-8") as f:
        emojis: dict = json.load(f)

    emojis_suffix = ''

    for emoji_txt in emojis.keys():
        if re.search(emoji_txt, title, re.IGNORECASE):
            # If a match, go through each emoji in the value and add it to the suffix
            for emoji in emojis[emoji_txt]:
                if emoji not in emojis_suffix:
                    # Must use += and not .join() to preserve unicode
                    emojis_suffix += f'{emoji}'

    return emojis_suffix

# Street-type words that mark a token as a real street (vs. "PARKING LOT", "DRIVEWAY", "HAM/PINE").
_STREET_SUFFIXES = {
    'ROAD', 'RD', 'DRIVE', 'DR', 'STREET', 'ST', 'AVENUE', 'AVE', 'BOULEVARD', 'BLVD', 'TRAIL', 'TRL',
    'PARKWAY', 'PKWY', 'HIGHWAY', 'HWY', 'CIRCLE', 'CIR', 'COURT', 'CT', 'LANE', 'LN', 'WAY', 'PLACE',
    'PL', 'TERRACE', 'TER', 'LOOP', 'PATH', 'PIKE', 'ROW', 'RUN', 'SQUARE', 'SQ', 'PLAZA', 'PLZ',
    'POINT', 'PT', 'COVE', 'CV', 'PASS', 'XING', 'CROSSING', 'BEND', 'ALY', 'EXPY', 'TPKE', 'TURNPIKE',
}
_ROUTE_RE = re.compile(r'^(SR|US|CR|I)[- ]?(\d+)$')  # SR408, US 441, I-4

def _clean_street(part: str) -> str | None:
    '''Normalize one street from a call location, or None if it isn't a geocodable street.'''
    part = part.strip()
    if route := _ROUTE_RE.match(part):
        return f"{route.group(1)} {route.group(2)}"
    if any(token in _STREET_SUFFIXES for token in part.split()):
        return part
    return None

# Locality appended to every query. Without it Nominatim matches the same street name anywhere in
# the viewbox, e.g. OPD's "700 N John Young Pkwy" landed in Kissimmee.
_LOCALITY = {"orange": "Orange County, FL", "orlando": "Orlando, FL"}

def format_call_location(location: str | None, policedpt: str) -> list[str]:
    '''
    Turn an OCSO ("orange") or OPD ("orlando") call location into Nominatim queries, most precise
    first. Empty if the location isn't geocodable.

      "[1800 - 1999] ROUSE ROAD"        -> ["1800 Rouse Road, Orange County, FL"]
      "[UNK] N ALAFAYA TRL"             -> ["N Alafaya Trl, Orange County, FL"]
      "E COLONIAL DRIVE/ROUSE ROAD"     -> ["E Colonial Drive & Rouse Road, ...", "Rouse Road, ...", "E Colonial Drive, ..."]
      "4500 BLOCK WATCH HILL RD"        -> ["4500 Watch Hill Rd, Orlando, FL"]
      "W ROBINSON ST / N GARLAND AVE"   -> ["W Robinson St & N Garland Ave, Orlando, FL", ...]
      "PARKING LOT", "[UNK] DRIVEWAY", "NOT AVAILABLE" -> []

    [UNK] addresses and single-street intersection fallbacks resolve to a point somewhere on that
    street, so they are approximate.
    '''
    if not isinstance(location, str) or not location.strip():
        return []

    loc = location.upper()
    loc = re.sub(r'\[\s*UNK\s*\]', '', loc)                     # OCSO: unknown house number
    loc = re.sub(r'\[\s*(\d+)\s*-\s*\d+\s*\]', r'\1', loc)      # OCSO: block range -> start of block
    loc = re.sub(r'\bBLOCK\b', '', loc)                           # OPD: "4500 BLOCK WATCH HILL RD"
    loc = re.sub(r'\s+', ' ', loc).strip()

    parts = [p for p in re.split(r'\s*/\s*', loc) if p]
    streets = [s for s in (_clean_street(p) for p in parts) if s]
    if not streets:
        return []

    queries = []
    if len(parts) >= 2 and len(streets) >= 2:
        queries.append(f"{streets[0]} & {streets[1]}")
    # Nominatim rarely resolves intersections, so fall back to single streets, cross street first:
    # the main road is usually listed first, and a single long road (W Colonial Dr) geocodes to one
    # fixed point miles away, stacking every call on it into a false hot spot. A short cross street's
    # point lands much closer (e.g. Clifton St 0.2 km vs W Colonial Dr 3.8 km from the intersection).
    queries += streets[::-1]

    locality = _LOCALITY.get(policedpt, "FL")
    return [f"{titlize(q)}, {locality}" for q in queries]

def osm_geocoder(address: str, OSM_USER_AGENT: str, OSM_REFERER: str, raise_on_error: bool = False) -> tuple[float, float] | tuple[None, None]:
    '''
    Uses OpenStreetMap’s Nominatim geocoder to get latitude and longitude from an address.
    It prefers locations within a bounding box (roughly covering UCF and Downtown).
    If the address includes predetermined keyword(s), it returns preset coordinates.
    With raise_on_error, an HTTP error raises instead of returning (None, None), so callers can
    tell "no such address" apart from "Nominatim failed".
    '''

    BL_BOUND = (28.0, -82.0)  # (lat, lon) bottom-left
    TR_BOUND = (29.0, -80.9)  # (lat, lon) top-right
    KEY_PHRASES = {
        'B8': (28.5939606, -81.2014182),
        '36 PINE ST W': (28.5412345, -81.3797360),
        'ON CAMPUS': (28.6024367, -81.2000568),
        'PLAZA DR E': (28.6069698, -81.1967868),
        'PLAZA DR W': (28.6074074, -81.1980356),
        'GEMINI/SCORPIUS': (28.6018854, -81.1944728),
        'KINGS KNIGHT': (28.6104158, -81.2154757),
        'KROSSING': (28.6113891, -81.2113697),
        '410 TERRY AVE N': (28.537944, -81.386917)
    }

    # Return preset coordinates if a key phrase is found in the address
    for phrase in KEY_PHRASES:
        if phrase in address:
            return KEY_PHRASES[phrase]

    # Nominatim API endpoint
    endpoint = 'https://nominatim.openstreetmap.org/search'
    
    # Construct the viewbox.
    # Nominatim expects the viewbox as: left (min lon), top (max lat), right (max lon), bottom (min lat)
    viewbox = f"{BL_BOUND[1]},{TR_BOUND[0]},{TR_BOUND[1]},{BL_BOUND[0]}"

    params = {
        'q': address,
        'format': 'json',
        'limit': 1,
        'viewbox': viewbox,
        'bounded': 1
    }
    
    # Nominatim requires a valid User-Agent header
    headers = {
        'User-Agent': OSM_USER_AGENT,
        'Referer': OSM_REFERER
    }

    response = requests.get(endpoint, params=params, headers=headers, timeout=15)

    if response.status_code not in range(200, 299):
        if raise_on_error:
            response.raise_for_status()
            raise requests.HTTPError(f"Nominatim returned {response.status_code}", response=response)
        return None, None

    data = response.json()
    if not data:
        return None, None

    try:
        lat = float(data[0]['lat'])
        lon = float(data[0]['lon'])
    except (KeyError, ValueError):
        return None, None

    return round(lat, 7), round(lon, 7)

# ---- Persistent geocode cache ------------------------------------------------------------------
# Hourly maps geocode the same streets every hour, and Nominatim allows ~1 request/second, so
# results are kept in the Postgres geocode_cache table (created by an admin; the bot role only has
# SELECT/INSERT/UPDATE/DELETE on it). The orlando_orange.py loader fills it every 10 minutes as
# calls come in (prewarm_call_geocodes), so the hourly report usually needs no Nominatim requests.
#
#   CREATE TABLE geocode_cache (query VARCHAR PRIMARY KEY, lat DOUBLE PRECISION, lng DOUBLE PRECISION,
#                               updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

NOMINATIM_DELAY = 1.1          # Nominatim usage policy: at most 1 request per second
GEOCODE_MISS_RETRY_DAYS = 14   # re-ask Nominatim about misses occasionally; OSM data improves

NOMINATIM_BACKOFF = 15 * 60    # after a rate-limit response, skip Nominatim this long
_NOMINATIM_LIMITED = {429, 503, 509, 629}  # 629 is Nominatim's "bandwidth limit exceeded"

_nominatim_lock = threading.Lock()
_nominatim_last = 0.0
_nominatim_blocked_until = 0.0

def _cache_read(engine: Engine, queries: list[str]) -> dict:
    '''Cached results for these queries (misses older than GEOCODE_MISS_RETRY_DAYS left out).'''
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT query, lat, lng FROM geocode_cache
                WHERE query = ANY(:queries)
                  AND (lat IS NOT NULL OR updated_at > now() - make_interval(days => :retry_days))
            """), {"queries": list(queries), "retry_days": GEOCODE_MISS_RETRY_DAYS}).all()
        return {row.query: (row.lat, row.lng) for row in rows}
    except SQLAlchemyError as e:
        print(f"Geocode cache read failed: {type(e).__name__}: {e}")
        return {}

def _cache_write(engine: Engine, query: str, lat, lng) -> None:
    try:
        with engine.connect() as connection:
            connection.execute(text("""
                INSERT INTO geocode_cache (query, lat, lng) VALUES (:query, :lat, :lng)
                ON CONFLICT (query) DO UPDATE SET lat = EXCLUDED.lat, lng = EXCLUDED.lng, updated_at = now()
            """), {"query": query, "lat": lat, "lng": lng})
            connection.commit()
    except SQLAlchemyError as e:
        print(f"Geocode cache write failed: {type(e).__name__}: {e}")

def _throttled_osm_geocoder(query: str, OSM_USER_AGENT: str, OSM_REFERER: str):
    '''
    osm_geocoder, spaced NOMINATIM_DELAY apart across all threads in this process. After a
    rate-limit response it stops asking for NOMINATIM_BACKOFF seconds (callers use the cache only),
    since retrying while blocked just prolongs the block.
    '''
    global _nominatim_last, _nominatim_blocked_until
    with _nominatim_lock:
        if time.monotonic() < _nominatim_blocked_until:
            raise RuntimeError("Nominatim rate limited; skipping lookups for now")
        wait = _nominatim_last + NOMINATIM_DELAY - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return osm_geocoder(query, OSM_USER_AGENT, OSM_REFERER, raise_on_error=True)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code in _NOMINATIM_LIMITED:
                _nominatim_blocked_until = time.monotonic() + NOMINATIM_BACKOFF
                print(f"Nominatim rate limited us (HTTP {e.response.status_code}); "
                      f"using cached geocodes only for {NOMINATIM_BACKOFF // 60} minutes.")
            raise
        finally:
            _nominatim_last = time.monotonic()

def geocode_cached(engine: Engine, queries: list[str], OSM_USER_AGENT: str, OSM_REFERER: str,
                   allow_network: bool = True) -> tuple[float, float] | tuple[None, None]:
    '''
    Coordinates for the first query in `queries` that geocodes (see format_call_location), only
    asking Nominatim about queries not in the cache. Blocking: call from a thread in async code.
    Nominatim failures are logged and not cached, so they are retried next time. If the cache file
    can't be reached it falls back to Nominatim alone.
    '''
    if not queries:
        return None, None

    cached = _cache_read(engine, queries)
    for query in queries:
        if query not in cached:
            if not allow_network:
                continue
            try:
                lat, lng = _throttled_osm_geocoder(query, OSM_USER_AGENT, OSM_REFERER)
            except Exception as e:
                print(f"Geocoding failed for {query!r}: {type(e).__name__}: {e}")
                continue
            _cache_write(engine, query, lat, lng)
            cached[query] = (lat, lng)

        lat, lng = cached.get(query, (None, None))
        if lat is not None and lng is not None:
            return lat, lng

    return None, None

def prewarm_call_geocodes(engine: Engine, OSM_USER_AGENT: str, OSM_REFERER: str, time_budget: float = 240) -> None:
    '''
    Geocode the current and previous hour's OPD and OCSO call locations into geocode_cache, so the
    hourly heatmap finds them cached. Stops after time_budget seconds; the rest is picked up on the
    next run (or by the heatmap itself).
    '''
    now = datetime.now()
    hours = [(now - timedelta(hours=h)).strftime("%-m/%d/%Y %H") + "%" for h in (0, 1)]
    deadline = time.monotonic() + time_budget
    looked_up = located = 0

    for policedpt, key in (("orlando", "date"), ("orange", "entrytime")):
        with engine.connect() as connection:
            locations = connection.execute(
                text(f"SELECT DISTINCT location FROM {policedpt}_crimes WHERE {key} LIKE :h0 OR {key} LIKE :h1"),
                {"h0": hours[0], "h1": hours[1]},
            ).scalars().all()

        for location in locations:
            if time.monotonic() > deadline:
                print(f"Geocode prewarm hit its {time_budget:.0f}s budget; continuing next run.")
                return
            queries = format_call_location(location, policedpt)
            if queries:
                looked_up += 1
                lat, _ = geocode_cached(engine, queries, OSM_USER_AGENT, OSM_REFERER)
                located += lat is not None

    print(f"Geocode prewarm: {located}/{looked_up} recent call locations located.")

def google_geocoder(address: str, google_maps_api_key: str) -> tuple[float, float] | tuple[None, None]:
    '''
    Uses Google geocoding endpoint to get lat, lng from address.
    Will prefer locations within box containing UCF and Downtown
    (Does not totally restrict results to this box).
    If address includes predetermined keyword(s), it will use given results
    '''

    BL_BOUND = (28.522318, -81.407249)
    TR_BOUND = (28.644500, -81.155722)
    KEY_PHRASES = {
        'B8': (28.5939606, -81.2014182),
        '36 PINE ST W': (28.5412345, -81.3797360),
        'ON CAMPUS': (28.6024367, -81.2000568),
        'PLAZA DR E':(28.6069698, -81.1967868),
        'PLAZA DR W': (28.6074074, -81.1980356),
        'GEMINI/SCORPIUS': (28.6018854, -81.1944728),
        'KINGS KNIGHT': (28.6104158, -81.2154757),
        'KROSSING': (28.6113891, -81.2113697),
        '410 TERRY AVE N': (28.537944, -81.386917)
    }

    endpoint = 'https://maps.googleapis.com/maps/api/geocode/json'
    params = {
        'address': address,
        'key': google_maps_api_key,
        'bounds': f'{BL_BOUND[0]},{BL_BOUND[1]}|{TR_BOUND[0]},{TR_BOUND[1]}'
    }

    # Use key phrases if they are in address
    for phrase in KEY_PHRASES.keys():
        if phrase in address:
            return KEY_PHRASES[phrase]

    try:
        result = requests.get(endpoint, params=params, timeout=30)
        if result.status_code not in range(200, 299):
            print(f"Geocoding failed for {address!r}: HTTP {result.status_code}")
            return None, None

        data = result.json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        if data.get('status') != 'OK':
            print(f"Geocoding failed for {address!r}: "
                  f"{data.get('status')} - {data.get('error_message', 'No error message provided')}")
            return None, None

        location = data['results'][0]['geometry']['location']
        return round(location['lat'], 7), round(location['lng'], 7)
    except requests.exceptions.RequestException as error:
        # Request exception messages can include the URL and API key.
        print(f"Geocoding request failed for {address!r}: {type(error).__name__}")
    except (IndexError, KeyError, TypeError, ValueError) as error:
        print(f"Invalid geocoding response for {address!r}: {error}")

    return None, None

def haversine_form(latitude_center: float, longitude_center: float, latitude_place: float, longitude_place: float) -> float:
    lat1 = radians(latitude_center)
    lon1 = radians(longitude_center)
    lat2 = radians(latitude_place)
    lon2 = radians(longitude_place)

    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat / 2)**2 + cos(lat1) * cos(lat2) * sin(dlon / 2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    distance = 6371 * c  # Radius of the Earth in kilometers

    return distance

def get_place_name(lat: float, lng: float, GMAPS_API_KEY: str, radius: int=100) -> str | None:
    '''
    Sends request to Google Places API for all place markers within 100 meters
    of address coordinates; finds closest that is not a road or transit station.
    '''
    
    url = f'https://maps.googleapis.com/maps/api/place/nearbysearch/json?location={lat},{lng}&radius={radius}&key={GMAPS_API_KEY}'

    r = requests.get(url)
    # print(r.text)
    json_r = json.loads(r.text)

    places_dict = {}

    for result in json_r["results"]:
        if "route" not in result["types"] and "transit_station" not in result["types"]:
            lat_place = result["geometry"]["location"]["lat"]
            lon_place = result["geometry"]["location"]["lng"]

            distance = haversine_form(lat, lng, lat_place, lon_place)
            # print(f"Distance for {result['name']} = {distance}")

            places_dict[distance] = result['name']

    if len(places_dict.keys()) > 0:
        return places_dict[min(places_dict.keys())]
    else:
        return None

def change_all_addresses(GMAPS_API_KEY: str) -> None:
    crimes = pd.read_csv('crimes.csv', index_col=0)

    for idx, row in crimes.iterrows():
        if row["address"] == row["place"].upper():
            lat, lng = osm_geocoder(row["address"], GMAPS_API_KEY)
            if place_name := get_place_name(lat, lng, GMAPS_API_KEY):
                crimes.at[idx, "place"] = f"near {place_name}"
                print(place_name)

    crimes.to_csv('crimes.csv')

def address_to_place(address: str, 
                     lat: float, lng: float, 
                     GMAPS_API_KEY: str, 
                     typo_tolerance=1) -> str | None:
    '''
    Takes address and compares it to the locations.json file
    Robust against varying word positions and typo errors
    If selenium scraping is enabled and a path is given, the function will use that as a backup
    Otherwise, returns the titled version of the address
    '''

    # Make proper substitutions to change cardinal directions and other syntax
    address = ' ' + address + ' '
    address = re.sub(r'\.', '', address)   # Remove periods

    subs = {
        ' W ': ' WEST ',
        ' E ': ' EAST ',
        ' N ': ' NORTH ',
        ' S ': ' SOUTH ',
        'BLD' : 'BLVD',
        r' ?(?: and |\/) ?': ' & '     # Change intersection of streets to &
    }
    for key, value in subs.items():
        address = re.sub(key, value, address, flags=re.IGNORECASE)
    address = address.strip()

    # Tokenize address
    txt_tokens = address.split()

    # Start by looking through the address
    with open('locations.json') as f:
        locations: dict[str] = json.load(f)

    # Do this with every key
    for key in locations.keys():
        key_tokens = key.split()
        is_match = True

        # If numerical start, make sure it matches exactly
        if re.match(r'\d', txt_tokens[0]) and re.match(r'\d', key_tokens[0]) and txt_tokens[0] != key_tokens[0]:
            is_match = False
            continue

        # Go through each token in key
        for key_token in key_tokens:
            token_distances = [editdistance.eval(key_token, txt_token) for txt_token in txt_tokens]
            # Fail if no token is within tokerance for all key tokens
            if min(token_distances) > typo_tolerance:
                is_match = False
                break

        # If all tokens are within tolerance, locations is found
        if is_match:
            return locations[key]
    
    if lat is not None and lng is not None and (place_name := get_place_name(lat, lng, GMAPS_API_KEY)):
            print(place_name)
            return f"near {place_name}"
    
    return titlize(address)
