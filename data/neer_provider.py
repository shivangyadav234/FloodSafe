import requests


class NEERFloodProvider:

    BASE_URL = "https://flood-api.neer.io"

    def get_flood_risk(self, lat, lon):

        url = f"{self.BASE_URL}/v1/point"

        params = {
            "lat": lat,
            "lon": lon
        }

        response = requests.get(
            url,
            params=params,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        return data


if __name__ == "__main__":

    provider = NEERFloodProvider()

    # Ghaziabad test point
    lat = 28.6692
    lon = 77.4538

    print("Querying NEER...")
    print()

    result = provider.get_flood_risk(
        lat,
        lon
    )

    print("NEER FLOOD DATA")
    print("----------------")

    print(result)