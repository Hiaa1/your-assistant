"""今日天气：wttr.in 的 JSON 接口 + 10 分钟内存缓存。

失败时不阻塞页面（返回上一次成功的数据或 None），前端隐藏天气块。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

CITY = "Shanghai"
URL = f"https://wttr.in/{CITY}?format=j1&lang=zh"
TTL = 600

_CACHE: dict[str, Any] = {"at": 0.0, "data": None}

_LABELS = {
    113: "晴",
    116: "多云",
    119: "阴",
    122: "阴",
    143: "雾",
    248: "雾",
    260: "雾",
    176: "阵雨",
    263: "毛毛雨",
    266: "毛毛雨",
    281: "冻雨",
    284: "冻雨",
    293: "小雨",
    296: "小雨",
    299: "中雨",
    302: "中雨",
    305: "大雨",
    308: "大雨",
    311: "冻雨",
    314: "冻雨",
    317: "雨夹雪",
    320: "雨夹雪",
    323: "小雪",
    326: "小雪",
    329: "中雪",
    332: "中雪",
    335: "大雪",
    338: "大雪",
    350: "冰雹",
    353: "阵雨",
    356: "阵雨",
    359: "暴雨",
    362: "雨夹雪",
    365: "雨夹雪",
    368: "阵雪",
    371: "阵雪",
    374: "冰雹",
    377: "冰雹",
    386: "雷阵雨",
    389: "雷阵雨",
    392: "雷雪",
    395: "雷雪",
    179: "阵雪",
    182: "雨夹雪",
    185: "冻雨",
    200: "雷阵雨",
    227: "阵雪",
    230: "暴风雪",
}

_ICONS = {
    113: "sun",
    116: "sun-cloud",
    119: "cloud",
    122: "cloud",
    143: "fog",
    248: "fog",
    260: "fog",
    176: "rain",
    263: "rain",
    266: "rain",
    293: "rain",
    296: "rain",
    299: "rain",
    302: "rain",
    305: "rain",
    308: "rain",
    353: "rain",
    356: "rain",
    359: "rain",
    386: "storm",
    389: "storm",
    179: "snow",
    182: "sleet",
    185: "sleet",
    227: "snow",
    230: "snow",
    323: "snow",
    326: "snow",
    329: "snow",
    332: "snow",
    335: "snow",
    338: "snow",
    368: "snow",
    371: "snow",
    392: "snow",
    395: "snow",
}


def fetch_weather(timeout: float = 8.0) -> dict[str, Any] | None:
    now = time.time()
    cached = _CACHE.get("data")
    if cached and now - float(_CACHE.get("at") or 0) < TTL:
        return cached
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            response = client.get(URL)
            response.raise_for_status()
            payload = response.json()
        current = payload["current_condition"][0]
        today = payload["weather"][0]
        code = int(current.get("weatherCode") or 0)
        desc = _LABELS.get(code, "")
        if not desc:
            desc = str((current.get("weatherDesc") or [{}])[0].get("value") or "")
        astronomy = (today.get("astronomy") or [{}])[0]
        data = {
            "temp": int(current["temp_C"]),
            "feels": int(current.get("FeelsLikeC") or current["temp_C"]),
            "high": int(today["maxtempC"]),
            "low": int(today["mintempC"]),
            "desc": desc,
            "icon": _ICONS.get(code, "cloud"),
            "humidity": int(current.get("humidity") or 0),
            "wind": (
                f"{current.get('windspeedKmph') or 0} km/h {current.get('winddir16Point') or ''}"
            ).strip(),
            "precip": str(current.get("precipMM") or "0.0"),
            "cloud": int(current.get("cloudcover") or 0),
            "uv": str(current.get("uvIndex") or "0"),
            "visibility": str(current.get("visibility") or ""),
            "pressure": str(current.get("pressure") or ""),
            "sunrise": str(astronomy.get("sunrise") or ""),
            "sunset": str(astronomy.get("sunset") or ""),
            "observed": str(
                current.get("localObsDateTime") or current.get("observation_time") or ""
            ),
        }
    except Exception:  # noqa: BLE001 - 天气失败不能影响页面
        return cached
    _CACHE.update(at=now, data=data)
    return data
