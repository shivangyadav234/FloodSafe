import { useState, useMemo, useRef, useCallback, useEffect, memo } from "react";
import Map, {
  Layer,
  NavigationControl,
  Popup,
  Source
} from "react-map-gl/maplibre";
import type { MapRef } from "react-map-gl/maplibre";
import type {
  MapLayerMouseEvent,
  FillLayerSpecification,
  LineLayerSpecification,
  CircleLayerSpecification,
} from "maplibre-gl";

import "maplibre-gl/dist/maplibre-gl.css";
import {
  getWardsGeoJSON,
  getGridGeoJSON,
  getWatershedsGeoJSON,
  getIotSensorsGeoJSON,
  getLandslidesGeoJSON,
  getNearestWard,
  getWardDetail,
  type WardFeatureProperties,
} from "../services/ffgsApiService";
import WardDetailPanel from "./WardDetailPanel";

/* ==========================
   STATIC MAP STYLES & CONFIG
========================== */

const INITIAL_VIEW_STATE = {
  longitude: 79.0193,
  latitude: 30.0668,
  zoom: 7
};

// Below this zoom the fine 100-250m grid would be thousands of cells across
// the whole viewport -- only fetch/render it once the user has zoomed in
// enough that a bbox query stays small (see MAX_GRID_CELLS_PER_REQUEST server-side).
const GRID_LAYER_MIN_ZOOM = 12;

const INTERACTIVE_LAYER_IDS = ["ward-risk-fill", "grid-risk-fill"];

const WARD_RISK_FILL_PAINT: FillLayerSpecification["paint"] = {
  "fill-color": [
    "match",
    ["get", "risk_category"],
    "LOW", "#22c55e",
    "MODERATE", "#eab308",
    "HIGH", "#f97316",
    "CRITICAL", "#ef4444",
    "#94a3b8"
  ],
  "fill-opacity": 0.55
};

const WARD_BORDERS_PAINT: LineLayerSpecification["paint"] = {
  "line-color": "#0f172a",
  "line-width": 0.6
};

const GRID_RISK_FILL_PAINT: FillLayerSpecification["paint"] = {
  "fill-color": [
    "interpolate", ["linear"], ["coalesce", ["get", "combined_risk"], 0],
    0, "#22c55e",
    0.4, "#eab308",
    0.6, "#f97316",
    0.8, "#ef4444"
  ],
  "fill-opacity": 0.65
};

const HOVER_GLOW_PAINT: LineLayerSpecification["paint"] = {
  "line-color": "#ffffff",
  "line-width": 6,
  "line-opacity": 0.5,
  "line-blur": 4
};

const IOT_SENSOR_PAINT: CircleLayerSpecification["paint"] = {
  "circle-radius": 5,
  "circle-color": "#38bdf8",
  "circle-stroke-width": 1.5,
  "circle-stroke-color": "#0f172a"
};

const LANDSLIDE_PAINT: CircleLayerSpecification["paint"] = {
  "circle-radius": 5,
  "circle-color": "#a855f7",
  "circle-stroke-width": 1.5,
  "circle-stroke-color": "#0f172a"
};

function getRiskColor(risk?: string | null): string {
  if (risk === "LOW") return "#22c55e";
  if (risk === "MODERATE") return "#eab308";
  if (risk === "HIGH") return "#f97316";
  if (risk === "CRITICAL") return "#ef4444";
  return "#94a3b8";
}

/* ==========================
   COMPONENT
========================== */

interface FloodMapProps {
  /** When provided, the map auto-opens the ward containing this point on load (item 3: "default to the ward user is in"). */
  userLocation?: { lat: number; lon: number } | null;
}

