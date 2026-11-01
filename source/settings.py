"""Validated defaults shared by commands, persistence and voice handling."""


def normalize_settings(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Settings must be a JSON object")
    result = dict(value)
    result.setdefault("RoomMode", "temporary")
    if result["RoomMode"] not in ("temporary", "semi"):
        raise ValueError("RoomMode must be temporary or semi")
    limit = result.get("MaxVoiceChannels", 1)
    if isinstance(limit, str) and limit.isdecimal():
        limit = int(limit)  # Repair numbers stored by the old JSON setter.
    if type(limit) is not int or limit < 1:
        raise ValueError("MaxVoiceChannels must be a positive integer")
    result["MaxVoiceChannels"] = limit
    names = result.get("NameDefaults", {})
    if not isinstance(names, dict):
        raise ValueError("NameDefaults must be an object")
    names = {"general": "General %randnum%", **names}
    if any(not isinstance(name, str) or not name.strip() or len(name) > 100 for name in names.values()):
        raise ValueError("Channel name templates must contain 1–100 characters")
    result["NameDefaults"] = names
    return result
