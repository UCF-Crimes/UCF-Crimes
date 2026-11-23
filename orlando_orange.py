'''

UCF Crimes: orlando_orange.py

An extension to UCF Crimes that adds all Orlando PD & OSCO active calls to separate database 
tables, will be used for querying calls and displaying active call report each hour.

Written by Ethan Frakes

'''

import requests
from bs4 import BeautifulSoup
from utils import setup_db, prewarm_call_geocodes
import xmltodict
import pandas as pd
import json
from time import sleep
from datetime import datetime
from configparser import ConfigParser
from sqlalchemy import text
from sqlalchemy.engine.base import Engine

def load_orlando_active(engine: Engine) -> None:
    '''
    Load XML file from Orlando PD website with BeautifulSoup and parse the file to SQL table.
    '''
    
    connection = engine.connect()

    active_req = requests.get('https://www1.cityoforlando.net/opd/activecalls/activecadpolice.xml')
    soup = BeautifulSoup(active_req.content, "lxml-xml")
    active_dict = xmltodict.parse(str(soup))
        
    for crime in active_dict["CALLS"]["CALL"]:
        try:
            print(crime)
            query = f"""SELECT * FROM orlando_crimes WHERE incident = '{crime["@incident"]}'"""
            result = connection.execute(text(query))

            if result.rowcount == 0:
                query = f"""INSERT INTO orlando_crimes (incident, date, description, location, district)""" \
                        f"""VALUES ('{crime['@incident']}', '{crime['DATE']}', '{crime['DESC']}', '{crime['LOCATION'].replace("'", "''")}', '{crime['DISTRICT']}')"""

            else:
                query = f"""UPDATE orlando_crimes SET date = '{crime['DATE']}', description = '{crime['DESC']}', """ \
                        f"""location = '{crime['LOCATION'].replace("'", "''")}', district = '{crime['DISTRICT']}' WHERE incident = '{crime['@incident']}'"""
                
            connection.execute(text(query))
            connection.commit()

        except Exception as e:
            print(f"Exception occurred: {type(e).__name__}: {str(e)}")
            # traceback.print_exc()

    query = f"SELECT count(*) AS exact_count FROM orlando_crimes"
    result = connection.execute(text(query)).fetchone()
    print("Orlando crimes database updated.")
    print(f"Current number of entries in database: {result[0]}")
    connection.close()

OCSO_CALLS_URL = "https://www.ocso.com/wp-admin/admin-ajax.php?action=get_active_calls"

