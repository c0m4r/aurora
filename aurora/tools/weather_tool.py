"""Weather forecast tool using Open-Meteo API (no API key required)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from .base import BaseTool, ToolDefinition

logger = logging.getLogger(__name__)

# Open-Meteo API base URL
_OPEN_METEO_BASE = "https://api.open-meteo.com/v1"

# WMO weather code descriptions
_WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Depositing rime fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Moderate drizzle", "🌦️"),
    55: ("Dense drizzle", "🌧️"),
    56: ("Light freezing drizzle", "🌧️"),
    57: ("Dense freezing drizzle", "🌧️"),
    61: ("Slight rain", "🌦️"),
    63: ("Moderate rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    66: ("Light freezing rain", "🌧️"),
    67: ("Heavy freezing rain", "🌧️"),
    71: ("Slight snowfall", "🌨️"),
    73: ("Moderate snowfall", "❄️"),
    75: ("Heavy snowfall", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Slight rain showers", "🌦️"),
    81: ("Moderate rain showers", "🌧️"),
    82: ("Violent rain showers", "⛈️"),
    85: ("Slight snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm with slight hail", "⛈️"),
    99: ("Thunderstorm with heavy hail", "⛈️"),
}


def _describe_weather(code: int) -> str:
    desc, emoji = _WMO_CODES.get(code, ("Unknown", "❓"))
    return f"{emoji} {desc} (code {code})"


# Popular cities with coordinates
_CITIES: dict[str, tuple[float, float]] = {
    # Europe
    "warsaw": (52.23, 21.01),
    "krakow": (50.06, 19.94),
    "wroclaw": (51.11, 17.03),
    "gdansk": (54.35, 18.65),
    "poznan": (52.41, 16.93),
    "berlin": (52.52, 13.41),
    "london": (51.51, -0.13),
    "paris": (48.86, 2.35),
    "amsterdam": (52.37, 4.90),
    "rome": (41.90, 12.50),
    "madrid": (40.42, -3.70),
    "vienna": (48.21, 16.37),
    "prague": (50.08, 14.43),
    "stockholm": (59.33, 18.07),
    "oslo": (59.91, 10.75),
    # North America
    "new york": (40.71, -74.01),
    "los angeles": (34.05, -118.24),
    "chicago": (41.88, -87.63),
    "san francisco": (37.77, -122.42),
    "toronto": (43.65, -79.38),
    "vancouver": (49.28, -123.12),
    # Asia
    "tokyo": (35.68, 139.69),
    "shanghai": (31.23, 121.47),
    "singapore": (1.35, 103.82),
    "mumbai": (19.08, 72.88),
    "seoul": (37.57, 126.98),
    "beijing": (39.90, 116.40),
    # Australia
    "sydney": (-33.87, 151.21),
    "melbourne": (-37.81, 144.96),
}


class WeatherTool(BaseTool):
    def __init__(self, default_forecast_days: int = 3):
        self.default_forecast_days = min(max(default_forecast_days, 1), 16)

    def definition(self) -> ToolDefinition:
        city_list = ", ".join(sorted(_CITIES.keys()))
        return ToolDefinition(
            name="weather",
            description=(
                "Get current weather and forecast data from Open-Meteo API.\n\n"
                "Provide either:\n"
                "- `city`: a well-known city name (e.g. 'Warsaw', 'London', 'Tokyo')\n"
                "- `latitude` and `longitude`: precise coordinates\n\n"
                "Returns temperature, wind, precipitation, weather condition description,\n"
                "and a multi-day forecast.\n\n"
                f"**Built-in cities**: {city_list}\n\n"
                "Use this when the user asks about weather, temperature, forecast, "
                "or 'what's it like outside'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": (
                            "City name (e.g. 'Warsaw', 'London', 'New York'). "
                            "If the city is not in the built-in list, provide coordinates instead."
                        ),
                    },
                    "latitude": {
                        "type": "number",
                        "description": "Latitude in decimal degrees (e.g. 52.23). Required if `city` is not provided.",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "Longitude in decimal degrees (e.g. 21.01). Required if `city` is not provided.",
                    },
                    "forecast_days": {
                        "type": "integer",
                        "description": f"Number of forecast days (1–16, default {self.default_forecast_days}).",
                    },
                    "temperature_unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": "Temperature unit (default: celsius).",
                    },
                },
            },
        )

    async def execute(
        self,
        city: str | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        forecast_days: int | None = None,
        temperature_unit: str = "celsius",
        **_: Any,
    ) -> str:
        # Resolve location
        if city:
            coords = _resolve_city(city)
            if coords is None:
                return (
                    f"Unknown city '{city}'.\n"
                    f"Available cities: {', '.join(sorted(_CITIES.keys()))}\n"
                    "Alternatively, provide `latitude` and `longitude` directly."
                )
            lat, lon = coords
        elif latitude is not None and longitude is not None:
            lat, lon = latitude, longitude
        else:
            return "Provide either `city` name, or both `latitude` and `longitude`."

        days = min(int(forecast_days or self.default_forecast_days), 16)
        temp_unit = temperature_unit.lower() if temperature_unit else "celsius"
        if temp_unit not in ("celsius", "fahrenheit"):
            temp_unit = "celsius"

        # Fetch weather data from Open-Meteo
        data = await self._fetch_weather(lat, lon, days, temp_unit)
        if not data:
            return f"Failed to fetch weather data for coordinates ({lat}, {lon})."

        return _format_weather(lat, lon, data, temp_unit)

    async def _fetch_weather(
        self, lat: float, lon: float, days: int, temp_unit: str
    ) -> dict | None:
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "precipitation,"
                "weather_code,"
                "wind_speed_10m,"
                "wind_direction_10m,"
                "wind_gusts_10m,"
                "cloud_cover,"
                "apparent_temperature"
            ),
            "daily": (
                "weather_code,"
                "temperature_2m_max,"
                "temperature_2m_min,"
                "apparent_temperature_max,"
                "apparent_temperature_min,"
                "precipitation_sum,"
                "precipitation_probability_max,"
                "wind_speed_10m_max,"
                "wind_gusts_10m_max,"
                "sunrise,"
                "sunset"
            ),
            "forecast_days": days,
            "temperature_unit": temp_unit,
            "wind_speed_unit": "kmh",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(f"{_OPEN_METEO_BASE}/forecast", params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            logger.debug("Open-Meteo fetch failed: %s", exc)
            return None


def _resolve_city(name: str) -> tuple[float, float] | None:
    """Look up city coordinates (case-insensitive, with fallback matching)."""
    key = name.lower().strip()
    if key in _CITIES:
        return _CITIES[key]
    # Try partial match
    for city_name, coords in _CITIES.items():
        if key in city_name or city_name in key:
            return coords
    return None


def _format_weather(lat: float, lon: float, data: dict, temp_unit: str) -> str:
    """Format Open-Meteo response into readable text."""
    temp_symbol = "°F" if temp_unit == "fahrenheit" else "°C"
    wind_unit = "km/h"

    current = data.get("current", {})
    daily = data.get("daily", {})
    tz = data.get("timezone", "local")
    elev = data.get("elevation", "unknown")

    # Location info
    lines = [
        f"### Weather Report",
        f"**Location**: ({lat}, {lon}) — Timezone: {tz}, Elevation: {elev}m",
        "",
    ]

    # Current conditions
    weather_code = current.get("weather_code", -1)
    weather_desc = _describe_weather(weather_code) if weather_code >= 0 else "Unknown"
    temp = current.get("temperature_2m", "N/A")
    feels_like = current.get("apparent_temperature", "N/A")
    humidity = current.get("relative_humidity_2m", "N/A")
    wind_speed = current.get("wind_speed_10m", "N/A")
    wind_gusts = current.get("wind_gusts_10m", "N/A")
    wind_dir = current.get("wind_direction_10m", 0)
    precip = current.get("precipitation", "N/A")
    cloud_cover = current.get("cloud_cover", "N/A")

    now_str = current.get("time", "")
    if now_str:
        try:
            now_dt = datetime.fromisoformat(now_str)
            now_human = now_dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            now_human = now_str
    else:
        now_human = "N/A"

    lines += [
        f"**Current conditions** ({now_human})",
        f"{weather_desc}",
        f"  Temperature: **{temp}{temp_symbol}** (feels like {feels_like}{temp_symbol})",
        f"  Humidity: {humidity}%",
        f"  Wind: {wind_speed} {wind_unit} (gusts: {wind_gusts} {wind_unit}), direction: {_wind_dir(wind_dir)}",
        f"  Precipitation: {precip} mm",
        f"  Cloud cover: {cloud_cover}%",
        "",
    ]

    # Daily forecast
    times = daily.get("time", [])
    if times:
        lines.append(f"**{len(times)}-day forecast**\n")
        for i, day_str in enumerate(times):
            day_lines = _format_day(
                daily, i, temp_symbol, wind_unit
            )
            lines.extend(day_lines)
            lines.append("")

    return "\n".join(lines)


def _format_day(daily: dict, idx: int, temp_symbol: str, wind_unit: str) -> list[str]:
    """Format a single day of forecast data."""
    date_str = daily["time"][idx] if idx < len(daily["time"]) else "Unknown"
    code = daily.get("weather_code", [None])[idx]
    desc = _describe_weather(code) if code is not None else "Unknown"

    t_min = daily.get("temperature_2m_min", ["N/A"])[idx]
    t_max = daily.get("temperature_2m_max", ["N/A"])[idx]
    at_min = daily.get("apparent_temperature_min", ["N/A"])[idx]
    at_max = daily.get("apparent_temperature_max", ["N/A"])[idx]
    precip = daily.get("precipitation_sum", ["N/A"])[idx]
    precip_prob = daily.get("precipitation_probability_max", ["N/A"])[idx]
    wind_max = daily.get("wind_speed_10m_max", ["N/A"])[idx]
    gust_max = daily.get("wind_gusts_10m_max", ["N/A"])[idx]
    sunrise = daily.get("sunrise", [""])[idx]
    sunset = daily.get("sunset", [""])[idx]

    sunrise_str = _format_time(sunrise) if sunrise else "N/A"
    sunset_str = _format_time(sunset) if sunset else "N/A"

    lines = [
        f"**{date_str}** — {desc}",
        f"  Temp: {t_min}{temp_symbol} / {t_max}{temp_symbol} (feels like {at_min}–{at_max}{temp_symbol})",
    ]
    if precip_prob != "N/A":
        lines.append(f"  Precipitation: {precip} mm (probability: {precip_prob}%)")
    else:
        lines.append(f"  Precipitation: {precip} mm")
    lines.append(f"  Wind: up to {wind_max} {wind_unit} (gusts: {gust_max} {wind_unit})")
    lines.append(f"  Sunrise: {sunrise_str} | Sunset: {sunset_str}")
    return lines


def _format_time(iso_str: str) -> str:
    """Extract time from ISO string."""
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str


def _wind_dir(degrees: int | float) -> str:
    """Convert wind direction degrees to compass direction."""
    directions = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"
    ]
    try:
        idx = round((degrees or 0) / 22.5) % 16
        return f"{directions[idx]} ({degrees}°)"
    except Exception:
        return str(degrees)
