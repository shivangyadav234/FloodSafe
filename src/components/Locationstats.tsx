import {
  useEffect,
  useState
} from "react";

// Same SOS backend base URL as sosApiService.ts -- there's no dev-server
// proxy for a production static build, so these can't be relative "/api/..."
// paths (they'd hit this site's own origin instead of the backend).
const SOS_API_BASE = import.meta.env.VITE_SOS_API_URL ?? "/api";


type UserLocation = {
  place: string;
  latitude: number;
  longitude: number;
};


type LocationStatsProps = {
  user: UserLocation;
};


type StatsData = {
  rainfall: number | null;
  discharge: number | null;
  shelters: number | null;
  alerts: number | null;
};


function LocationStats({
  user
}: LocationStatsProps) {

  const [stats, setStats] =
    useState<StatsData>({
      rainfall: null,
      discharge: null,
      shelters: null,
      alerts: null
    });


  const [loading, setLoading] =
    useState(true);


  /* =====================================
     DISTANCE BETWEEN TWO COORDINATES
  ====================================== */

  const getDistance = (
    lat1: number,
    lon1: number,
    lat2: number,
    lon2: number
  ) => {

    const R = 6371;

    const dLat =
      ((lat2 - lat1) * Math.PI) / 180;

    const dLon =
      ((lon2 - lon1) * Math.PI) / 180;


    const a =
      Math.sin(dLat / 2) *
        Math.sin(dLat / 2) +

      Math.cos(
        (lat1 * Math.PI) / 180
      ) *

      Math.cos(
        (lat2 * Math.PI) / 180
      ) *

      Math.sin(dLon / 2) *
        Math.sin(dLon / 2);


    const c =
      2 *
      Math.atan2(
        Math.sqrt(a),
        Math.sqrt(1 - a)
      );


    return R * c;
  };


  /* =====================================
     LOAD LIVE LOCATION DATA
  ====================================== */

  useEffect(() => {

    if (
      !user.latitude ||
      !user.longitude
    ) {
      return;
    }


    const loadData = async () => {

      setLoading(true);


      const lat =
        user.latitude;

      const lon =
        user.longitude;


      /* =====================================
         1. RAINFALL - OPEN METEO
      ====================================== */

      let rainfall:
        number | null = null;


      try {

        const weatherURL =

          "https://api.open-meteo.com/v1/forecast" +

          `?latitude=${lat}` +

          `&longitude=${lon}` +

          "&hourly=precipitation" +

          "&past_hours=24" +

          "&forecast_hours=1";


        const response =
          await fetch(weatherURL);


        const data =
          await response.json();


        const rainfallValues:
          number[] =
          data.hourly
            ?.precipitation || [];


        /*
          past_hours=24 gives us
          approximately the previous
          24 hourly values.

          Ignore the final forecast
          value.
        */

        const previous24Hours =
          rainfallValues.slice(
            0,
            24
          );


        rainfall =
          previous24Hours.reduce(
            (
              total,
              value
            ) =>
              total +
              (value || 0),

            0
          );


        rainfall =
          Number(
            rainfall.toFixed(1)
          );


      } catch (error) {

        console.error(
          "Rainfall API error:",
          error
        );

      }


      /* =====================================
         2. RIVER DISCHARGE - GLOFAS
      ====================================== */

      let discharge:
        number | null = null;


      try {

        const floodURL =

          "https://flood-api.open-meteo.com/v1/flood" +

          `?latitude=${lat}` +

          `&longitude=${lon}` +

          "&daily=river_discharge" +

          "&forecast_days=1";


        const response =
          await fetch(floodURL);


        const data =
          await response.json();


        const value =
          data.daily
            ?.river_discharge
            ?.[0];


        if (
          typeof value ===
          "number"
        ) {

          discharge =
            Number(
              value.toFixed(1)
            );

        }

      } catch (error) {

        console.error(
          "Flood API error:",
          error
        );

      }


      /* =====================================
         3. NEARBY EMERGENCY SHELTERS
            OPENSTREETMAP
      ====================================== */

      let shelters:
        number | null = null;


      try {
        // Try backend proxy first, fallback to Overpass public
        let data: any = null;
        try {
          const res = await fetch(`${SOS_API_BASE}/shelters?lat=${lat}&lon=${lon}`);
          if (res.ok) {
            data = await res.json();
          }
        } catch {
          // fallback
        }

        if (!data || !Array.isArray(data.elements)) {
          const overpassQuery = `[out:json][timeout:10];(nwr["amenity"="social_facility"]["social_facility"="shelter"](around:20000,${lat},${lon});nwr["emergency:social_facility"="shelter"](around:20000,${lat},${lon});nwr["evacuation_center"="yes"](around:20000,${lat},${lon}););out center;`;
          const overpassURL = "https://overpass-api.de/api/interpreter?data=" + encodeURIComponent(overpassQuery);
          const response = await fetch(overpassURL);
          if (response.ok) {
            const text = await response.text();
            if (text.startsWith("{")) {
              data = JSON.parse(text);
            }
          }
        }

        shelters = data?.elements?.length ?? 0;
      } catch (error) {
        shelters = 0;
      }


      /* =====================================
         4. OFFICIAL NDMA SACHET ALERTS
      ====================================== */

      let alerts:
        number | null = null;


      try {
        let alertArray: any[] = [];
        try {
          const response = await fetch(`${SOS_API_BASE}/alerts/sachet`);
          if (response.ok) {
            const data = await response.json();
            if (Array.isArray(data)) alertArray = data;
          }
        } catch {
          // If backend proxy unreachable, alertArray remains []
        }


        /*
          Search area descriptions for
          the selected city/state and
          also use the alert centroid
          when available.
        */

        const locationTerms =
          user.place
            .toLowerCase()
            .split(",")
            .map(
              (item) =>
                item.trim()
            )
            .filter(
              (item) =>
                item.length > 2
            );


        const relevantAlerts =
          alertArray.filter(
            (alert: any) => {

              const alertText =

                `${alert.area_description || ""} ` +

                `${alert.warning_message || ""}`

                  .toLowerCase();


              const textMatch =
                locationTerms.some(
                  (location) =>
                    alertText.includes(
                      location
                    )
                );


              if (textMatch) {
                return true;
              }


              /*
                SACHET centroid format:
                longitude,latitude
              */

              if (
                alert.centroid
              ) {

                const coordinates =
                  alert.centroid
                    .split(",")
                    .map(Number);


                if (
                  coordinates.length === 2 &&
                  !Number.isNaN(
                    coordinates[0]
                  ) &&
                  !Number.isNaN(
                    coordinates[1]
                  )
                ) {

                  const alertLon =
                    coordinates[0];

                  const alertLat =
                    coordinates[1];


                  const distance =
                    getDistance(
                      lat,
                      lon,
                      alertLat,
                      alertLon
                    );


                  /*
                    Alert is considered
                    nearby within 100 KM.
                  */

                  return (
                    distance <= 100
                  );

                }

              }


              return false;

            }
          );


        alerts =
          relevantAlerts.length;


      } catch (error) {

        console.error(
          "NDMA SACHET error:",
          error
        );

        /*
          Don't fake an alert number.
          Keep it null if official
          service cannot be reached.
        */

        alerts = null;

      }


      /* =====================================
         UPDATE ALL CARDS
      ====================================== */

      setStats({

        rainfall:
          rainfall,

        discharge:
          discharge,

        shelters:
          shelters,

        alerts:
          alerts

      });


      setLoading(false);

    };


    loadData();


  }, [
    user.latitude,
    user.longitude,
    user.place
  ]);


  /* =====================================
     PAGE
  ====================================== */

  return (

    <section className="local-stats-section">


      {/* LOCATION TITLE */}

      <div className="stats-location-heading">

        <div>

          <p className="small-heading">
            LIVE LOCAL CONDITIONS
          </p>


          <h2>
            📍 {user.place}
          </h2>

        </div>


        <div className="live-data-badge">

          <span className="status-dot">
          </span>

          Live Data

        </div>

      </div>


      {/* =====================================
          CARDS
      ====================================== */}

      <div className="stats">


        {/* RAINFALL */}

        <div className="card">

          <div className="card-icon">
            🌧️
          </div>


          <div>

            <p className="card-title">
              Rainfall
            </p>


            <h2>

              {loading
                ? "..."
                : stats.rainfall !== null
                ? `${stats.rainfall} mm`
                : "—"
              }

            </h2>


            <p className="card-text">
              Previous 24 Hours
            </p>

          </div>

        </div>


        {/* RIVER DISCHARGE */}

        <div className="card">

          <div className="card-icon">
            🌊
          </div>


          <div>

            <p className="card-title">
              River Discharge
            </p>


            <h2>

              {loading
                ? "..."
                : stats.discharge !== null
                ? `${stats.discharge} m³/s`
                : "—"
              }

            </h2>


            <p className="card-text">
              GloFAS Estimate
            </p>

          </div>

        </div>


        {/* SHELTERS */}

        <div className="card">

          <div className="card-icon">
            🏠
          </div>


          <div>

            <p className="card-title">
              Mapped Shelters
            </p>


            <h2>

              {loading
                ? "..."
                : stats.shelters !== null
                ? stats.shelters
                : "—"
              }

            </h2>


            <p className="card-text">
              Within 20 km
            </p>

          </div>

        </div>


        {/* ALERTS */}

        <div className="card">

          <div className="card-icon">
            ⚠️
          </div>


          <div>

            <p className="card-title">
              Official Alerts
            </p>


            <h2>

              {loading
                ? "..."
                : stats.alerts !== null
                ? stats.alerts
                : "—"
              }

            </h2>


            <p className="card-text">
              NDMA SACHET
            </p>

          </div>

        </div>


      </div>

    </section>

  );
}


export default LocationStats;