def load_orange_active(engine: Engine) -> None:
    '''
    Load OCSO active calls and upsert them into orange_crimes.

    https://www.ocso.com/calls-for-service/ is an HTML page, but its table is built client-side by
    publiccfs.js from this XML feed (the old CFS_FEED/activecalls.xml, moved behind admin-ajax).
    Reading the feed directly avoids scraping the paginated table.

    Feed entry format: <CALL INCIDENT="0706"><ENTRYTIME>11/22/26 09:54 pm</ENTRYTIME><DESC>..</DESC>
    <LOCATION>[848 - 899] LEE ROAD</LOCATION><SECTOR>S1  </SECTOR><ZONE>109  </ZONE><RD>OCSO 109</RD></CALL>
    '''

    # Fetch before checking out a DB connection, so a slow or failed request never holds one.
    r = requests.get(OCSO_CALLS_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    feed = xmltodict.parse(r.content, force_list=("CALL",))
    calls = (feed.get("CALLS") or {}).get("CALL", [])

    def clean(value):
        return value.strip() if isinstance(value, str) and value.strip() else None

    with engine.connect() as connection:
        for crime in calls:
            try:
                print(crime)
                dt = datetime.strptime(crime["ENTRYTIME"].strip(), "%m/%d/%y %I:%M %p")
                # Incident numbers reset daily, so prefix with year + day-of-year (same key format
                # as rows from the old CFS_FEED XML, e.g. 2026-027-0515).
                params = {
                    "incident": f"{dt.strftime('%Y-%j')}-{crime['@INCIDENT'].strip()}",
                    # Matches the LIKE '%-m/%d/%Y %H%' lookup in sendcrimes.send_hourly.
                    "entrytime": dt.strftime("%-m/%d/%Y %H:%M:%S"),
                    "description": clean(crime.get("DESC")),
                    "location": clean(crime.get("LOCATION")),
                    "sector": clean(crime.get("SECTOR")),
                    "zone": clean(crime.get("ZONE")),
                    "rd": clean(crime.get("RD")),
                }

                exists = connection.execute(
                    text("SELECT 1 FROM orange_crimes WHERE incident = :incident"), params
                ).first()

                if exists is None:
                    connection.execute(text("""
                        INSERT INTO orange_crimes (incident, entrytime, description, location, sector, zone, rd)
                        VALUES (:incident, :entrytime, :description, :location, :sector, :zone, :rd)
                    """), params)
                else:
                    connection.execute(text("""
                        UPDATE orange_crimes
                        SET entrytime = :entrytime, description = :description, location = :location,
                            sector = :sector, zone = :zone, rd = :rd
                        WHERE incident = :incident
                    """), params)

                connection.commit()

            except Exception as e:
                # Roll back so one bad row doesn't leave the transaction aborted for the rest.
                connection.rollback()
                print(f"Exception occurred: {type(e).__name__}: {str(e)}")

        result = connection.execute(text("SELECT count(*) FROM orange_crimes")).fetchone()
        print("Orange County crimes database updated.")
        print(f"Current number of entries in database: {result[0]}")

def load_orange_active_backup(engine: Engine) -> None:
    """
    Backup source: load active incidents from the davnit ESMap API and upsert OCSO calls into
    orange_crimes. Use if the OCSO feed in load_orange_active() goes down. Its incident keys differ
    from OCSO's, so running both at once duplicates calls.
    """
    connection = engine.connect()

    r = requests.get("https://www.davnit.net/esmap/api/incidents/active", headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = json.loads(r.text)
    for feature in data.get('features', []):
        try:
            crime = feature.get('properties')

            if crime.get('source') == "OCSO":
                print(crime)
                dt = datetime.strptime(crime.get("time"), "%Y-%m-%d %H:%M:%S")
                if 1 <= dt.hour <= 11 and (datetime.now().hour >= 13 or datetime.now().hour == 0):
                    dt = dt.replace(hour=dt.hour + 12)
                elif dt.hour == 12 and datetime.now().hour in (0, 1):
                    dt = dt.replace(hour=0)
                    
                print(dt)
                year_date = dt.strftime("%Y-%j")
                entrytime = dt.strftime("%-m/%d/%Y %H:%M:%S")
                source_id = str(crime.get("source_id", "")).strip()
                key = str(crime.get("key", "")).strip()
                incident_key = f"{source_id}/{key}" if (source_id and key) else (key or "unknown")
                expanded_incident = f"{year_date}-{incident_key}"
                description = crime.get('description')

                if crime.get('location'):
                    location = crime['location'].replace("'", "''")
                else:
                    location = crime['location']

                query = f"""SELECT * FROM orange_crimes WHERE incident = '{expanded_incident}'"""
                result = connection.execute(text(query))

                if result.rowcount == 0:
                    connection.execute(
                    text("""
                        INSERT INTO orange_crimes
                          (incident, entrytime, description, location, sector, zone, rd)
                        VALUES
                          (:incident, :entrytime, :description, :location, :sector, :zone, :rd)
                    """),
                    {
                        "incident": expanded_incident,
                        "entrytime": entrytime,
                        "description": description,
                        "location": location,
                        "sector": None,
                        "zone": None,
                        "rd": None,
                    },
                )

                else:
                    connection.execute(
                        text("""
                            UPDATE orange_crimes
                            SET description = :description,
                                location = :location,
                                sector = :sector,
                                zone = :zone,
                                rd = :rd
                            WHERE incident = :incident
                        """),
                        {
                            "incident": expanded_incident,
                            "description": description,
                            "location": location,
                            "sector": None,
                            "zone": None,
                            "rd": None,
                        },
                    )
                    
                connection.execute(text(query))
                connection.commit()

        except Exception as e:
            print(f"Exception occurred: {type(e).__name__}: {str(e)}")
            # traceback.print_exc()

    query = f"SELECT count(*) AS exact_count FROM orange_crimes"
    result = connection.execute(text(query)).fetchone()
    print("Orange County crimes database updated.")
    print(f"Current number of entries in database: {result[0]}")
    connection.close()

def backup_tables(engine: Engine) -> None:
    orlando_list = pd.read_sql_table("orlando_crimes", engine)
    orlando_list['date'] = pd.to_datetime(orlando_list['date'], format='%m/%d/%Y %H:%M')
    orlando_list = orlando_list.sort_values(by='date')
    orlando_list = orlando_list.reset_index(drop=True)
    orlando_list.to_csv("./backups/orlando_backup.csv", index=True)

    orange_list = pd.read_sql_table("orange_crimes", engine)
    orange_list['entrytime'] = pd.to_datetime(orange_list['entrytime'], format='%m/%d/%Y %H:%M:%S')
    orange_list = orange_list.sort_values(by='entrytime')
    orange_list = orange_list.reset_index(drop=True)
    orange_list.to_csv("./backups/orange_backup.csv", index=True)

    print("OPD & OSCO tables backed up to CSV files.")

if __name__ == '__main__':
    main_config = ConfigParser()
    main_config.read('config.ini')
    engine = setup_db(main_config)
    osm_user_agent = main_config.get('DISCORD', 'OSM_USER_AGENT')
    osm_referer = main_config.get('DISCORD', 'OSM_REFERER')

    counter = 0
    last_backup_date = None

    print("Starting OPD & OCSO time counter (every 10 mins)")
    while True:
        if counter >= 600:
            counter = 0
            attempts = 0
            while attempts < 5:
                try:
                    load_orlando_active(engine)
                    load_orange_active(engine)
                    break
                except Exception as e:
                    print(f"Exception occurred: {type(e).__name__}: {str(e)}")
                    attempts += 1
                    sleep(1)

            # Geocode new call locations now so the hourly heatmap finds them cached. Kept outside
            # the retry loop above so a geocoding problem never re-runs the loads.
            try:
                prewarm_call_geocodes(engine, osm_user_agent, osm_referer)
            except Exception as e:
                print(f"Geocode prewarm failed: {type(e).__name__}: {str(e)}")
        
        counter += 1

        # Once per day during the 1 AM hour. (An exact 1:00:00 check could be skipped while a load
        # or geocode prewarm was running.)
        now = datetime.now()
        if now.hour == 1 and last_backup_date != now.date():
            last_backup_date = now.date()
            backup_tables(engine)

        sleep(1)
