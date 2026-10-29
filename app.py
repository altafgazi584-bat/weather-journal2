
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# --------------------------------------------------
# Application configuration
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
BOUNDARY_URL = "https://nominatim.openstreetmap.org/search"

REQUEST_TIMEOUT = 12
BOUNDARY_TIMEOUT = 8
BOUNDARY_CACHE_TTL = 24 * 60 * 60  # Keep city outlines for 24 hours.
BOUNDARY_MAX_POINTS = 180

# A small in-memory cache avoids repeatedly asking OpenStreetMap for the same
# city outline during local development. Weather requests never depend on it.
_boundary_cache = {}

# --------------------------------------------------
# Weather condition mapping
# Based on Open-Meteo WMO weather codes
# --------------------------------------------------

def get_weather_condition(weather_code, is_day=True):
    """Return a readable condition, icon, and animation scene."""

    if weather_code is None:
        return {
            "condition": "Unknown",
            "icon": "weather",
            "scene": "cloudy",
        }

    if weather_code == 0:
        if is_day:
            return {
                "condition": "Clear sky",
                "icon": "sun",
                "scene": "clear-day",
            }

        return {
            "condition": "Clear night",
            "icon": "moon",
            "scene": "clear-night",
        }

    if weather_code in (1, 2):
        if is_day:
            return {
                "condition": "Mainly clear" if weather_code == 1
                else "Partly cloudy",
                "icon": "partly-cloudy",
                "scene": "partly-cloudy-day",
            }

        return {
            "condition": "Mainly clear" if weather_code == 1
            else "Partly cloudy",
            "icon": "partly-cloudy-night",
            "scene": "partly-cloudy-night",
        }

    if weather_code == 3:
        return {
            "condition": "Overcast",
            "icon": "cloud",
            "scene": "cloudy",
        }

    if weather_code in (45, 48):
        return {
            "condition": "Fog",
            "icon": "fog",
            "scene": "fog",
        }

    if weather_code in (51, 53, 55):
        return {
            "condition": "Drizzle",
            "icon": "drizzle",
            "scene": "drizzle",
        }

    if weather_code in (56, 57):
        return {
            "condition": "Freezing drizzle",
            "icon": "drizzle",
            "scene": "drizzle",
        }

    if weather_code in (61, 63, 65):
        return {
            "condition": "Rain",
            "icon": "rain",
            "scene": "rain",
        }

    if weather_code in (66, 67):
        return {
            "condition": "Freezing rain",
            "icon": "rain",
            "scene": "rain",
        }

    if weather_code in (71, 73, 75, 77):
        return {
            "condition": "Snow",
            "icon": "snow",
            "scene": "snow",
        }

    if weather_code in (80, 81, 82):
        return {
            "condition": "Rain showers",
            "icon": "rain",
            "scene": "rain",
        }

    if weather_code in (85, 86):
        return {
            "condition": "Snow showers",
            "icon": "snow",
            "scene": "snow",
        }

    if weather_code in (95, 96, 99):
        return {
            "condition": "Thunderstorm",
            "icon": "thunderstorm",
            "scene": "thunderstorm",
        }

    return {
        "condition": "Variable conditions",
        "icon": "cloud",
        "scene": "cloudy",
    }


# --------------------------------------------------
# General helpers
# --------------------------------------------------

def safe_number(value, digits=1):
    """Convert a numeric value safely, preserving None."""

    if value is None:
        return None

    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def get_json(url, params=None):
    """Request JSON data and handle network/API errors."""

    response = requests.get(
        url,
        params=params,
        timeout=REQUEST_TIMEOUT,
        headers={"Accept": "application/json"},
    )

    response.raise_for_status()
    data = response.json()

    if not isinstance(data, dict):
        raise ValueError("The weather service returned invalid data.")

    if data.get("error"):
        raise ValueError(
            data.get("reason", "The weather service returned an error.")
        )

    return data


def get_local_time(timezone_name):
    """Return local time in the location's timezone."""

    try:
        local_now = datetime.now(ZoneInfo(timezone_name))

        return {
            "time": local_now.strftime("%I:%M %p").lstrip("0"),
            "date": local_now.strftime("%A, %d %B %Y"),
            "timezone": timezone_name,
        }

    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return {
            "time": None,
            "date": None,
            "timezone": timezone_name,
        }


