def calculate_flood_risk(
    flood_ratio,
    flood_depth=0.0,
    water_velocity=0.0
):
    """
    Calculate flood risk for a road segment.

    Parameters
    ----------
    flood_ratio : float
        Fraction of road affected by flood.
        0.0 = no flooding
        1.0 = entire road flooded

    flood_depth : float
        Estimated water depth in meters.

    water_velocity : float
        Estimated water velocity in m/s.

    Returns
    -------
    float
        Risk score between 0 and 1.
    """

    # Keep values within valid ranges

    flood_ratio = max(
        0.0,
        min(1.0, flood_ratio)
    )

    flood_depth = max(
        0.0,
        flood_depth
    )

    water_velocity = max(
        0.0,
        water_velocity
    )

    # -----------------------------------------
    # Flood coverage component
    # -----------------------------------------

    coverage_score = flood_ratio

    # -----------------------------------------
    # Water depth component
    #
    # 0 m      -> 0 risk
    # 0.5 m    -> 0.5
    # 1.0 m+   -> 1.0
    # -----------------------------------------

    depth_score = min(
        flood_depth / 1.0,
        1.0
    )

    # -----------------------------------------
    # Water velocity component
    #
    # 0 m/s -> 0
    # 2 m/s -> 1
    # -----------------------------------------

    velocity_score = min(
        water_velocity / 2.0,
        1.0
    )

    # -----------------------------------------
    # Weighted risk
    # -----------------------------------------

    risk = (
        0.40 * coverage_score
        + 0.40 * depth_score
        + 0.20 * velocity_score
    )

    return min(
        max(risk, 0.0),
        1.0
    )


if __name__ == "__main__":

    test_cases = [
        {
            "name": "No flood",
            "ratio": 0.0,
            "depth": 0.0,
            "velocity": 0.0
        },
        {
            "name": "Light flooding",
            "ratio": 0.2,
            "depth": 0.2,
            "velocity": 0.3
        },
        {
            "name": "Moderate flooding",
            "ratio": 0.5,
            "depth": 0.5,
            "velocity": 0.8
        },
        {
            "name": "Severe flooding",
            "ratio": 0.8,
            "depth": 0.9,
            "velocity": 1.5
        },
        {
            "name": "Extreme flooding",
            "ratio": 1.0,
            "depth": 1.5,
            "velocity": 2.5
        }
    ]

    print("Flood Risk Test")
    print("----------------")

    for case in test_cases:

        risk = calculate_flood_risk(
            case["ratio"],
            case["depth"],
            case["velocity"]
        )

        print(
            f'{case["name"]}: '
            f'Risk = {risk:.3f}'
        )