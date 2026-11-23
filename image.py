'''

UCF Crimes: image.py
Written by Jack Sweeney, Ethan Frakes

'''

import utils
import staticmaps
from PIL import Image, ImageDraw
import pandas as pd
import folium
from folium.plugins import HeatMap
from configparser import ConfigParser
import os
import io
import discord
import math
import asyncio
from sqlalchemy.engine.base import Engine

# CARTO basemap key. Kept in config.ini so it stays out of source. Without it
# CARTO still serves tiles, but stamped "API KEY REQUIRED", so warn rather than
# fail silently. Also honours a CARTO_API_KEY environment variable.
_carto_config = ConfigParser()
_carto_config.read(["config.ini", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")])

# raw=True so a '%' in the key isn't treated as configparser interpolation
CARTO_API_KEY = (os.environ.get("CARTO_API_KEY", "").strip()
                 or _carto_config.get("CARTO", "CARTO_API_KEY", raw=True, fallback="").strip())

if not CARTO_API_KEY:
    print("WARNING: [CARTO] CARTO_API_KEY not found in config.ini - basemap tiles will be watermarked.")

CARTO_ATTRIBUTION = "Maps (C) CARTO (C) OpenStreetMap.org contributors"

# CARTO hands out .../{z}/{x}/{y}.png?key=<key>, which is the form folium and
# Leaflet want. py-staticmaps templates with string.Template instead, so it takes
# $z/$x/$y and fills $k from its api_key argument. Same endpoint either way.
CARTO_VOYAGER_FOLIUM_URL = "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=" + CARTO_API_KEY

CARTODB_VOYAGER_TILE_PROVIDER = staticmaps.TileProvider(
    "cartodb-voyager",
    url_pattern="https://basemaps.cartocdn.com/rastertiles/voyager/$z/$x/$y.png?key=$k",
    api_key=CARTO_API_KEY,
    attribution=CARTO_ATTRIBUTION,
    max_zoom=20,
)

def generate_image(crime: pd.Series) -> None:
    context = staticmaps.Context()
    context.set_tile_provider(CARTODB_VOYAGER_TILE_PROVIDER)

    lat = crime["lat"]
    lng = crime["lng"]

    if pd.isna(lat) or pd.isna(lng):
        lat, lng = 28.60, -81.20

    loc = staticmaps.create_latlng(lat, lng)
    context.add_object(staticmaps.Marker(loc, color=staticmaps.RED, size=12))

    # render anti-aliased png (this only works if pycairo is installed)
    image = context.render_cairo(380, 380)
    image.write_to_png("case.png")

    # render anti-aliased png (this only works if pycairo is installed)
    context.set_zoom(18)
    image = context.render_cairo(1080, 1080)
    image.write_to_png("casez.png")
    
    im1 = Image.open('casez.png')
    im2 = Image.open('case.png')
    im1.paste(im2, (700, 700))
    draw = ImageDraw.Draw(im1)
    draw.line((700, 700, 700, 1080), fill=(0, 0, 0), width=10)
    draw.line((696, 700, 1080, 700), fill=(0, 0, 0), width=10)
    im1.save('caseout.png', quality=100)

    os.remove('casez.png')
    os.remove('case.png')

def generate_image_all(crimes: pd.DataFrame) -> None:
    context = staticmaps.Context()
    context.set_tile_provider(CARTODB_VOYAGER_TILE_PROVIDER)

    for _, crime in crimes.iterrows():
        lat, lng = crime["lat"], crime["lng"]

        if not pd.isna(lat) and not pd.isna(lng):
            loc = staticmaps.create_latlng(lat, lng)
            context.add_object(staticmaps.Marker(loc, color=staticmaps.RED, size=12))
            
    image = context.render_cairo(1080, 1080)
    image.write_to_png("caseall.png")

HOURLY_MAP_CENTER = (28.55, -81.39)
HOURLY_MAP_SIZE = (1366, 768)  # pixel size folium's _to_png renders at

def _fits_view(points: list, center: tuple[float, float], zoom: int, margin: int = 25) -> bool:
    '''True if every (lat, lng) lands inside the rendered frame at this center/zoom (Web Mercator).'''
    def to_px(lat, lng):
        scale = 256 * 2 ** zoom
        siny = math.sin(math.radians(lat))
        return (lng + 180) / 360 * scale, (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * scale
    cx, cy = to_px(*center)
    half_w, half_h = HOURLY_MAP_SIZE[0] / 2 - margin, HOURLY_MAP_SIZE[1] / 2 - margin
    return all(abs(x - cx) <= half_w and abs(y - cy) <= half_h for x, y in (to_px(lat, lng) for lat, lng, _ in points))

def _render_hourly_png(heat_map_data: list, zoom: int) -> None:
    '''Build and screenshot the folium map. Blocking (headless browser), so run it in a thread.'''
    # folium's built-in "CartoDB Voyager" is the unkeyed endpoint, which comes back
    # watermarked, so use the keyed URL built above.
    m = folium.Map(
        location=list(HOURLY_MAP_CENTER),
        zoom_start=zoom,
        tiles=CARTO_VOYAGER_FOLIUM_URL,
        attr=CARTO_ATTRIBUTION,
    )
    HeatMap(heat_map_data).add_to(m)
    img_data = m._to_png(5)
    img = Image.open(io.BytesIO(img_data))
    img.save('hourly_map.png')

async def generate_hourly_heatmap(calls: pd.DataFrame, channel: discord.TextChannel, OSM_USER_AGENT: str, OSM_REFERER: str,
                                  zoom: int, policedpt: str, engine: Engine):
    '''
    Geocode the hour's calls and post a heatmap. Locations come from the geocode cache (filled
    every 10 minutes by orlando_orange.py), so usually only new ones hit Nominatim.
    Blocking work runs in threads so the Discord heartbeat keeps going.
    '''
    def geocode_all() -> list:
        points = []
        for location in calls['location']:
            queries = utils.format_call_location(location, policedpt)
            lat, lng = utils.geocode_cached(engine, queries, OSM_USER_AGENT, OSM_REFERER)
            if lat is not None and lng is not None:
                points.append([lat, lng, 0.3])
        return points

    heat_map_data = await asyncio.to_thread(geocode_all)

    # Zoom out one level if calls fall outside the default frame (e.g. OPD's Lake Nona/airport area
    # at zoom 12), but no further, so one bad geocode far away can't shrink the whole map.
    if heat_map_data and not _fits_view(heat_map_data, HOURLY_MAP_CENTER, zoom):
        zoom -= 1

    print(f"Hourly heatmap ({policedpt}): plotted {len(heat_map_data)}/{len(calls)} calls at zoom {zoom}.")
    await asyncio.to_thread(_render_hourly_png, heat_map_data, zoom)

    await channel.send(file=discord.File("./hourly_map.png"))

async def generate_heatmap(interaction: discord.Interaction, command_arg: str, main_config: ConfigParser) -> None:
    engine = utils.setup_db(main_config)
    await interaction.response.defer()
    await interaction.followup.send("Generating Heatmap... This May Take a Moment...")

    query = "SELECT lat, lng from crimes WHERE address != '4000 CENTRAL FLORIDA BLVD' AND lat IS NOT NULL AND lng IS NOT NULL;"
    crime_coords = pd.read_sql_query(query, engine)

    if command_arg.title().startswith('Main'):
        coords = [28.60, -81.20]
    elif command_arg.title().startswith('Downtown'):
        coords = [28.55, -81.39]
    elif command_arg.title().startswith('Rosen'):
        coords = [28.43, -81.44]
    else:
        return await interaction.followup.send("Please choose from one of these campuses: Main, Downtown, or Rosen.")

    m = folium.Map(location=coords, zoom_start=14)
    heat_map_data = []

    for _, coords in crime_coords.iterrows():
        heat_map_data.append([float(coords["lat"]), float(coords["lng"]), 0.3])

    HeatMap(heat_map_data).add_to(m)
    img_data = m._to_png(5)
    img = Image.open(io.BytesIO(img_data))
    img.save('heatmap.png')

    await interaction.followup.send(file=discord.File("./heatmap.png"))
    print("Heatmap sent.")

async def generate_heatmap_csv(interaction: discord.Interaction, command_arg: str) -> None:
    await interaction.response.defer()
    await interaction.followup.send("Generating Heatmap... This May Take a Moment...")
    
    crimes = pd.read_csv('crimes.csv')
    
    if command_arg.title().startswith('Main'):
        coords = [28.60, -81.20]
    elif command_arg.title().startswith('Downtown'):
        coords = [28.55, -81.39]
    elif command_arg.title().startswith('Rosen'):
        coords = [28.43, -81.44]
    else:
        return await interaction.followup.send("Please choose from one of these campuses: Main, Downtown, or Rosen.")

    m = folium.Map(location=coords, zoom_start=15)
    heat_map_data = []

    for _, crime in crimes.iterrows():
        if "4000 CENTRAL FLORIDA BLVD" not in crime["place"]:
            if not math.isnan(crime["lat"]) and not math.isnan(crime["lng"]):
                lat = float(crime["lat"])
                lng = float(crime["lng"])

                heat_map_data.append([lat, lng, 0.3])

    HeatMap(heat_map_data).add_to(m)
    img_data = m._to_png(5)
    img = Image.open(io.BytesIO(img_data))
    img.save('heatmap.png')

    await interaction.followup.send(file=discord.File("./heatmap.png"))