# --------------------------------------------------
# City search / geocoding
# --------------------------------------------------

def search_city(city):
    """Convert a city name into geographic coordinates."""

    city = (city or "").strip()

    if not city:
        raise ValueError("Please enter a city name.")

    if len(city) > 100:
        raise ValueError("Please enter a shorter city name.")

    data = get_json(
        GEOCODING_URL,
        params={
            "name": city,
            "count": 10,
            "language": "en",
            "format": "json",
        },
    )

    results = data.get("results", [])

    if not results:
        raise ValueError(
            f"We couldn't find '{city}'. Check the spelling and try again."
        )

    # Prefer an exact case-insensitive city-name match when available.
    city_lower = city.casefold()

    result = next(
        (
            item for item in results
            if item.get("name", "").casefold() == city_lower
        ),
        results[0],
    )

    return {
        "name": result.get("name", city),
        "latitude": result.get("latitude"),
        "longitude": result.get("longitude"),
        "country": result.get("country", ""),
        "admin1": result.get("admin1", ""),
        "country_code": result.get("country_code", ""),
    }


# --------------------------------------------------
# Optional city administrative boundary (OpenStreetMap)
# --------------------------------------------------

def _sample_ring(ring, max_points=BOUNDARY_MAX_POINTS):
    """Reduce a GeoJSON ring to a manageable number of points."""
    if not isinstance(ring, list) or len(ring) < 4:
        return []

    # Keep the closing point when the ring is closed.
    points = ring[:-1] if ring[0] == ring[-1] else ring[:]
    if len(points) > max_points:
        step = (len(points) - 1) / float(max_points - 1)
        points = [points[round(index * step)] for index in range(max_points)]

    cleaned = []
    for point in points:
        if (isinstance(point, (list, tuple)) and len(point) >= 2):
            try:
                lon, lat = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                continue
            if -180 <= lon <= 180 and -90 <= lat <= 90:
                cleaned.append([lon, lat])

    if len(cleaned) < 3:
        return []
    cleaned.append(cleaned[0])
    return cleaned


def _extract_boundary_polygons(geometry):
    """Return simplified outer rings from a GeoJSON Polygon/MultiPolygon."""
    if not isinstance(geometry, dict):
        return []

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    polygons = []

    if geometry_type == "Polygon" and isinstance(coordinates, list):
        source_polygons = [coordinates]
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        source_polygons = coordinates
    else:
        return []

    for polygon in source_polygons:
        # The first ring is the exterior boundary; inner rings are holes.
        if not isinstance(polygon, list) or not polygon:
            continue
        ring = _sample_ring(polygon[0])
        if ring:
            polygons.append(ring)

    return polygons


def get_city_boundary(location):
    """Fetch a real OSM city outline; return None if no usable boundary exists.

    Boundary lookup is deliberately optional: a Nominatim/API failure must not
    stop the weather dashboard from working.
    """
    if not isinstance(location, dict):
        return None

    city_name = (location.get("name") or "").strip()
    country = (location.get("country") or "").strip()
    region = (location.get("admin1") or "").strip()
    if not city_name or city_name == "Your Location":
        return None

    cache_key = "|".join((city_name.casefold(), region.casefold(), country.casefold()))
    now = time.time()
    cached = _boundary_cache.get(cache_key)
    if cached and now - cached[0] < BOUNDARY_CACHE_TTL:
        return cached[1]

    query_parts = [city_name]
    if region and region.casefold() != city_name.casefold():
        query_parts.append(region)
    if country:
        query_parts.append(country)

    try:
        response = requests.get(
            BOUNDARY_URL,
            params={
                "q": ", ".join(query_parts),
                "format": "jsonv2",
                "polygon_geojson": 1,
                "addressdetails": 1,
                "limit": 5,
            },
            timeout=BOUNDARY_TIMEOUT,
            headers={
                "Accept": "application/json",
                "User-Agent": "WeatherDashboardStudentProject/1.0",
            },
        )
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list):
            return None

        # Prefer a result with a real polygon geometry rather than a point.
        for result in results:
            polygons = _extract_boundary_polygons(result.get("geojson"))
            if polygons:
                boundary = {
                    "polygons": polygons,
                    "source": "OpenStreetMap",
                    "label": result.get("display_name", city_name),
                }
                _boundary_cache[cache_key] = (now, boundary)
                return boundary

        _boundary_cache[cache_key] = (now, None)
        return None

    except (requests.RequestException, ValueError, TypeError, KeyError):
        logger.info("City boundary unavailable for %s; continuing without it.", city_name)
        return None


