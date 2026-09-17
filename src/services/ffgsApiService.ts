/**
 * Client for the FFGS read API (ml-pipeline/src/api/main.py) — grid/ward
 * risk data for the map, ward detail panel, rainfall panel and alert panel.
 * Separate from sosApiService.ts, which talks to the Node SOS backend.
 */

const FFGS_API_URL = import.meta.env.VITE_FFGS_API_URL || "/ffgs-api";

export class FfgsApiError extends Error {
  status: number;
  constructor(path: string, status: number) {
    super(`FFGS API ${path} returned ${status}`);
    this.status = status;
  }
}

export interface WardFeatureProperties {
  ward_id: number;
  ward_name: string;
  population: number | null;
  avg_risk: number | null;
  max_risk: number | null;
  high_risk_area_pct: number | null;
  exposure_score: number | null;
  ward_risk_score: number | null;
  risk_category: "LOW" | "MODERATE" | "HIGH" | "CRITICAL" | null;
  confidence: number | null;
  valid_for: string;
}

export interface GridCellProperties {
  cell_id: number;
  ward_id: number | null;
  combined_risk: number | null;
  ml_probability: number | null;
  hydrological_threat: number | null;
  confidence: number | null;
}

export interface WardDetail {
  ward_id: number;
  ward_name: string;
  population_exposed: number | null;
  flood_probability: number | null;
  risk_level: string | null;
  confidence: number | null;
  lead_time_minutes: number | null; // always null today — see docs/TODO.md (no forecast rainfall)
  rainfall_mm: { "1h": number | null; "3h": number | null; "6h": number | null; "12h": number | null; "24h": number | null };
  soil_moisture_pct: number | null;
  hydrological_threat: number | null;
  high_risk_area_pct: number | null;
  avg_risk: number | null;
  max_risk: number | null;
  ward_risk_score: number | null;
  valid_for: string;
}

export interface WardTimeseriesPoint {
  observed_at: string;
  rain_mm_6h: number | null;
  dynamic_threshold_mm: number | null;
}

export interface ActiveAlert {
  ward_id: number;
  ward_name: string;
  risk_category: "HIGH" | "CRITICAL";
  ward_risk_score: number;
  confidence: number | null;
  valid_for: string;
  lead_time_minutes: null;
}

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(`${FFGS_API_URL}${path}`);
  if (!res.ok) {
    throw new FfgsApiError(path, res.status);
  }
  return res.json();
}

export interface WardSearchResult {
  ward_id: number;
  ward_name: string;
  population: number | null;
  risk_category: "LOW" | "MODERATE" | "HIGH" | "CRITICAL" | null;
  ward_risk_score: number | null;
  lon: number;
  lat: number;
}

export function getWardsGeoJSON(bbox: { minLon: number; minLat: number; maxLon: number; maxLat: number }) {
  const params = new URLSearchParams({
    min_lon: String(bbox.minLon),
    min_lat: String(bbox.minLat),
    max_lon: String(bbox.maxLon),
    max_lat: String(bbox.maxLat),
  });
  return getJSON<GeoJSON.FeatureCollection<GeoJSON.Geometry, WardFeatureProperties>>(`/wards.geojson?${params}`);
}

export function searchWards(query: string) {
  return getJSON<WardSearchResult[]>(`/wards/search?q=${encodeURIComponent(query)}`);
}

export function getGridGeoJSON(bbox: { minLon: number; minLat: number; maxLon: number; maxLat: number }) {
  const params = new URLSearchParams({
    min_lon: String(bbox.minLon),
    min_lat: String(bbox.minLat),
    max_lon: String(bbox.maxLon),
    max_lat: String(bbox.maxLat),
  });
  return getJSON<GeoJSON.FeatureCollection<GeoJSON.Geometry, GridCellProperties> & { truncated: boolean }>(
    `/grid.geojson?${params}`
  );
}

export function getWardDetail(wardId: number) {
  return getJSON<WardDetail>(`/ward/${wardId}`);
}

export function getWardTimeseries(wardId: number) {
  return getJSON<WardTimeseriesPoint[]>(`/ward/${wardId}/timeseries`);
}

export function getNearestWard(lat: number, lon: number) {
  return getJSON<{ ward_id: number; ward_name: string; distance_m: number }>(
    `/wards/nearest?lat=${lat}&lon=${lon}`
  );
}

export function getActiveAlerts() {
  return getJSON<ActiveAlert[]>("/alerts");
}

export function getWatershedsGeoJSON() {
  return getJSON<GeoJSON.FeatureCollection>("/watersheds.geojson");
}

export function getIotSensorsGeoJSON() {
  return getJSON<GeoJSON.FeatureCollection>("/iot-sensors.geojson");
}

export function getLandslidesGeoJSON() {
  return getJSON<GeoJSON.FeatureCollection>("/landslides.geojson");
}
