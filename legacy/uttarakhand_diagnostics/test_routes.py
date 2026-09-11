from .routing_engine import calculate_route

tests = [
    ("Dehradun", 30.3165, 78.0322, "Haridwar", 29.9457, 78.1642),
    ("Rishikesh", 30.1087, 78.2916, "Devprayag", 30.1460, 78.5980),
    ("Haldwani", 29.2183, 79.5130, "Nainital", 29.3919, 79.4542),
    ("Almora", 29.5971, 79.6591, "Ranikhet", 29.6434, 79.4322),
    ("Srinagar", 30.2225, 78.7832, "Pauri", 30.1460, 78.7800),
]


for start_name, start_lat, start_lon, end_name, end_lat, end_lon in tests:

    print("\n" + "=" * 60)
    print(f"{start_name} → {end_name}")
    print("=" * 60)

    for mode in ["FASTEST", "SAFEST", "BALANCED"]:

        try:
            result = calculate_route(
                start_lat=start_lat,
                start_lon=start_lon,
                end_lat=end_lat,
                end_lon=end_lon,
                mode=mode
            )

            print(
    f"{mode:9} | "
    f"nodes: {len(result['path'])}"
            )

        except Exception as e:
            print(f"{mode:9} | ERROR: {e}")