# --------------------------------------------------
# Current weather and forecast
# --------------------------------------------------

def get_weather_data(latitude, longitude, location=None):
    """
    Fetch current weather and a six-day forecast:
    today plus the next five days.
    """

    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        raise ValueError("Invalid geographic coordinates.")

    if not (-90 <= latitude <= 90):
        raise ValueError("The latitude is outside the valid range.")

    if not (-180 <= longitude <= 180):
        raise ValueError("The longitude is outside the valid range.")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": (
            "temperature_2m,"
            "relative_humidity_2m,"
            "apparent_temperature,"
            "is_day,"
            "precipitation,"
            "rain,"
            "showers,"
            "snowfall,"
            "weather_code,"
            "wind_speed_10m,"
            "wind_direction_10m"
        ),
        "daily": (
            "weather_code,"
            "temperature_2m_max,"
            "temperature_2m_min,"
            "sunrise,"
            "sunset,"
            "precipitation_probability_max"
        ),
        "forecast_days": 6,
        "timezone": "auto",
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    data = get_json(WEATHER_URL, params=params)

    current = data.get("current")
    daily = data.get("daily")

    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise ValueError("Weather data is temporarily incomplete.")

    is_day = current.get("is_day", 1) == 1

    current_code = current.get("weather_code")
    current_condition = get_weather_condition(current_code, is_day)

    timezone_name = data.get("timezone", "UTC")
    local_time = get_local_time(timezone_name)

    # Sunrise and sunset for today.
    sunrise_times = daily.get("sunrise") or []
    sunset_times = daily.get("sunset") or []

    sunrise = (
        sunrise_times[0].split("T")[-1]
        if sunrise_times and sunrise_times[0]
        else None
    )

    sunset = (
        sunset_times[0].split("T")[-1]
        if sunset_times and sunset_times[0]
        else None
    )

    # Build today's forecast plus five following days.
    forecast = []

    dates = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    precipitation = daily.get("precipitation_probability_max") or []

    for index, forecast_date in enumerate(dates):
        code = codes[index] if index < len(codes) else None

        condition = get_weather_condition(code, is_day=True)

        try:
            readable_date = datetime.strptime(
                forecast_date, "%Y-%m-%d"
            ).strftime("%a, %d %b")
            day_name = datetime.strptime(
                forecast_date, "%Y-%m-%d"
            ).strftime("%A")
        except (TypeError, ValueError):
            readable_date = forecast_date
            day_name = forecast_date

        forecast.append({
            "date": forecast_date,
            "display_date": readable_date,
            "day": day_name,
            "is_today": index == 0,
            "temperature_max": (
                safe_number(highs[index])
                if index < len(highs) else None
            ),
            "temperature_min": (
                safe_number(lows[index])
                if index < len(lows) else None
            ),
            "weather_code": code,
            "condition": condition["condition"],
            "icon": condition["icon"],
            "scene": condition["scene"],
            "precipitation_probability": (
                precipitation[index]
                if index < len(precipitation) else None
            ),
        })

    place = location or {}

    city_name = place.get("name", "Your Location")
    country = place.get("country", "")
    region = place.get("admin1", "")

    return {
        "city": city_name,
        "country": country,
        "region": region,
        "latitude": latitude,
        "longitude": longitude,

        "temperature": safe_number(
            current.get("temperature_2m")
        ),
        "feels_like": safe_number(
            current.get("apparent_temperature")
        ),
        "humidity": current.get("relative_humidity_2m"),
        "wind_speed": safe_number(
            current.get("wind_speed_10m")
        ),
        "wind_direction": safe_number(
            current.get("wind_direction_10m"), digits=0
        ),

        "precipitation": safe_number(
            current.get("precipitation")
        ),
        "rain": safe_number(current.get("rain")),
        "showers": safe_number(current.get("showers")),
        "snowfall": safe_number(current.get("snowfall")),

        "weather_code": current_code,
        "condition": current_condition["condition"],
        "icon": current_condition["icon"],
        "scene": current_condition["scene"],
        "is_day": is_day,

        "local_time": local_time["time"],
        "local_date": local_time["date"],
        "timezone": timezone_name,

        "sunrise": sunrise,
        "sunset": sunset,

        "forecast": forecast,
        "temperature_unit": "°C",
        "wind_unit": "km/h",
        "precipitation_unit": "mm",
    }