function FloodMapComponent({ userLocation }: FloodMapProps) {
  const mapRef = useRef<MapRef | null>(null);

  const [wardsGeoJSON, setWardsGeoJSON] = useState<GeoJSON.FeatureCollection | null>(null);
  const [gridGeoJSON, setGridGeoJSON] = useState<GeoJSON.FeatureCollection | null>(null);
  const [watershedsGeoJSON, setWatershedsGeoJSON] = useState<GeoJSON.FeatureCollection | null>(null);
  const [iotGeoJSON, setIotGeoJSON] = useState<GeoJSON.FeatureCollection | null>(null);
  const [landslidesGeoJSON, setLandslidesGeoJSON] = useState<GeoJSON.FeatureCollection | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [hoveredWard, setHoveredWard] = useState<WardFeatureProperties | null>(null);
  const [popupLocation, setPopupLocation] = useState<{ longitude: number; latitude: number } | null>(null);
  const [selectedWardId, setSelectedWardId] = useState<number | null>(null);

  const [showGrid, setShowGrid] = useState(true);
  const [showWatersheds, setShowWatersheds] = useState(false);
  const [showIot, setShowIot] = useState(true);
  const [showLandslides, setShowLandslides] = useState(true);

  const rafId = useRef<number | null>(null);

  /* Static layers: fetched once. */
  useEffect(() => {
    getWardsGeoJSON()
      .then((fc) => setWardsGeoJSON(fc as GeoJSON.FeatureCollection))
      .catch((err) => setLoadError(err.message || "Failed to load ward risk data."));

    getWatershedsGeoJSON()
      .then((fc) => setWatershedsGeoJSON(fc as GeoJSON.FeatureCollection))
      .catch(() => {});

    getIotSensorsGeoJSON()
      .then((fc) => setIotGeoJSON(fc as GeoJSON.FeatureCollection))
      .catch(() => {});

    getLandslidesGeoJSON()
      .then((fc) => setLandslidesGeoJSON(fc as GeoJSON.FeatureCollection))
      .catch(() => {});
  }, []);

  /*
   * Default-select the user's own ward on load, if we know where they are --
   * but only if it actually has scored risk data. Auto-opening the (full-
   * viewport, blurred-backdrop) detail panel for a ward with no data yet
   * just blocks the whole map behind an empty error state.
   */
  useEffect(() => {
    if (!userLocation) return;
    getNearestWard(userLocation.lat, userLocation.lon)
      .then((ward) => getWardDetail(ward.ward_id).then(() => ward.ward_id))
      .then((wardId) => setSelectedWardId(wardId))
      .catch(() => {});
    // Only run once per mount -- the user can close/reselect afterward.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* Grid layer: viewport-scoped, refetched on move once zoomed in enough. */
  const fetchGridForViewport = useCallback(() => {
    const map = mapRef.current?.getMap();
    if (!map || map.getZoom() < GRID_LAYER_MIN_ZOOM) {
      setGridGeoJSON(null);
      return;
    }
    const bounds = map.getBounds();
    getGridGeoJSON({
      minLon: bounds.getWest(),
      minLat: bounds.getSouth(),
      maxLon: bounds.getEast(),
      maxLat: bounds.getNorth(),
    })
      .then((fc) => setGridGeoJSON(fc as GeoJSON.FeatureCollection))
      .catch(() => setGridGeoJSON(null));
  }, []);

  const handleMouseMove = useCallback((event: MapLayerMouseEvent) => {
    const feature = event.features?.[0];
    const props = feature?.properties as WardFeatureProperties | undefined;

    if (!props?.ward_id) {
      setHoveredWard(null);
      setPopupLocation(null);
      event.target.getCanvas().style.cursor = "";
      return;
    }

    event.target.getCanvas().style.cursor = "pointer";
    const lng = event.lngLat.lng;
    const lat = event.lngLat.lat;

    if (rafId.current !== null) cancelAnimationFrame(rafId.current);
    rafId.current = requestAnimationFrame(() => {
      setHoveredWard(props);
      setPopupLocation({ longitude: lng, latitude: lat });
    });
  }, []);

  const handleMouseLeave = useCallback(() => {
    setHoveredWard(null);
    setPopupLocation(null);
  }, []);

  const handleClick = useCallback((event: MapLayerMouseEvent) => {
    const feature = event.features?.[0];
    const wardId = feature?.properties?.ward_id as number | undefined;
    if (wardId) setSelectedWardId(wardId);
  }, []);

  const hoverFilter = useMemo(() => {
    return (hoveredWard
      ? ["==", ["get", "ward_id"], hoveredWard.ward_id]
      : ["==", ["get", "ward_id"], -1]) as unknown as import("maplibre-gl").FilterSpecification;
  }, [hoveredWard]);

  return (
    <div style={{ width: "100%", height: "560px", position: "relative" }}>
      <Map
        ref={mapRef}
        initialViewState={INITIAL_VIEW_STATE}
        mapStyle="https://demotiles.maplibre.org/style.json"
        interactiveLayerIds={INTERACTIVE_LAYER_IDS}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onLoad={fetchGridForViewport}
        onMoveEnd={fetchGridForViewport}
      >
        <NavigationControl position="top-right" />

        {/* ===== STATE OUTLINE ===== */}
        <Source id="uttarakhand" type="geojson" data="/data/uttarakhand.geojson">
          <Layer id="uttarakhand-border" type="line" paint={{ "line-color": "#0f172a", "line-width": 2 }} />
        </Source>

        {/* ===== WARD-LEVEL RISK (real village boundaries) ===== */}
        {wardsGeoJSON && (
          <Source id="wards" type="geojson" data={wardsGeoJSON}>
            <Layer id="ward-risk-fill" type="fill" paint={WARD_RISK_FILL_PAINT} />
            <Layer id="ward-borders" type="line" paint={WARD_BORDERS_PAINT} />
            <Layer id="ward-hover-glow" type="line" filter={hoverFilter} paint={HOVER_GLOW_PAINT} />
          </Source>
        )}

        {/* ===== WATERSHED BOUNDARIES (optional) ===== */}
        {showWatersheds && watershedsGeoJSON && (
          <Source id="watersheds" type="geojson" data={watershedsGeoJSON}>
            <Layer
              id="watershed-borders"
              type="line"
              paint={{ "line-color": "#38bdf8", "line-width": 1, "line-dasharray": [2, 2] }}
            />
          </Source>
        )}

        {/* ===== FINE-GRID RISK (100-250m cells, viewport-scoped) ===== */}
        {showGrid && gridGeoJSON && (
          <Source id="grid" type="geojson" data={gridGeoJSON}>
            <Layer id="grid-risk-fill" type="fill" paint={GRID_RISK_FILL_PAINT} />
          </Source>
        )}

        {/* ===== IOT SENSORS ===== */}
        {showIot && iotGeoJSON && (
          <Source id="iot-sensors" type="geojson" data={iotGeoJSON}>
            <Layer id="iot-sensor-points" type="circle" paint={IOT_SENSOR_PAINT} />
          </Source>
        )}

        {/* ===== HISTORICAL LANDSLIDES ===== */}
        {showLandslides && landslidesGeoJSON && (
          <Source id="landslides" type="geojson" data={landslidesGeoJSON}>
            <Layer id="landslide-points" type="circle" paint={LANDSLIDE_PAINT} />
          </Source>
        )}

        {/* ===== HOVER POPUP ===== */}
        {hoveredWard && popupLocation && (
          <Popup
            longitude={popupLocation.longitude}
            latitude={popupLocation.latitude}
            closeButton={false}
            closeOnClick={false}
            offset={20}
          >
            <div className="district-popup">
              <p className="popup-small">WARD</p>
              <h2>{hoveredWard.ward_name}</h2>

              <div className="popup-stat">
                <span>⚠ Risk Level</span>
                <strong style={{ color: getRiskColor(hoveredWard.risk_category) }}>
                  {hoveredWard.risk_category ?? "Unassigned"}
                </strong>
              </div>

              <div className="popup-stat">
                <span>📊 Risk Score</span>
                <strong>{hoveredWard.ward_risk_score != null ? hoveredWard.ward_risk_score.toFixed(2) : "—"}</strong>
              </div>

              <p className="popup-description">Click for full ward details.</p>
            </div>
          </Popup>
        )}
      </Map>

      {/* ===== LAYER TOGGLES ===== */}
      <div className="risk-legend" style={{ top: 12, right: 60 }}>
        <h3>Layers</h3>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={showGrid} onChange={(e) => setShowGrid(e.target.checked)} />
          Fine grid (zoom ≥ {GRID_LAYER_MIN_ZOOM})
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={showWatersheds} onChange={(e) => setShowWatersheds(e.target.checked)} />
          Watershed boundaries
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={showIot} onChange={(e) => setShowIot(e.target.checked)} />
          IoT sensors
        </label>
        <label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
          <input type="checkbox" checked={showLandslides} onChange={(e) => setShowLandslides(e.target.checked)} />
          Historical landslides
        </label>
      </div>

      {/* ===== FLOOD RISK LEGEND ===== */}
      <div className="risk-legend">
        <h3>Flood Risk</h3>
        <div className="legend-item">
          <span className="legend-color" style={{ background: "#22c55e" }}></span>
          Low
        </div>
        <div className="legend-item">
          <span className="legend-color" style={{ background: "#eab308" }}></span>
          Moderate
        </div>
        <div className="legend-item">
          <span className="legend-color" style={{ background: "#f97316" }}></span>
          High
        </div>
        <div className="legend-item">
          <span className="legend-color" style={{ background: "#ef4444" }}></span>
          Critical
        </div>
      </div>

      {loadError && (
        <div style={{ position: "absolute", bottom: 12, left: 12, color: "#f87171", fontSize: 13 }}>
          ⚠ {loadError}
        </div>
      )}

      {selectedWardId != null && (
        <WardDetailPanel wardId={selectedWardId} onClose={() => setSelectedWardId(null)} />
      )}
    </div>
  );
}

const FloodMap = memo(FloodMapComponent);
export default FloodMap;