def get_weather(city):
    """Search for a city and retrieve its weather."""

    location = search_city(city)

    weather = get_weather_data(
        location["latitude"],
        location["longitude"],
        location=location,
    )

    # The boundary is an optional visual enhancement, never a dependency for
    # retrieving or displaying weather data.
    weather["city_boundary"] = get_city_boundary(location)
    return weather


# --------------------------------------------------
# Main dashboard
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    weather = None
    error = None
    city = ""

    if request.method == "POST":
        city = request.form.get("city", "").strip()

        try:
            weather = get_weather(city)
            city = weather["city"]

        except ValueError as exc:
            error = str(exc)

        except requests.Timeout:
            error = (
                "The weather service took too long to respond. "
                "Please try again."
            )

        except requests.RequestException:
            logger.exception("Weather API request failed.")
            error = (
                "We couldn't connect to the weather service. "
                "Check your internet connection and try again."
            )

        except (KeyError, TypeError, IndexError):
            logger.exception("Unexpected weather response structure.")
            error = (
                "We couldn't process the weather data. "
                "Please try again shortly."
            )

        except Exception:
            logger.exception("Unexpected error while retrieving weather.")
            error = "Something went wrong. Please try again."

    return render_template(
        "index.html",
        weather=weather,
        error=error,
        city=city,
    )


# --------------------------------------------------
# Current-location dashboard route
# Used when the visitor has not searched a city yet, so the page can
# render the full dashboard with the location forecast.
# --------------------------------------------------

@app.route("/current-location", methods=["GET"])
def current_location_dashboard():
    latitude = request.args.get("latitude")
    longitude = request.args.get("longitude")

    try:
        if latitude is None or longitude is None:
            raise ValueError("Location coordinates are required.")

        weather = get_weather_data(
            latitude,
            longitude,
            location={"name": "Your Location", "country": "", "admin1": ""},
        )
        return render_template(
            "index.html", weather=weather, error=None, city=weather["city"]
        )

    except ValueError as exc:
        error = str(exc)
    except requests.Timeout:
        error = "The weather service timed out. Please try again."
    except requests.RequestException:
        logger.exception("Current-location dashboard request failed.")
        error = "Unable to connect to the weather service. Please try again."
    except Exception:
        logger.exception("Unexpected current-location dashboard error.")
        error = "Unable to retrieve weather right now. Please try again."

    return render_template("index.html", weather=None, error=error, city=""), 400


# --------------------------------------------------
# Current-location API endpoint
# The browser supplies coordinates after permission.
# --------------------------------------------------

@app.route("/api/weather", methods=["GET"])
def current_location_weather():
    latitude = request.args.get("latitude")
    longitude = request.args.get("longitude")

    if latitude is None or longitude is None:
        return jsonify({
            "success": False,
            "error": "Location coordinates are required.",
        }), 400

    try:
        weather = get_weather_data(
            latitude,
            longitude,
            location={
                "name": "Your Location",
                "country": "",
                "admin1": "",
            },
        )

        return jsonify({
            "success": True,
            "weather": weather,
        })

    except ValueError as exc:
        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400

    except requests.Timeout:
        return jsonify({
            "success": False,
            "error": "The weather service timed out. Please try again.",
        }), 504

    except requests.RequestException:
        logger.exception("Current-location weather API request failed.")

        return jsonify({
            "success": False,
            "error": "Unable to connect to the weather service.",
        }), 502

    except Exception:
        logger.exception("Unexpected current-location weather error.")

        return jsonify({
            "success": False,
            "error": "Unable to retrieve weather right now.",
        }), 500


# --------------------------------------------------
# Development server
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)

